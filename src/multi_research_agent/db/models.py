"""hub.db 스키마 — SQLAlchemy 2.x ORM.

설계 원칙:
- 모든 테이블에 status enum CHECK 제약 (핸드오버 §2-2)
- created_at / updated_at 자동 갱신
- (status, priority) 복합 인덱스 — supervisor가 매 사이클 폴링
- 가설 폭발 방지용 depth 컬럼 (§3-5)
- task age 추적용 assigned_at (§3-8)
- finding_provenance view는 마이그레이션에서 정의 (§3-10)
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ─── Enums (CHECK constraint로 강제) ──────────────────────────
HYPOTHESIS_STATUS = (
    "pending",  # 신규, 수집 대기
    "collecting",  # collector 진행 중
    "collected",  # 수집 완료, 분석 대기
    "analyzing",  # analyzer 진행 중
    "analyzed",  # 분석 완료, 검증 대기
    "verifying",  # verifier 진행 중
    "significant",  # 검증 통과 — finding 후보
    "rejected",  # 기각
    "needs_refinement",  # 재정의 필요 → hypothesis가 파생 가설 생성
    "failed",  # 실패 (timeout, persistent error)
)

DATASET_STATUS = ("collecting", "collected", "failed")
ANALYSIS_STATUS = ("running", "complete", "irreproducible", "failed")
VERDICT_DECISION = ("significant", "rejected", "needs_refinement")
TASK_PRIORITY = ("high", "medium", "low")
RUBRIC_STAGE = ("sift", "precision")
RUBRIC_DECISION = ("pass", "retry", "fail")


def _enum_check(col: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{col} IN ({quoted})", name=f"ck_{col}")


# ─── Hypothesis ──────────────────────────────────────────────
class Hypothesis(Base):
    __tablename__ = "hypotheses"

    hypothesis_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    factor_name: Mapped[str] = mapped_column(String(255))
    factor_category: Mapped[str] = mapped_column(String(64))
    causal_claim: Mapped[str] = mapped_column(Text)
    data_sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    time_range: Mapped[str | None] = mapped_column(String(64), nullable=True)
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")

    # 가설 폭발 방지 (§3-5)
    parent_hypothesis_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("hypotheses.hypothesis_id"), nullable=True
    )
    depth: Mapped[int] = mapped_column(Integer, default=0)

    # 데드락 방지 (§3-8)
    assigned_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    assigned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 텍스트 메모 (이중 산출물 모델, reference/00_writing_style_guide.md)
    brief_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lead orchestrator 라운드 추적 (4-factor 병렬 spawn 의 공통 라벨)
    round_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    datasets: Mapped[list[Dataset]] = relationship(back_populates="hypothesis")
    analyses: Mapped[list[Analysis]] = relationship(back_populates="hypothesis")
    verdicts: Mapped[list[Verdict]] = relationship(back_populates="hypothesis")
    parent: Mapped[Hypothesis | None] = relationship(
        back_populates="children", remote_side=[hypothesis_id]
    )
    children: Mapped[list[Hypothesis]] = relationship(back_populates="parent")

    __table_args__ = (
        _enum_check("status", HYPOTHESIS_STATUS),
        _enum_check("priority", TASK_PRIORITY),
        Index("ix_hypotheses_status_priority", "status", "priority"),
        Index("ix_hypotheses_assigned_at", "assigned_at"),
        Index("ix_hypotheses_round", "round_id"),
    )


# ─── Dataset ─────────────────────────────────────────────────
class Dataset(Base):
    __tablename__ = "datasets"

    dataset_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hypotheses.hypothesis_id"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # api / scrape / download
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    memo_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="collecting")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    hypothesis: Mapped[Hypothesis] = relationship(back_populates="datasets")

    __table_args__ = (
        _enum_check("status", DATASET_STATUS),
        Index("ix_datasets_hypothesis", "hypothesis_id"),
    )


# ─── Analysis ────────────────────────────────────────────────
class Analysis(Base):
    __tablename__ = "analyses"

    analysis_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hypotheses.hypothesis_id"), nullable=False
    )
    # regression / correlation / panel / event_study
    method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    script_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {p_value, r2, n, ...}
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    hypothesis: Mapped[Hypothesis] = relationship(back_populates="analyses")
    verdicts: Mapped[list[Verdict]] = relationship(back_populates="analysis")

    __table_args__ = (
        _enum_check("status", ANALYSIS_STATUS),
        Index("ix_analyses_hypothesis", "hypothesis_id"),
    )


# ─── Verdict ─────────────────────────────────────────────────
class Verdict(Base):
    __tablename__ = "verdicts"

    verdict_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hypotheses.hypothesis_id"), nullable=False
    )
    analysis_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("analyses.analysis_id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32))
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    reproduced: Mapped[bool | None] = mapped_column(default=None, nullable=True)
    verdict_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    hypothesis: Mapped[Hypothesis] = relationship(back_populates="verdicts")
    analysis: Mapped[Analysis] = relationship(back_populates="verdicts")

    __table_args__ = (
        _enum_check("decision", VERDICT_DECISION),
        Index("ix_verdicts_hypothesis", "hypothesis_id"),
    )


# ─── Finding ─────────────────────────────────────────────────
class Finding(Base):
    __tablename__ = "findings"

    finding_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hypotheses.hypothesis_id"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text)
    significance: Mapped[float | None] = mapped_column(Float, nullable=True)  # 효과 크기 지표
    finding_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Outcomes-style rubric grader 통과 여부 — True 만 SK 납품 큐
    rubric_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ─── Rubric Score (Outcomes-style grader 결과) ───────────────
class RubricScoreRow(Base):
    """grader 가 채점한 한 finding(또는 verdict)의 5개 항목 결과.

    - decision='pass' 인 행이 있는 finding 만 SK 납품 큐 (findings.rubric_pass=True).
    - dominant_failure 열은 weekly_review 가 패턴 추출 (어느 항목이 자주 실패하는가).
    - sift 와 precision 각 1행씩 INSERT (precision 이 최종).
    """

    __tablename__ = "rubric_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    finding_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("findings.finding_id"), nullable=True
    )
    hypothesis_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hypotheses.hypothesis_id"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(16))   # sift | precision
    model: Mapped[str] = mapped_column(String(64))
    weighted_score: Mapped[float] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(16))   # pass | retry | fail
    dominant_failure: Mapped[str | None] = mapped_column(String(64), nullable=True)
    criterion_scores: Mapped[list] = mapped_column(JSON)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        _enum_check("stage", RUBRIC_STAGE),
        _enum_check("decision", RUBRIC_DECISION),
        Index("ix_rubric_scores_hypothesis", "hypothesis_id"),
        Index("ix_rubric_scores_decision_created", "decision", "created_at"),
    )


# ─── Agent Logs ──────────────────────────────────────────────
class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64))  # start / end / error / cost
    task_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_agent_logs_agent_created", "agent", "created_at"),)


# ─── Cost Tracker ────────────────────────────────────────────
class CostEntry(Base):
    __tablename__ = "cost_tracker"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)  # §8-4
    task_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_cost_created", "created_at"),)


# ─── Provenance View 정의 (마이그레이션에서 CREATE VIEW) ──────
PROVENANCE_VIEW_SQL = text("""
CREATE VIEW IF NOT EXISTS finding_provenance AS
SELECT
    f.finding_id,
    f.summary,
    f.significance,
    v.decision,
    v.reasoning,
    v.reproduced,
    a.method,
    a.script_path,
    a.metrics,
    d.file_path,
    d.source_url,
    d.source_type,
    h.hypothesis_id,
    h.factor_name,
    h.factor_category,
    h.causal_claim,
    h.parent_hypothesis_id,
    h.depth,
    h.rationale
FROM findings f
JOIN hypotheses h ON f.hypothesis_id = h.hypothesis_id
LEFT JOIN verdicts v ON v.hypothesis_id = h.hypothesis_id
LEFT JOIN analyses a ON v.analysis_id = a.analysis_id
LEFT JOIN datasets d ON d.hypothesis_id = h.hypothesis_id
""")
