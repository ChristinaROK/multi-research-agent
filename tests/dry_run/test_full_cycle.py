"""통합 dry-run 시나리오 — 가설 1건이 4 에이전트를 모두 거치는지 검증.

dry-run 모드는 실제 LLM 호출 없이 mock 응답을 반환하므로,
실제 에이전트가 DB 변경(가설 status 진행, 데이터셋·분석·판정 INSERT)을 했다고 가정하고
사이클러가 적절한 에이전트를 디스패치하는지만 검증합니다.

이 테스트는 첫 라이브 호출 직전의 마지막 안전망입니다.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

# dry-run 모드 강제
os.environ["RUN_MODE"] = "dry_run"

from multi_research_agent.db.models import (
    AgentLog,
    Analysis,
    Base,
    CostEntry,
    Dataset,
    Finding,
    Hypothesis,
    Verdict,
)
from multi_research_agent.db.session import get_engine, get_session
from multi_research_agent.supervisor import cycle


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch):
    """사이클별 일회용 hub.db. 글로벌 세션 팩토리도 리셋."""
    db = tmp_path / "hub.db"
    monkeypatch.setenv("HUB_DB_PATH", str(db))

    # 모듈 캐시 리셋
    import multi_research_agent.config as cfg
    import multi_research_agent.db.session as sess_mod
    cfg._settings = None  # 새 환경변수 반영
    sess_mod._SessionFactory = None

    engine = get_engine(db)
    Base.metadata.create_all(engine)
    return db


def _make_hypothesis(session, **kwargs) -> Hypothesis:
    defaults = dict(
        hypothesis_id="H-test-001",
        factor_name="foreign_holding_pct",
        factor_category="governance",
        causal_claim="외국인 지분율 ↑ → NAV 할인율 ↓",
        priority="high",
        status="pending",
        depth=0,
        data_sources=[{"source": "pykrx", "fetch": "stock.get_market_cap"}],
        time_range="2018-01-01..2026-04-01",
    )
    defaults.update(kwargs)
    h = Hypothesis(**defaults)
    session.add(h)
    return h


def test_pending_to_collector_dispatch(fresh_db):
    """pending 가설 → collector 호출 → status=collecting 마킹."""
    with get_session() as session:
        _make_hypothesis(session, hypothesis_id="H-001")

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        h = session.get(Hypothesis, "H-001")
        # dry-run runner 가 success 반환만 하지 status 는 사이클러가 미리 collecting 마킹.
        # 실제 에이전트가 DB 갱신을 안 하므로 collecting 상태에서 멈춤.
        assert h.status == "collecting"
        logs = session.query(AgentLog).filter_by(agent="collector").all()
        assert any(log.event == "start" for log in logs)
        assert any(log.event in ("end", "error") for log in logs)


def test_collected_to_analyzer_dispatch(fresh_db):
    """collected 가설 → analyzer 호출."""
    with get_session() as session:
        h = _make_hypothesis(session, hypothesis_id="H-002", status="collected")
        session.flush()
        session.add(Dataset(
            dataset_id="D-001",
            hypothesis_id="H-002",
            file_path="workspace/data/processed/foo.csv",
            rows=1000, status="collected",
        ))

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        h = session.get(Hypothesis, "H-002")
        assert h.status == "analyzing"


def test_analyzed_to_verifier_dispatch(fresh_db):
    """analyzed 가설 → verifier 호출."""
    with get_session() as session:
        h = _make_hypothesis(session, hypothesis_id="H-003", status="analyzed")
        session.flush()
        session.add(Analysis(
            analysis_id="A-001",
            hypothesis_id="H-003",
            method="ols_hac",
            metrics={"p_value": 0.01, "coefficient": -0.18},
            status="complete",
        ))

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        h = session.get(Hypothesis, "H-003")
        assert h.status == "verifying"


def test_priority_order(fresh_db):
    """high → medium → low 순서로 collector 가 픽."""
    with get_session() as session:
        _make_hypothesis(session, hypothesis_id="H-low", priority="low")
        _make_hypothesis(session, hypothesis_id="H-high", priority="high")
        _make_hypothesis(session, hypothesis_id="H-med", priority="medium")

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        # high 만 collecting
        statuses = {h.hypothesis_id: h.status for h in session.query(Hypothesis).all()}
        assert statuses["H-high"] == "collecting"
        assert statuses["H-med"] == "pending"
        assert statuses["H-low"] == "pending"


def test_priority_order_within_verifier_first(fresh_db):
    """analyzed 가 있으면 pending 보다 먼저 처리."""
    with get_session() as session:
        _make_hypothesis(session, hypothesis_id="H-pending", priority="high")
        _make_hypothesis(session, hypothesis_id="H-analyzed", status="analyzed")
        session.flush()
        session.add(Analysis(
            analysis_id="A-1", hypothesis_id="H-analyzed",
            method="ols", status="complete", metrics={"p_value": 0.04},
        ))

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        statuses = {h.hypothesis_id: h.status for h in session.query(Hypothesis).all()}
        assert statuses["H-analyzed"] == "verifying"  # Verifier 먼저
        assert statuses["H-pending"] == "pending"


def test_stale_reclaim(fresh_db):
    """assigned_at 이 4시간 넘은 in_progress → pending 회수."""
    with get_session() as session:
        h = _make_hypothesis(session, hypothesis_id="H-stale", status="collecting")
        h.assigned_to = "collector"
        h.assigned_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=5)

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        h = session.get(Hypothesis, "H-stale")
        # 회수되어 다시 pending → 즉시 collecting 으로 잡힘 (cycle 안의 Collector 호출)
        # 또는 reclaim 후 같은 사이클에서 collector 가 잡음.
        assert h.status in ("pending", "collecting")
        recl = session.query(AgentLog).filter_by(event="reclaim_stale").first()
        assert recl is not None
        assert "H-stale" in recl.payload["hypothesis_ids"]


def test_empty_queue_triggers_hypothesis(fresh_db):
    """전부 비어있을 때 Hypothesis 호출 (가설 보충)."""
    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        logs = session.query(AgentLog).filter_by(agent="hypothesis", event="start").all()
        assert len(logs) == 1


def test_routing_at_collector(fresh_db):
    """needs_refinement + '@ collector:' → 다시 collecting."""
    with get_session() as session:
        h = _make_hypothesis(
            session, hypothesis_id="H-refine-c", status="needs_refinement",
            rejection_reason="@ collector: 결측 60% — KCGS 추가 데이터 필요",
        )

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        h = session.get(Hypothesis, "H-refine-c")
        assert h.status == "collecting"


def test_routing_at_analyzer(fresh_db):
    """needs_refinement + '@ analyzer:' → 다시 analyzing."""
    with get_session() as session:
        _make_hypothesis(
            session, hypothesis_id="H-refine-a", status="needs_refinement",
            rejection_reason="@ analyzer: HAC 미통과, lag 1~5 추가 필요",
        )

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        h = session.get(Hypothesis, "H-refine-a")
        assert h.status == "analyzing"


def test_routing_at_hypothesis(fresh_db):
    """needs_refinement + '@ hypothesis:' → rejected + Hypothesis 새 사이클."""
    with get_session() as session:
        _make_hypothesis(
            session, hypothesis_id="H-refine-h", status="needs_refinement",
            rejection_reason="@ hypothesis: 변수 정의 모호, factor_name 분리 필요",
        )

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        h = session.get(Hypothesis, "H-refine-h")
        assert h.status == "rejected"
        # Hypothesis 호출됐는지
        logs = session.query(AgentLog).filter_by(agent="hypothesis", event="start").all()
        assert len(logs) >= 1


def test_cost_recorded_per_call(fresh_db):
    """dry-run 도 cost_tracker INSERT 는 일어남 ($0)."""
    with get_session() as session:
        _make_hypothesis(session, hypothesis_id="H-cost")

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        entries = session.query(CostEntry).all()
        assert len(entries) == 1
        assert entries[0].agent == "collector"
        assert entries[0].cost_usd == 0.0


def test_no_finding_in_dry_run(fresh_db):
    """dry-run 은 LLM 호출 안 하므로 findings/verdicts 도 자동 생성 X (실제 에이전트 책임)."""
    with get_session() as session:
        _make_hypothesis(session, hypothesis_id="H-no-finding", status="analyzed")
        session.flush()
        session.add(Analysis(
            analysis_id="A-no-find", hypothesis_id="H-no-finding",
            method="ols", status="complete", metrics={"p_value": 0.001},
        ))

    with get_session() as session:
        cycle.dispatch_cycle(session)

    with get_session() as session:
        assert session.query(Finding).count() == 0
        assert session.query(Verdict).count() == 0
