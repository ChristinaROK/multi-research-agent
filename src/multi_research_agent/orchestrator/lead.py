"""Lead orchestrator — 라운드 단위 4-factor 병렬 worker spawn.

설계 메모:
- worker spawn 은 ``concurrent.futures.ThreadPoolExecutor`` 로 4개 동시 (subprocess 호출이라
  asyncio 보다 thread pool 이 단순). max 4 worker.
- Lead T0 는 워커 spawn 직전 1회, T1 은 라운드 가설 모두 종결 후 1회. 그 사이 supervisor 의
  cycle.py 가 collect/analyze/verify 를 직렬 처리한다 (기존 코드 무수정).
- ``--once`` 모드와 호환: Phase 1 에서는 supervisor 가 한 사이클 돈 직후 ``run_round`` 가
  호출되도록 scripts/run_round.py 가 별도. 운영 가동 시점은 사용자가 결정.

Phase 3 마이그레이션: ``spawn_workers`` 내부의 ``run_agent`` 호출 4개를 Managed Agents 의
``beta.sessions.create`` 의 subagents 배열로 교체. 외부 시그니처 (``run_round(session)``) 는 그대로.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from multi_research_agent.config import get_settings
from multi_research_agent.db.models import AgentLog, Hypothesis
from multi_research_agent.supervisor import cost
from multi_research_agent.supervisor.runner import AgentResult, run_agent

logger = logging.getLogger(__name__)

# 4 factor — 외부 평판 관리 도메인. agents/lead/CLAUDE.md §7 와 동일.
FACTORS: tuple[str, ...] = ("esg", "media", "advertising", "governance")

# 가설이 라운드에서 "종결" 로 간주되는 상태들
_TERMINAL_STATUSES = ("significant", "rejected", "failed")


@dataclasses.dataclass(slots=True)
class WorkerOutcome:
    factor: str
    hypothesis_ids: list[str]
    success: bool
    cost_usd: float
    stderr_tail: str
    raw: AgentResult


@dataclasses.dataclass(slots=True)
class RoundResult:
    round_id: str
    plan_path: Path
    workers: list[WorkerOutcome]
    summary_path: Path | None
    terminated_complete: bool       # 모든 가설이 종결된 상태로 T1 까지 갔는가
    duration_sec: float
    total_cost_usd: float


# ─── public entry point ──────────────────────────────────────────────────


def run_round(
    session: Session,
    *,
    round_id: str | None = None,
    factors: tuple[str, ...] = FACTORS,
    max_per_worker: int | None = None,
    wait_for_completion: bool = False,
    completion_timeout_sec: int = 14400,
    completion_poll_sec: int = 60,
) -> RoundResult:
    """라운드 1회 실행.

    Parameters
    ----------
    session : 활성 SQLAlchemy 세션 (caller 가 commit 책임)
    round_id : 미지정 시 ``R-{YYYYMMDD}-{HHMM}``
    factors : 이번 라운드에서 가동할 factor 부분집합. 기본 4개 전체.
    max_per_worker : 각 worker 가 발행할 신규 가설 개수 한도. None 이면 settings 기본값.
    wait_for_completion : True 면 모든 가설이 종결될 때까지 polling 후 T1 호출.
                          False (기본) 면 T0 + spawn 까지만 하고 반환. T1 은 별도 호출.
    """
    s = get_settings()
    started = dt.datetime.now(dt.UTC)

    if round_id is None:
        round_id = f"R-{started.strftime('%Y%m%d-%H%M')}"

    round_dir = s.workspace_dir / "rounds" / round_id
    round_dir.mkdir(parents=True, exist_ok=True)

    n_per_worker = max_per_worker or s.max_new_hypotheses_per_cycle

    # ── T0: Lead → round_plan.md ────────────────────────────────────
    plan_path = _invoke_lead_t0(
        session,
        round_id=round_id,
        round_dir=round_dir,
        factors=factors,
        max_per_worker=n_per_worker,
    )

    # ── spawn 4 factor worker (병렬) ────────────────────────────────
    worker_outcomes = _spawn_workers(
        session,
        round_id=round_id,
        plan_path=plan_path,
        factors=factors,
        max_per_worker=n_per_worker,
    )

    summary_path: Path | None = None
    terminated_complete = False

    if wait_for_completion:
        terminated_complete = _wait_until_terminal(
            session,
            round_id=round_id,
            timeout_sec=completion_timeout_sec,
            poll_sec=completion_poll_sec,
        )
        if terminated_complete:
            summary_path = _invoke_lead_t1(
                session,
                round_id=round_id,
                round_dir=round_dir,
            )

    duration = (dt.datetime.now(dt.UTC) - started).total_seconds()
    total_cost = sum(w.cost_usd for w in worker_outcomes)

    return RoundResult(
        round_id=round_id,
        plan_path=plan_path,
        workers=worker_outcomes,
        summary_path=summary_path,
        terminated_complete=terminated_complete,
        duration_sec=duration,
        total_cost_usd=total_cost,
    )


# ─── Lead T0 ────────────────────────────────────────────────────────────


def _invoke_lead_t0(
    session: Session,
    *,
    round_id: str,
    round_dir: Path,
    factors: tuple[str, ...],
    max_per_worker: int,
) -> Path:
    s = get_settings()
    plan_path = round_dir / "00_round_plan.md"
    task_ref = f"lead-t0-{round_id}"

    session.add(AgentLog(
        agent="lead", event="start", task_ref=task_ref,
        payload={"phase": "t0", "round_id": round_id, "factors": list(factors)},
    ))
    session.flush()

    prompt = (
        f"agents/lead/CLAUDE.md §4 절차로 라운드 plan 을 작성하세요.\n\n"
        f"round_id: {round_id}\n"
        f"factors: {list(factors)}\n"
        f"max_hypotheses_per_worker: {max_per_worker}\n\n"
        f"## 작업 순서 (이 순서로 정확히)\n"
        "1. 직전 1~3 라운드 round_summary.md 읽기:\n"
        "     bash: ls -t workspace/rounds/*/01_round_summary.md 2>/dev/null | head -3\n"
        "     (없으면 첫 라운드, 회고 섹션은 'first round' 로 표기)\n"
        "2. rubric 패턴 SQL:\n"
        "     bash: sqlite3 workspace/hub.db \"SELECT dominant_failure, COUNT(*) FROM "
        "rubric_scores WHERE created_at > datetime('now', '-14 days') GROUP BY dominant_failure;\"\n"
        "3. reference/02_factor_playbook.md Read (카테고리별 변수 후보)\n"
        f"4. {plan_path} 작성 (CLAUDE.md §4 의 7 섹션, 3~6KB)\n"
        "5. 검증: 4 factor 모두 directive 가 채워졌는지 self-check\n\n"
        "## 절대 규칙\n"
        "- 각 directive 는 측정 가능 변수 + 시간 범위 포함 (CLAUDE.md §8)\n"
        "- 이번 라운드 plan 작성만. 가설 INSERT 는 worker 의 책임\n"
    )

    result = run_agent(
        agent="lead",
        prompt=prompt,
        model=s.model_hypothesis,    # Lead 와 Hypothesis 는 같은 Opus
        add_dirs=[s.workspace_dir, s.reference_dir],
        allowed_tools=["Read", "Write", "Bash"],
        max_turns=20,
        task_ref=task_ref,
    )
    _record_cost(session, agent="lead", result=result, task_ref=task_ref)
    return plan_path


# ─── 4 factor worker 병렬 spawn ────────────────────────────────────────


def _spawn_workers(
    session: Session,
    *,
    round_id: str,
    plan_path: Path,
    factors: tuple[str, ...],
    max_per_worker: int,
) -> list[WorkerOutcome]:
    s = get_settings()

    def _run_one(factor: str) -> WorkerOutcome:
        task_ref = f"factor_worker_{factor}-{round_id}"
        prompt = _build_worker_prompt(
            factor=factor,
            round_id=round_id,
            plan_path=plan_path,
            max_per_worker=max_per_worker,
        )
        result = run_agent(
            agent="factor_worker",
            prompt=prompt,
            model=s.model_collector,     # worker = Sonnet (cost optimal)
            add_dirs=[s.workspace_dir, s.reference_dir],
            allowed_tools=["Read", "Write", "Bash", "WebSearch"],
            max_turns=30,
            task_ref=task_ref,
        )
        hyp_ids = _extract_hypothesis_ids(result.stdout)
        return WorkerOutcome(
            factor=factor,
            hypothesis_ids=hyp_ids,
            success=result.success,
            cost_usd=result.cost_usd,
            stderr_tail=(result.stderr or "")[-300:],
            raw=result,
        )

    outcomes: list[WorkerOutcome] = []
    # SK 프로젝트는 외부 SQLite + subprocess 라 GIL 영향 작음. ThreadPoolExecutor 충분.
    with ThreadPoolExecutor(max_workers=len(factors)) as pool:
        futures = {pool.submit(_run_one, f): f for f in factors}
        for fut in as_completed(futures):
            factor = futures[fut]
            try:
                outcomes.append(fut.result())
            except Exception as exc:
                logger.exception("factor_worker %s 실패", factor)
                outcomes.append(WorkerOutcome(
                    factor=factor,
                    hypothesis_ids=[],
                    success=False,
                    cost_usd=0.0,
                    stderr_tail=str(exc)[-300:],
                    raw=AgentResult(
                        agent="factor_worker",
                        success=False,
                        stdout="",
                        stderr=str(exc),
                        duration_sec=0.0,
                    ),
                ))

    # 비용/로그 일괄 기록 (SQLite WAL 이라 같은 세션에서 직렬화)
    for outcome in outcomes:
        _record_cost(
            session,
            agent=f"factor_worker_{outcome.factor}",
            result=outcome.raw,
            task_ref=f"factor_worker_{outcome.factor}-{round_id}",
        )

    return outcomes


def _build_worker_prompt(*, factor: str, round_id: str, plan_path: Path, max_per_worker: int) -> str:
    return (
        f"agents/factor_worker/CLAUDE.md §1 contract 로 작업하세요.\n\n"
        f"factor: {factor}\n"
        f"round_id: {round_id}\n"
        f"max_hypotheses: {max_per_worker}\n"
        f"directive 위치: {plan_path}  (§3 의 '{factor}:' 항목만 읽고 다른 factor 는 무시)\n\n"
        "## 작업 순서 (이 순서로 정확히)\n"
        f"1. {plan_path} Read → §3 '{factor}:' directive 추출 + §4 금지 영역 확인\n"
        f"2. reference/00_writing_style_guide.md §4-1 (brief 표준 7 섹션) Read\n"
        f"3. reference/02_factor_playbook.md 의 '{factor}' 섹션 Read\n"
        f"4. configs/rubric.yaml 5개 항목 인지 (brief §6 self-check 용)\n"
        f"5. hub.db 에서 같은 factor 의 기존 가설 SELECT:\n"
        f"     bash: sqlite3 workspace/hub.db \"SELECT hypothesis_id, factor_name, status "
        f"FROM hypotheses WHERE factor_category='{factor}';\"\n"
        f"6. 신규 가설 {max_per_worker} 개 brainstorm. 각 가설마다:\n"
        f"   a) hypothesis_id = H-{{YYYYMMDD}}-{{NNN}} (NNN 충돌 회피)\n"
        f"   b) workspace/briefs/{{hid}}/01_hypothesis_brief.md (CLAUDE.md §3 추가 2줄 명시)\n"
        f"   c) brief §6 마지막에 'rubric self-check' 5줄 (5개 항목 각각 anchor 8 이상 가능한가)\n"
        f"   d) hypotheses INSERT — round_id='{round_id}', factor_category='{factor}', "
        f"status='pending', depth=0\n"
        f"7. 마지막에 응답 끝에 'CREATED_HYPOTHESES=[\"H-...\", ...]' JSON 한 줄 출력\n"
        f"   (orchestrator 가 이 줄을 파싱해 round 추적)\n\n"
        "## 절대 규칙\n"
        f"- factor 침범 금지 — 다른 factor 변수 등장 시 즉시 제거 후 재작성\n"
        f"- 내부 자료(SK 광고비, 매출) 가설 발행 금지 (CLAUDE.md §1 tool guidance)\n"
        f"- rubric self-check 통과 못할 가설은 발행 자체 안 함 (CLAUDE.md §3)\n"
    )


# ─── 라운드 종결 polling ───────────────────────────────────────────────


def _wait_until_terminal(
    session: Session,
    *,
    round_id: str,
    timeout_sec: int,
    poll_sec: int,
) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        active = session.scalar(
            select(func.count(Hypothesis.hypothesis_id)).where(
                Hypothesis.round_id == round_id,
                Hypothesis.status.notin_(_TERMINAL_STATUSES),
            )
        )
        total = session.scalar(
            select(func.count(Hypothesis.hypothesis_id)).where(Hypothesis.round_id == round_id)
        )
        if total and active == 0:
            return True
        time.sleep(poll_sec)
        # session 캐시 무효화 — 다른 프로세스 (supervisor) 가 갱신 중
        session.expire_all()
    logger.warning("round %s: completion timeout %ds", round_id, timeout_sec)
    return False


# ─── Lead T1 ────────────────────────────────────────────────────────────


def _invoke_lead_t1(
    session: Session,
    *,
    round_id: str,
    round_dir: Path,
) -> Path:
    s = get_settings()
    summary_path = round_dir / "01_round_summary.md"
    task_ref = f"lead-t1-{round_id}"

    session.add(AgentLog(
        agent="lead", event="start", task_ref=task_ref,
        payload={"phase": "t1", "round_id": round_id},
    ))
    session.flush()

    prompt = (
        f"agents/lead/CLAUDE.md §5 절차로 round_summary 를 작성하세요.\n\n"
        f"round_id: {round_id}\n\n"
        "## 작업 순서\n"
        f"1. hub.db 슬라이스 SQL:\n"
        f"     bash: sqlite3 workspace/hub.db \"SELECT hypothesis_id, factor_category, status, "
        f"rejection_reason FROM hypotheses WHERE round_id='{round_id}';\"\n"
        f"     bash: sqlite3 workspace/hub.db \"SELECT hypothesis_id, weighted_score, decision, "
        f"dominant_failure FROM rubric_scores WHERE hypothesis_id IN (SELECT hypothesis_id FROM "
        f"hypotheses WHERE round_id='{round_id}') AND stage='precision';\"\n"
        f"     bash: sqlite3 workspace/hub.db \"SELECT f.finding_id, f.summary, f.significance, "
        f"f.rubric_pass FROM findings f JOIN hypotheses h ON f.hypothesis_id=h.hypothesis_id "
        f"WHERE h.round_id='{round_id}';\"\n"
        f"2. 직전 1 라운드 plan.md (있으면) Read — directive 와 결과 비교\n"
        f"3. {summary_path} 작성 (CLAUDE.md §5 의 6 섹션, 4~8KB)\n"
        f"4. 다음 라운드 후보 directive 초안 (§5 의 5) 은 3~5줄 으로 명확하게 — \n"
        f"   다음 T0 가 baseline 으로 사용\n\n"
        "## 절대 규칙\n"
        f"- 새 가설 발행 X (Lead 책임 아님)\n"
        f"- factor 별 표 정확히 (rubric pass/retry/fail 분포 + dominant_failure top3)\n"
        f"- rubric_pass=True 인 finding 만 §3 에 등재 (SK 납품 가능)\n"
    )

    result = run_agent(
        agent="lead",
        prompt=prompt,
        model=s.model_hypothesis,
        add_dirs=[s.workspace_dir, s.reference_dir],
        allowed_tools=["Read", "Write", "Bash"],
        max_turns=20,
        task_ref=task_ref,
    )
    _record_cost(session, agent="lead", result=result, task_ref=task_ref)
    return summary_path


# ─── 헬퍼 ────────────────────────────────────────────────────────────────


def _extract_hypothesis_ids(stdout: str) -> list[str]:
    """worker stdout 마지막 JSON 한 줄에서 'CREATED_HYPOTHESES' 추출."""
    if not stdout:
        return []
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line or "CREATED_HYPOTHESES" not in line:
            continue
        # 'CREATED_HYPOTHESES=[...]' 또는 JSON 객체 안에 키로 들어있을 수 있음
        idx = line.find("[")
        if idx < 0:
            continue
        end = line.rfind("]")
        if end <= idx:
            continue
        try:
            return list(json.loads(line[idx : end + 1]))
        except json.JSONDecodeError:
            continue
    return []


def _record_cost(session: Session, *, agent: str, result: AgentResult, task_ref: str) -> None:
    s = get_settings()
    cost.record(
        session,
        agent=agent,
        model=getattr(s, f"model_{agent}", s.model_supervisor),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
        cost_usd=result.cost_usd,
        estimated_cost_usd=result.estimated_cost_usd,
        task_ref=task_ref,
    )
    session.add(AgentLog(
        agent=agent, event="end" if result.success else "error", task_ref=task_ref,
        payload={
            "success": result.success,
            "duration_sec": result.duration_sec,
            "cost_usd": result.cost_usd,
            "stderr_tail": (result.stderr or "")[-300:],
        },
    ))
