#!/usr/bin/env python3
"""Monitor 사이드 데몬 — 5분 간격 sleep loop.

Supervisor 와 분리된 별도 프로세스. claude --print 로 monitor 에이전트 호출.
실패해도 supervisor 영향 없음 (격리).
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from types import FrameType

from multi_research_agent.config import get_settings
from multi_research_agent.db.session import get_session
from multi_research_agent.supervisor import alerts, cost
from multi_research_agent.supervisor.runner import run_agent

log = logging.getLogger("monitor")
_shutdown = False


def _handle(signum: int, _f: FrameType | None) -> None:
    global _shutdown
    _shutdown = True
    log.warning("signal %s — monitor 종료 예약", signal.Signals(signum).name)


def _setup_logging() -> None:
    s = get_settings()
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, s.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(s.logs_dir / "monitor.log", encoding="utf-8"),
        ],
        force=True,
    )


MONITOR_INTERVAL_SEC = 300  # 5분 (핸드오버 §1-5 권고)


def run(*, once: bool = False) -> int:
    _setup_logging()
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    s = get_settings()

    log.info("Monitor 데몬 시작 — interval=%ds mode=%s once=%s",
             MONITOR_INTERVAL_SEC, s.run_mode, once)
    cycle = 0

    while not _shutdown:
        cycle += 1
        try:
            _one_cycle(cycle)
        except Exception:
            log.exception("monitor cycle %d 실패", cycle)
            alerts.alert(f"Monitor 사이클 {cycle} 예외", level="warn")

        if once:
            log.info("--once 모드 — 1 사이클 후 종료")
            break

        for _ in range(MONITOR_INTERVAL_SEC):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Monitor 데몬 종료")
    return 0


def _one_cycle(cycle: int) -> None:
    s = get_settings()
    prompt = (
        f"Monitor 사이클 #{cycle}.\n"
        "agents/monitor/CLAUDE.md 의 절차에 따라:\n"
        "1) hub.db 의 hypotheses/datasets/analyses/verdicts/findings 통계,\n"
        "2) workspace/logs/*.log tail,\n"
        "3) cost_tracker 누적/시간당,\n"
        "4) workspace/STATUS.md 직전 헤드라인\n"
        "을 읽고 STATUS.md 를 갱신하라. 의미 있는 변화가 있으면 Telegram 알림."
    )

    result = run_agent(
        agent="monitor",
        prompt=prompt,
        model=s.model_monitor,
        add_dirs=[s.workspace_dir],
        allowed_tools=["Read", "Write", "Bash"],
        max_turns=15,  # 5는 부족 (hub.db 쿼리 + log tail + STATUS.md R/W = 5~7 turns)
        timeout_sec=300,
        task_ref=f"monitor-{cycle}",
    )

    with get_session() as session:
        cost.record(
            session,
            agent="monitor",
            model=s.model_monitor,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            cost_usd=result.cost_usd,
            estimated_cost_usd=result.estimated_cost_usd,
            task_ref=result.task_ref,
        )

    log.info(
        "monitor cycle %d done: success=%s cost=$%.4f",
        cycle, result.success, result.cost_usd,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="1 사이클 후 종료")
    args = parser.parse_args()
    sys.exit(run(once=args.once))
