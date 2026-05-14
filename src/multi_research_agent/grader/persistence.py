"""hub.db 와 grader 사이의 어댑터.

- RubricScore → rubric_scores 행 INSERT
- pass 결정이면 findings.rubric_pass=True 로 마킹
- Verifier 단계 끝에서 호출되도록 cycle.py 가 import

이 파일은 grader 모듈 안에 둔다 — grader 가 DB 스키마를 알 필요는 없지만,
"채점 결과를 hub.db 에 저장하는 책임" 은 단일 모듈로 두는 게 추적성에 좋다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from multi_research_agent.db.models import Finding, RubricScoreRow
from multi_research_agent.grader.rubric import (
    FindingPayload,
    Grader,
    GraderDecision,
    RubricScore,
)

logger = logging.getLogger(__name__)


def grade_and_persist(
    session: Session,
    payload: FindingPayload,
    *,
    grader: Grader | None = None,
) -> RubricScore:
    """1건 채점 후 hub.db 에 결과 저장.

    cycle.py 가 verifier 종료 직후 호출. 호출 컨텍스트는 SQLAlchemy 세션 트랜잭션 안.
    """
    if grader is None:
        grader = Grader()

    score = grader.evaluate(payload)

    row = RubricScoreRow(
        finding_id=payload.finding_id,
        hypothesis_id=payload.hypothesis_id,
        stage=score.stage,
        model=score.model,
        weighted_score=score.weighted_score,
        decision=score.decision.value,
        dominant_failure=score.dominant_failure,
        criterion_scores=[
            {"name": s.name, "score": s.score, "reasoning": s.reasoning}
            for s in score.criterion_scores
        ],
        input_tokens=score.input_tokens,
        output_tokens=score.output_tokens,
        cache_read_tokens=score.cache_read_tokens,
        cache_creation_tokens=score.cache_creation_tokens,
    )
    session.add(row)

    if score.decision is GraderDecision.PASS and payload.finding_id:
        session.execute(
            update(Finding)
            .where(Finding.finding_id == payload.finding_id)
            .values(rubric_pass=True)
        )

    logger.info(
        "rubric persisted: %s decision=%s weighted=%.2f dominant=%s",
        payload.finding_id,
        score.decision.value,
        score.weighted_score,
        score.dominant_failure,
    )
    session.flush()
    return score


def load_finding_text(workspace_dir: Path, hypothesis_id: str, filename: str) -> str:
    """workspace/briefs/{hid}/{filename} 안전 읽기 — 없으면 빈 문자열."""
    path = workspace_dir / "briefs" / hypothesis_id / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
