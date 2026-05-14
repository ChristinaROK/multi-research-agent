"""add rubric_scores table + round_id columns (Outcomes-style grader + Lead-Worker round)

Revision ID: a3f1c7d22b4e
Revises: 6720e30902e0
Create Date: 2026-05-14 10:00:00.000000

새 layer 통합:
- rubric_scores: 5개 항목 채점 결과 영구 저장. dominant_failure 패턴은 weekly_review 가 추출.
- hypotheses.round_id: Lead orchestrator 가 같은 라운드에서 spawn 한 worker 출력 묶음 추적.
- findings.rubric_pass: grader 통과 finding 만 SK 납품 큐에 올라가게 필터링.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3f1c7d22b4e"
down_revision: Union[str, Sequence[str], None] = "6720e30902e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# initial migration 에서 만든 finding_provenance view 를 batch_alter_table 가 깨므로 (SQLite 의
# RENAME TABLE 패턴), 본 migration 안에서 drop → 컬럼 변경 → recreate 으로 감싼다.
_PROVENANCE_VIEW_SQL = """
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
"""


def upgrade() -> None:
    """Upgrade schema."""
    # view 가 hypotheses/findings 를 참조 → batch_alter_table 가 RENAME 시 view 무결성 오류.
    op.execute("DROP VIEW IF EXISTS finding_provenance")

    # ── rubric_scores ──────────────────────────────────────────────
    op.create_table(
        "rubric_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("finding_id", sa.String(length=32), nullable=True),  # nullable: verdict-only 채점도 허용
        sa.Column("hypothesis_id", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),       # sift | precision
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("weighted_score", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),    # pass | retry | fail
        sa.Column("dominant_failure", sa.String(length=64), nullable=True),
        sa.Column("criterion_scores", sa.JSON(), nullable=False),       # list of {name, score, reasoning}
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "stage IN ('sift', 'precision')",
            name="ck_rubric_scores_stage",
        ),
        sa.CheckConstraint(
            "decision IN ('pass', 'retry', 'fail')",
            name="ck_rubric_scores_decision",
        ),
        sa.ForeignKeyConstraint(["hypothesis_id"], ["hypotheses.hypothesis_id"]),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.finding_id"]),
    )
    op.create_index(
        "ix_rubric_scores_hypothesis", "rubric_scores", ["hypothesis_id"]
    )
    op.create_index(
        "ix_rubric_scores_decision_created",
        "rubric_scores",
        ["decision", "created_at"],
    )

    # ── hypotheses.round_id (Lead orchestrator 추적) ─────────────────
    with op.batch_alter_table("hypotheses", schema=None) as batch_op:
        batch_op.add_column(sa.Column("round_id", sa.String(length=32), nullable=True))
        batch_op.create_index("ix_hypotheses_round", ["round_id"])

    # ── findings.rubric_pass ────────────────────────────────────────
    with op.batch_alter_table("findings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "rubric_pass",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # view 재생성 — finding_provenance 는 컬럼 변경 후 다시 빌드
    op.execute(_PROVENANCE_VIEW_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP VIEW IF EXISTS finding_provenance")

    with op.batch_alter_table("findings", schema=None) as batch_op:
        batch_op.drop_column("rubric_pass")

    with op.batch_alter_table("hypotheses", schema=None) as batch_op:
        batch_op.drop_index("ix_hypotheses_round")
        batch_op.drop_column("round_id")

    op.drop_index("ix_rubric_scores_decision_created", table_name="rubric_scores")
    op.drop_index("ix_rubric_scores_hypothesis", table_name="rubric_scores")
    op.drop_table("rubric_scores")

    # downgrade 이후 view 도 같이 사라짐. 운영 환경에서는 head 로만 운영.
