"""Outcomes-style rubric grader.

기존 Verifier 가 통계 검증을 통과시킨 finding 위에서, 메타 적합성 (인과 명료성, proxy 강도,
외부 검증성, 억지 회피, baseline) 을 5개 항목으로 객관 채점한다.

Phase 1 (현재): Anthropic SDK 직접 호출 (Haiku 1차 sift → Opus 정밀, borderline 만).
Phase 3:        Anthropic Managed Agents 의 beta.outcomes 로 마이그레이션 — 본 모듈의
                ``Grader.evaluate`` 시그니처는 유지, 내부 구현만 hosted 호출로 교체.
"""

from multi_research_agent.grader.persistence import grade_and_persist, load_finding_text
from multi_research_agent.grader.rubric import (
    CriterionScore,
    FindingPayload,
    Grader,
    GraderDecision,
    Rubric,
    RubricCriterion,
    RubricScore,
)

__all__ = [
    "CriterionScore",
    "FindingPayload",
    "Grader",
    "GraderDecision",
    "Rubric",
    "RubricCriterion",
    "RubricScore",
    "grade_and_persist",
    "load_finding_text",
]
