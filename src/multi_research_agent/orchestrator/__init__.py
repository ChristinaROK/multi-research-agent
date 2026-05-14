"""Lead-Worker orchestrator.

기존 supervisor 의 4-stage cycle 위에 "라운드" 개념을 얹는다. 매 라운드:
1. Lead (Opus) 가 round_plan.md 작성 — 4-factor directive 결정
2. 4 worker (Sonnet) 가 병렬 spawn → 각자 1~3 가설을 hub.db 에 INSERT (status=pending, round_id 채움)
3. 기존 supervisor cycle 이 collector/analyzer/verifier 를 직렬 진행 (변경 없음)
4. 라운드 가설들이 모두 종결(significant/rejected/failed) + grader 통과 → Lead T1 으로 round_summary.md
"""

from multi_research_agent.orchestrator.lead import RoundResult, run_round

__all__ = ["RoundResult", "run_round"]
