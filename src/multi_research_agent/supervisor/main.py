"""Supervisor 메인 루프 — polling, dispatch, signal handling, graceful shutdown.

핸드오버 §3-1 (SQLite polling, fswatch 금지)
핸드오버 §6 (4중 안전장치)
핸드오버 §10-1 (graceful shutdown)
"""

from __future__ import annotations

import datetime as dt
import logging
import signal
import sys
import time
from types import FrameType

from sqlalchemy import update

from multi_research_agent.config import get_settings
from multi_research_agent.db.models import Hypothesis
from multi_research_agent.db.session import get_session
from multi_research_agent.supervisor import alerts, cost
from multi_research_agent.supervisor.cycle import dispatch_cycle

log = logging.getLogger(__name__)

_shutdown_requested = False
_started_at: dt.datetime | None = None


def _handle_term(signum: int, _frame: FrameType | None) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    log.warning("signal %s 수신 — 현재 task 완료 후 종료", signal.Signals(signum).name)


def _install_signals() -> None:
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)


def _setup_logging() -> None:
    s = get_settings()
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(s.logs_dir / "supervisor.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=getattr(logging, s.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def _time_exceeded() -> bool:
    s = get_settings()
    if _started_at is None:
        return False
    return (dt.datetime.now(dt.UTC) - _started_at).total_seconds() > s.max_run_hours * 3600


def _stop_signal() -> bool:
    return get_settings().stop_file.exists()


def _rollback_in_progress() -> int:
    """graceful shutdown: in_progress 상태 task → pending 으로 되돌려 다음 run 에서 재개 가능."""
    in_progress_states = ("collecting", "analyzing", "verifying")
    with get_session() as session:
        result = session.execute(
            update(Hypothesis)
            .where(Hypothesis.status.in_(in_progress_states))
            .values(status="pending", assigned_to=None, assigned_at=None)
        )
        return int(result.rowcount or 0)


def run(*, once: bool = False) -> int:
    global _started_at
    _setup_logging()
    _install_signals()
    s = get_settings()
    _started_at = dt.datetime.now(dt.UTC)

    log.info(
        "Supervisor 시작 — mode=%s poll=%ds once=%s",
        s.run_mode, s.poll_interval_sec, once,
    )
    alerts.alert(f"Supervisor 시작 ({s.run_mode}{', once' if once else ''})", level="info")

    while not _shutdown_requested:
        if _stop_signal():
            log.warning("STOP 파일 감지 — 종료")
            alerts.alert("STOP 파일 감지 — graceful shutdown", level="warn")
            break
        if _time_exceeded():
            log.warning("최대 실행시간 도달 — 종료")
            alerts.alert(f"최대 실행시간({s.max_run_hours}h) 도달", level="warn")
            break

        with get_session() as session:
            cost_status = cost.assess(session)
            if not cost_status.can_proceed:
                log.warning("비용 한도로 사이클 스킵: %s", cost_status.reason)
                if once:
                    break
                _sleep(s.poll_interval_sec)
                continue
            try:
                dispatch_cycle(session)
            except Exception:
                log.exception("dispatch_cycle 실패")
                alerts.alert("dispatch_cycle 예외 — 로그 확인", level="urgent")

        if once:
            log.info("--once 모드 — 1 사이클 후 종료")
            break
        _sleep(s.poll_interval_sec)

    rolled = _rollback_in_progress()
    log.info("Graceful shutdown 완료 — in_progress→pending %d건", rolled)
    alerts.alert(f"Supervisor 종료 (rolled back {rolled})", level="info")
    return 0


def _sleep(seconds: float) -> None:
    """SIGTERM 빠른 응답을 위해 짧은 슬립으로 쪼갬."""
    end = time.monotonic() + seconds
    while not _shutdown_requested and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="1 사이클 후 종료")
    args = parser.parse_args()
    sys.exit(run(once=args.once))
