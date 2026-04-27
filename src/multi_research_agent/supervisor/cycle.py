"""사이클 디스패처 — 매 폴링 1 에이전트 호출.

우선순위 (위에서 아래로 검사):
  0) 데드락 청소: assigned_at 이 MAX_TASK_AGE_HOURS 초과한 in_progress → pending 회수
  1) status='analyzed' → Verifier (마지막 단계 우선, 큐 비움)
  2) status='collected' → Analyzer
  3) status='pending' (priority high>medium>low) → Collector
  4) status='needs_refinement' → 라우팅 prefix 따라 Hypothesis/Collector/Analyzer
  5) pending < MAX_NEW_HYPOTHESES_PER_CYCLE → Hypothesis (가설 보충)
  6) 그 외 idle

큐가 비어있을 때만 새 가설을 추가하므로 한 가설이 끝까지 빠르게 진행.
다중 비교 보정 또한 점진적 변화라 Verifier 가 매번 안정.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from multi_research_agent.config import get_settings
from multi_research_agent.db.models import (
    AgentLog,
    Analysis,
    Dataset,
    Hypothesis,
)
from multi_research_agent.supervisor import cost
from multi_research_agent.supervisor.runner import run_agent

log = logging.getLogger(__name__)

# in_progress 상태들 (rollback / deadlock 정의에 사용)
_IN_PROGRESS_STATUSES = ("collecting", "analyzing", "verifying")

# 라우팅 prefix — Verifier 가 needs_refinement 시 reason 앞에 "@ {agent}:" 로 표기
_ROUTE_PREFIX = {
    "@ collector:": "collector",
    "@ analyzer:": "analyzer",
    "@ hypothesis:": "hypothesis",
}


def dispatch_cycle(session: Session) -> None:
    s = get_settings()
    _reclaim_stale(session, max_age_hours=s.max_task_age_hours)

    # 1) 검증 (analyzed → verifying)
    h = _pick(session, status="analyzed")
    if h is not None:
        _run_verifier(session, h)
        return

    # 2) 분석 (collected → analyzing)
    h = _pick(session, status="collected")
    if h is not None:
        _run_analyzer(session, h)
        return

    # 3) 수집 (pending → collecting)
    h = _pick(session, status="pending", with_priority=True)
    if h is not None:
        _run_collector(session, h)
        return

    # 4) needs_refinement 라우팅
    h = _pick(session, status="needs_refinement")
    if h is not None:
        _run_refinement(session, h)
        return

    # 5) 가설 풀 보충
    pending_count = session.scalar(
        select(func.count(Hypothesis.hypothesis_id)).where(Hypothesis.status == "pending")
    ) or 0
    if pending_count < s.max_new_hypotheses_per_cycle:
        _run_hypothesis(session, current_pending=pending_count)
        return

    log.debug("사이클: idle (pending=%d, 처리할 task 없음)", pending_count)


# ──────────────────────────────────────────────────────────
# 데드락 청소
# ──────────────────────────────────────────────────────────
def _reclaim_stale(session: Session, *, max_age_hours: float) -> None:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=max_age_hours)
    stale = session.execute(
        select(Hypothesis).where(
            Hypothesis.status.in_(_IN_PROGRESS_STATUSES),
            Hypothesis.assigned_at < cutoff,
        )
    ).scalars().all()
    if not stale:
        return
    ids = [h.hypothesis_id for h in stale]
    session.execute(
        update(Hypothesis)
        .where(Hypothesis.hypothesis_id.in_(ids))
        .values(status="pending", assigned_to=None, assigned_at=None)
    )
    session.add(AgentLog(
        agent="supervisor", event="reclaim_stale",
        payload={"hypothesis_ids": ids, "max_age_hours": max_age_hours},
    ))
    log.warning("stale 회수: %s (%d건)", ids, len(ids))


# ──────────────────────────────────────────────────────────
# pick: 한 사이클당 1건만
# ──────────────────────────────────────────────────────────
def _pick(session: Session, *, status: str, with_priority: bool = False) -> Hypothesis | None:
    stmt = select(Hypothesis).where(Hypothesis.status == status)
    if with_priority:
        # high → medium → low 순. CASE WHEN 으로 정렬.
        from sqlalchemy import case
        order_priority = case(
            {"high": 0, "medium": 1, "low": 2},
            value=Hypothesis.priority,
            else_=3,
        )
        stmt = stmt.order_by(order_priority, Hypothesis.created_at)
    else:
        stmt = stmt.order_by(Hypothesis.created_at)
    return session.scalars(stmt.limit(1)).first()


def _claim(session: Session, h: Hypothesis, *, agent: str, new_status: str) -> str:
    """가설을 잡고 in_progress 마킹. task_ref 반환."""
    task_ref = f"{agent}-{h.hypothesis_id}-{secrets.token_hex(3)}"
    session.execute(
        update(Hypothesis)
        .where(Hypothesis.hypothesis_id == h.hypothesis_id)
        .values(status=new_status, assigned_to=agent, assigned_at=dt.datetime.now(dt.UTC))
    )
    session.add(AgentLog(
        agent=agent, event="start", task_ref=task_ref,
        payload={"hypothesis_id": h.hypothesis_id, "from_status": h.status},
    ))
    session.flush()
    return task_ref


def _record(session: Session, *, agent: str, result, task_ref: str) -> None:
    s = get_settings()
    cost.record(
        session, agent=agent,
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
            "stderr_tail": (result.stderr or "")[-400:],
        },
    ))


# ──────────────────────────────────────────────────────────
# 에이전트별 호출
# ──────────────────────────────────────────────────────────
def _run_hypothesis(session: Session, *, current_pending: int) -> None:
    s = get_settings()
    task_ref = f"hypothesis-bulk-{dt.datetime.now(dt.UTC).strftime('%Y%m%d%H%M%S')}"
    n_to_create = s.max_new_hypotheses_per_cycle - current_pending

    session.add(AgentLog(agent="hypothesis", event="start", task_ref=task_ref,
                         payload={"pending_count": current_pending, "n_to_create": n_to_create}))
    session.flush()

    prompt = (
        f"agents/hypothesis/CLAUDE.md 절차로 신규 가설 {n_to_create}개를 생성하세요.\n"
        f"현재 pending 수: {current_pending} (한도 {s.max_new_hypotheses_per_cycle}).\n\n"
        "필수 참조:\n"
        " - reference/00_writing_style_guide.md §4-1 (brief 표준 섹션)\n"
        " - reference/01_domain_knowledge.md (SK NAV 메커니즘 7가지, 함정 4가지)\n"
        " - reference/02_factor_playbook.md (카테고리별 변수 카탈로그 — 카테고리 균형 유지)\n\n"
        "## 작업 순서 (이 순서로 정확히 수행, 단계 누락 금지)\n"
        "1. references 3개 읽기 → 카테고리 분포·기존 가설 검토\n"
        "2. hub.db 의 기존 가설 SQL 쿼리: \n"
        "     bash: sqlite3 workspace/hub.db \"SELECT hypothesis_id, factor_name, factor_category, "
        "status FROM hypotheses;\"\n"
        "3. 각 신규 가설마다:\n"
        "   a) hypothesis_id 결정: H-{YYYYMMDD}-{NNN} 형식 (예: H-20260427-001)\n"
        f"   b) workspace/briefs/{{hypothesis_id}}/01_hypothesis_brief.md 작성 (3~5KB, 7 섹션)\n"
        "   c) **★ DB INSERT 의무 ★** sqlite3 로 hypotheses 테이블에 INSERT:\n"
        "      bash: sqlite3 workspace/hub.db \"INSERT INTO hypotheses(hypothesis_id, factor_name,"
        " factor_category, causal_claim, data_sources, time_range, priority, rationale, status,"
        " depth, brief_path) VALUES (...);\"\n"
        "      data_sources 는 JSON 문자열 (작은따옴표 escape 주의).\n"
        "      status='pending', depth=0 (또는 파생이면 depth+1).\n"
        f"4. 모든 INSERT 완료 후 검증: \n"
        "   bash: sqlite3 workspace/hub.db \"SELECT COUNT(*) FROM hypotheses WHERE status='pending';\"\n"
        f"   결과가 {n_to_create} 이상인지 반드시 확인.\n\n"
        "## 절대 규칙\n"
        f"- brief 파일만 만들고 INSERT 안 하면 작업 실패. 두 산출물 모두 필수.\n"
        f"- 각 가설은 brief_path = 'workspace/briefs/{{hypothesis_id}}/01_hypothesis_brief.md' 로 INSERT.\n"
        f"- 마지막에 SELECT COUNT 로 검증한 결과를 응답에 명시.\n\n"
        "이전 verdicts 의 rejection_reason 도 SQL 로 확인하고, rejected/needs_refinement 사유에서 "
        "파생 가설 후보를 도출 (depth+1, MAX_HYPOTHESIS_DEPTH 거부)."
    )

    result = run_agent(
        agent="hypothesis",
        prompt=prompt,
        model=s.model_hypothesis,
        add_dirs=[s.workspace_dir, s.reference_dir],
        allowed_tools=["Read", "Write", "Bash"],
        max_turns=30,
        task_ref=task_ref,
    )
    _record(session, agent="hypothesis", result=result, task_ref=task_ref)


def _run_collector(session: Session, h: Hypothesis) -> None:
    s = get_settings()
    task_ref = _claim(session, h, agent="collector", new_status="collecting")
    brief_path = h.brief_path or f"workspace/briefs/{h.hypothesis_id}/01_hypothesis_brief.md"
    prompt = (
        "agents/collector/CLAUDE.md 절차로 데이터를 수집하세요.\n\n"
        "**먼저 직전 메모를 Read 의무**:\n"
        f" - {brief_path}\n"
        "   → §4 데이터 수집 계획 의 권고를 따르고, §6 예상 함정 을 정제 결정에 반영.\n\n"
        "필수 참조:\n"
        " - reference/00_writing_style_guide.md §4-2 (memo 표준 섹션)\n"
        " - reference/03_collection_recipes.md (소스별 표준 코드 + ToS 체크)\n\n"
        f"가설: {h.hypothesis_id} ({h.factor_name})\n"
        f"causal_claim: {h.causal_claim}\n"
        f"data_sources: {h.data_sources}\n"
        f"time_range: {h.time_range}\n\n"
        "## 작업 순서 (이 순서대로, 단계 누락 금지)\n"
        "1. brief Read → §4 수집 계획 / §6 함정 인지\n"
        "2. reference/03 의 해당 소스 레시피 Read\n"
        "3. 디스크 사전 체크 (free < 1GB 면 abort)\n"
        "4. 각 데이터 소스별 수집 → CSV 정제 → workspace/data/processed/{factor_name}.csv (원자적 쓰기)\n"
        "5. workspace/briefs/{hypothesis_id}/02_collection_memo.md 작성 (3~8KB, 6 섹션)\n"
        "6. **★ datasets INSERT 의무 ★** 각 csv 파일마다:\n"
        "   bash: sqlite3 workspace/hub.db \"INSERT INTO datasets(dataset_id, hypothesis_id, file_path,"
        " source_url, source_type, rows, bytes_size, quality_notes, memo_path, status) VALUES (...);\"\n"
        "7. **hypotheses 상태 갱신**:\n"
        f"   bash: sqlite3 workspace/hub.db \"UPDATE hypotheses SET status='collected' WHERE"
        f" hypothesis_id='{h.hypothesis_id}';\"\n"
        "   - 전부 실패 → 'failed' + rejection_reason 명시\n"
        "   - 부분 성공도 'collected' (quality_notes 에 누락 명시)\n"
        "8. 검증: SELECT status FROM hypotheses WHERE hypothesis_id='" + h.hypothesis_id + "';\n"
        "   결과를 응답에 명시.\n\n"
        "## 절대 규칙\n"
        "- CSV 만 만들고 datasets INSERT 안 하면 작업 실패\n"
        "- hypotheses.status 갱신 안 하면 다음 사이클이 같은 가설을 또 수집 (무한루프)"
    )
    result = run_agent(
        agent="collector",
        prompt=prompt,
        model=s.model_collector,
        add_dirs=[s.workspace_dir, s.reference_dir],
        allowed_tools=["Read", "Write", "Bash", "WebSearch"],
        max_turns=50,
        timeout_sec=1800,
        task_ref=task_ref,
    )
    _record(session, agent="collector", result=result, task_ref=task_ref)


def _run_analyzer(session: Session, h: Hypothesis) -> None:
    s = get_settings()
    task_ref = _claim(session, h, agent="analyzer", new_status="analyzing")
    datasets = session.scalars(
        select(Dataset).where(Dataset.hypothesis_id == h.hypothesis_id)
    ).all()
    ds_summary = [
        {"file_path": d.file_path, "rows": d.rows, "status": d.status,
         "memo_path": d.memo_path}
        for d in datasets
    ]
    brief_path = h.brief_path or f"workspace/briefs/{h.hypothesis_id}/01_hypothesis_brief.md"
    memo_paths = sorted({d.memo_path for d in datasets if d.memo_path})
    memo_path = memo_paths[0] if memo_paths else \
        f"workspace/briefs/{h.hypothesis_id}/02_collection_memo.md"
    prompt = (
        "agents/analyzer/CLAUDE.md 절차로 분석을 수행하세요.\n\n"
        "**먼저 두 직전 메모를 Read 의무**:\n"
        f" - {brief_path}  → brief §5 분석 설계 권고 = 1차 모델, §7 해석 가이드 인지\n"
        f" - {memo_path}   → memo §6 Analyzer 메모, §3 정제 결정\n\n"
        "필수 참조:\n"
        " - reference/00_writing_style_guide.md §4-3 (report 표준 섹션)\n"
        " - reference/04_analysis_methods.md (방법 카탈로그, 7단계 절차, 함정)\n\n"
        f"가설: {h.hypothesis_id} ({h.factor_name})\n"
        f"causal_claim: {h.causal_claim}\n"
        f"datasets: {ds_summary}\n"
        f"time_range: {h.time_range}\n\n"
        "## 작업 순서\n"
        "1. brief + memo Read\n"
        "2. reference/04 의 해당 분석 방법 Read\n"
        "3. workspace/intermediate/{task_ref}/analyze.py 작성 (절대경로 / seed 고정 / dtype 명시)\n"
        "4. python analyze.py 실행 → result.json 생성 (metrics 표준 키)\n"
        "5. workspace/briefs/{hypothesis_id}/03_analysis_report.md 작성 (5~12KB, 10 섹션)\n"
        "6. **★ analyses INSERT 의무 ★**:\n"
        "   bash: sqlite3 workspace/hub.db \"INSERT INTO analyses(analysis_id, hypothesis_id, method,"
        " script_path, result_summary, metrics, report_path, status) VALUES (...);\"\n"
        "   metrics 는 result.json 내용 그대로 JSON 문자열로.\n"
        "7. **hypotheses 상태 갱신**:\n"
        f"   bash: sqlite3 workspace/hub.db \"UPDATE hypotheses SET status='analyzed' WHERE"
        f" hypothesis_id='{h.hypothesis_id}';\"\n"
        "8. 검증: SELECT status FROM hypotheses WHERE hypothesis_id='" + h.hypothesis_id + "';\n\n"
        "## 절대 규칙\n"
        "- 판정 X — 사실만. 'p<0.01 관측' ✅ / '유의한 영향' ❌\n"
        "- 사전 등록 모델만 시도 (data snooping 회피)\n"
        "- 결측 50%+ → analyses.status='failed', summary='nan_too_high'"
    )
    result = run_agent(
        agent="analyzer",
        prompt=prompt,
        model=s.model_analyzer,
        add_dirs=[s.workspace_dir, s.reference_dir],
        allowed_tools=["Read", "Write", "Bash"],
        max_turns=30,
        timeout_sec=1200,
        task_ref=task_ref,
    )
    _record(session, agent="analyzer", result=result, task_ref=task_ref)


def _run_verifier(session: Session, h: Hypothesis) -> None:
    s = get_settings()
    task_ref = _claim(session, h, agent="verifier", new_status="verifying")
    analyses = session.scalars(
        select(Analysis).where(Analysis.hypothesis_id == h.hypothesis_id)
    ).all()
    ana_summary = [
        {"analysis_id": a.analysis_id, "method": a.method, "metrics": a.metrics,
         "report_path": a.report_path, "script_path": a.script_path}
        for a in analyses
    ]
    brief_path = h.brief_path or f"workspace/briefs/{h.hypothesis_id}/01_hypothesis_brief.md"
    memo_path = f"workspace/briefs/{h.hypothesis_id}/02_collection_memo.md"
    report_paths = sorted({a.report_path for a in analyses if a.report_path})
    report_path = report_paths[0] if report_paths else \
        f"workspace/briefs/{h.hypothesis_id}/03_analysis_report.md"
    prompt = (
        "agents/verifier/CLAUDE.md 절차로 검증·판정하세요.\n\n"
        "**먼저 세 직전 메모를 모두 Read 의무**:\n"
        f" - {brief_path}    → brief §2 메커니즘 부호, §7 해석 가이드\n"
        f" - {memo_path}    → memo §5 데이터 한계\n"
        f" - {report_path}  → report §10 사전 등록 확인, §8 강건성\n\n"
        "필수 참조:\n"
        " - reference/00_writing_style_guide.md §4-4 (verdict), §4-5 (finding)\n"
        " - reference/05_verification_checklist.md\n\n"
        f"가설: {h.hypothesis_id} ({h.factor_name})\n"
        f"causal_claim: {h.causal_claim}\n"
        f"analyses: {ana_summary}\n\n"
        "## 작업 순서\n"
        "1. brief + memo + report 모두 Read\n"
        "2. analyze.py 재실행 → 원본 result.json 과 numeric 비교 (1e-6)\n"
        "3. 활성 가설의 raw p_value SQL 추출 → FDR (BH) 보정\n"
        "4. 강건성 6항목 카운트 + 메커니즘 부호 비교\n"
        "5. workspace/briefs/{hypothesis_id}/04_verdict.md 작성 (3~8KB, 8 섹션)\n"
        "6. significant 시: workspace/briefs/{hypothesis_id}/05_finding.md (2~4KB)\n"
        "7. **★ verdicts INSERT 의무 ★**:\n"
        "   bash: sqlite3 workspace/hub.db \"INSERT INTO verdicts(verdict_id, hypothesis_id,"
        " analysis_id, decision, reasoning, reproduced, verdict_path) VALUES (...);\"\n"
        "8. **significant 시 findings INSERT 의무**:\n"
        "   bash: sqlite3 workspace/hub.db \"INSERT INTO findings(finding_id, hypothesis_id,"
        " summary, significance, finding_path) VALUES (...);\"\n"
        "9. **hypotheses 상태 갱신**:\n"
        f"   bash: sqlite3 workspace/hub.db \"UPDATE hypotheses SET status='<DECISION>',"
        f" rejection_reason=<REASON> WHERE hypothesis_id='{h.hypothesis_id}';\"\n"
        "   decision 별:\n"
        "     significant      → status='significant'\n"
        "     rejected         → status='rejected' + 구체적 rejection_reason\n"
        "     needs_refinement → status='needs_refinement' + '@ {agent}: 사유'\n"
        "10. 검증 SELECT 후 응답에 결과 명시\n\n"
        "## 절대 규칙\n"
        "- 회의주의자 톤 — 모호하면 needs_refinement\n"
        "- significant 6 조건 모두 충족 시만 (재현/FDR/효과크기/강건성≥3/메커니즘/진단)\n"
        "- finding.md 는 SK PR/IR 납품용. 정책 제안 X, 사실만"
    )
    result = run_agent(
        agent="verifier",
        prompt=prompt,
        model=s.model_verifier,
        add_dirs=[s.workspace_dir, s.reference_dir],
        allowed_tools=["Read", "Write", "Bash"],
        max_turns=20,
        timeout_sec=1800,
        task_ref=task_ref,
    )
    _record(session, agent="verifier", result=result, task_ref=task_ref)


def _run_refinement(session: Session, h: Hypothesis) -> None:
    """needs_refinement 라우팅 — rejection_reason 의 prefix 보고 적절 에이전트 호출."""
    target = "hypothesis"  # default
    reason = h.rejection_reason or ""
    for prefix, agent in _ROUTE_PREFIX.items():
        if reason.startswith(prefix):
            target = agent
            break

    if target == "collector":
        # 같은 가설을 다시 수집 (보강) — pending 으로 회복 후 collector 호출
        session.execute(update(Hypothesis).where(Hypothesis.hypothesis_id == h.hypothesis_id)
                        .values(status="pending"))
        _run_collector(session, h)
    elif target == "analyzer":
        session.execute(update(Hypothesis).where(Hypothesis.hypothesis_id == h.hypothesis_id)
                        .values(status="collected"))
        _run_analyzer(session, h)
    else:  # hypothesis 가 정제 (파생 가설 만들기 또는 변수 재정의)
        # 일단 가설 상태를 rejected 로 변경 + Hypothesis 호출 시 파생 검토
        session.execute(update(Hypothesis).where(Hypothesis.hypothesis_id == h.hypothesis_id)
                        .values(status="rejected"))
        pending_count = session.scalar(
            select(func.count(Hypothesis.hypothesis_id)).where(Hypothesis.status == "pending")
        ) or 0
        _run_hypothesis(session, current_pending=pending_count)


# ──────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────
def list_active_hypotheses(session: Session) -> Iterable[Hypothesis]:
    """모니터/디버깅용 — 활성(완료/기각 제외) 가설."""
    inactive = ("significant", "rejected", "failed")
    return session.scalars(select(Hypothesis).where(Hypothesis.status.notin_(inactive))).all()


__all__ = ["dispatch_cycle", "list_active_hypotheses"]
