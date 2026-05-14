"""Rubric grader 단위 테스트.

Anthropic SDK 는 mock 으로 주입. 실제 API 호출 없이 검증.

테스트 포인트:
1. configs/rubric.yaml 로딩 + 가중치 합 = 1.0 검증
2. 명백한 실패 → stage2 skip (clear fail short-circuit)
3. high-score 응답 → pass decision
4. JSON 응답 파싱 (마크다운 코드블록, raw JSON, 둘 다)
5. dominant_failure 자동 계산 (모델이 None 줘도 grader 가 최저 항목 찾음)
6. yaml 가중치 합이 1 이 아니면 즉시 ValueError
7. 가중평균 계산 정확도
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from multi_research_agent.grader.rubric import (
    FindingPayload,
    Grader,
    GraderDecision,
    Rubric,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUBRIC = REPO_ROOT / "configs" / "rubric.yaml"


# ─── helpers ─────────────────────────────────────────────────────────────


def _payload(finding_id: str = "F-TEST-001") -> FindingPayload:
    return FindingPayload(
        finding_id=finding_id,
        hypothesis_id="H-TEST-001",
        factor_category="governance",
        factor_name="board_independence_pct",
        causal_claim="사외이사 비율 상승 시 NAV 할인율 감소",
        brief_text="brief body (3KB)",
        verdict_text="verdict body",
        finding_text="finding body",
        analysis_metrics={"p_value": 0.02, "n": 60},
    )


def _mock_response(json_payload: dict[str, Any], *, in_tokens: int = 1000, out_tokens: int = 300) -> Any:
    """anthropic.messages.create 응답 흉내."""
    text_block = MagicMock()
    text_block.text = f"```json\n{json.dumps(json_payload, ensure_ascii=False)}\n```"
    response = MagicMock()
    response.content = [text_block]
    response.usage = MagicMock(
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return response


def _mock_client(*responses: Any) -> MagicMock:
    """messages.create 가 순서대로 responses 를 반환하는 mock client."""
    client = MagicMock()
    client.messages.create.side_effect = list(responses)
    return client


def _criteria_payload(*scores: float, reasonings: tuple[str, ...] | None = None) -> dict[str, Any]:
    """rubric 의 5개 criterion 에 점수 매핑.

    Note: configs/rubric.yaml 의 criteria 순서가 다음과 같다고 가정 — yaml 변경 시 함께 변경.
    """
    names = (
        "causal_path_clarity",
        "proxy_strength",
        "external_verifiability",
        "not_forced",
        "comparable_baseline",
    )
    assert len(scores) == len(names), f"expected {len(names)} scores, got {len(scores)}"
    reasonings = reasonings or tuple(f"reason {i+1}" for i in range(len(names)))
    return {
        name: {"score": score, "reasoning": reasoning}
        for name, score, reasoning in zip(names, scores, reasonings, strict=True)
    }


# ─── tests ──────────────────────────────────────────────────────────────


def test_rubric_yaml_loads_and_weights_sum_to_one() -> None:
    rubric = Rubric.from_yaml(DEFAULT_RUBRIC)
    assert len(rubric.criteria) == 5
    total = sum(c.weight for c in rubric.criteria)
    assert abs(total - 1.0) < 1e-6
    # rubric.yaml 의 핵심 threshold 가 우리가 기대한 값인지 확인 (회귀 방지)
    assert rubric.pass_threshold == 7.0
    assert rubric.retry_threshold == 5.0


def test_rubric_yaml_rejects_broken_weights(tmp_path: Path) -> None:
    broken = tmp_path / "broken_rubric.yaml"
    broken.write_text(
        """
version: 1
name: broken
description: broken
aggregate:
  pass_threshold: 7.0
  retry_threshold: 5.0
  fail_threshold: 5.0
  retry_max: 1
criteria:
  - name: a
    weight: 0.5
    label_ko: a
    description: a
    scoring_guide: {"0-10": "x"}
  - name: b
    weight: 0.3   # 합 0.8, 1.0 아님 → 에러
    label_ko: b
    description: b
    scoring_guide: {"0-10": "x"}
reference_cases: []
grading_policy:
  stage1_sift: {model: claude-haiku-4-5, max_tokens: 1000}
  stage2_precision: {model: claude-opus-4-7, max_tokens: 2000}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="weight sum"):
        Rubric.from_yaml(broken)


def test_clear_fail_short_circuits_stage2() -> None:
    """stage1 평균이 fail_threshold - 1.0 미만이면 stage2 호출 안 함."""
    rubric = Rubric.from_yaml(DEFAULT_RUBRIC)
    # 모든 항목 2점 → 가중평균 2.0, fail_threshold(5.0) - 1.0 = 4.0 보다 한참 낮음 → stage2 skip
    sift_resp = _mock_response({
        "criteria": _criteria_payload(2.0, 2.0, 2.0, 2.0, 2.0),
        "weighted_score": 2.0,
        "decision_hint": "fail",
        "dominant_failure": "not_forced",
    })
    client = _mock_client(sift_resp)  # response 1개만 등록

    grader = Grader(rubric=rubric, client=client)
    score = grader.evaluate(_payload())

    assert score.decision is GraderDecision.FAIL
    assert score.stage == "sift"
    assert score.weighted_score == pytest.approx(2.0, abs=1e-3)
    assert client.messages.create.call_count == 1  # stage2 호출 X


def test_borderline_goes_to_precision_and_passes() -> None:
    rubric = Rubric.from_yaml(DEFAULT_RUBRIC)
    # stage1: 평균 6.5 (borderline, fail_threshold-1.0=4.0 보다 큼 → precision 진입)
    sift_resp = _mock_response({
        "criteria": _criteria_payload(7.0, 6.0, 6.0, 7.0, 6.0),
        "weighted_score": 6.55,
        "decision_hint": "retry",
        "dominant_failure": "proxy_strength",
    })
    # stage2: 평균 7.6 (pass)
    precision_resp = _mock_response({
        "criteria": _criteria_payload(8.0, 7.0, 8.0, 8.0, 7.0),
        "weighted_score": 7.65,
        "decision_hint": "pass",
        "dominant_failure": None,
    })
    client = _mock_client(sift_resp, precision_resp)
    grader = Grader(rubric=rubric, client=client)
    score = grader.evaluate(_payload())

    assert score.stage == "precision"
    assert score.decision is GraderDecision.PASS
    assert client.messages.create.call_count == 2


def test_weighted_score_computed_from_scratch_when_model_off() -> None:
    """모델이 weighted_score 를 엉터리로 줘도 grader 가 직접 가중평균 계산."""
    rubric = Rubric.from_yaml(DEFAULT_RUBRIC)
    sift_resp = _mock_response({
        "criteria": _criteria_payload(8.0, 8.0, 8.0, 8.0, 8.0),
        "weighted_score": 999.0,   # 비현실적 → grader 가 무시
        "decision_hint": "pass",
        "dominant_failure": None,
    })
    precision_resp = _mock_response({
        "criteria": _criteria_payload(8.0, 8.0, 8.0, 8.0, 8.0),
        "weighted_score": -5.0,
        "decision_hint": "pass",
        "dominant_failure": None,
    })
    client = _mock_client(sift_resp, precision_resp)
    grader = Grader(rubric=rubric, client=client)
    score = grader.evaluate(_payload())

    # 5개 항목 8.0 + rubric.yaml 가중치 (0.25, 0.20, 0.20, 0.25, 0.10) → 합 8.0
    assert score.weighted_score == pytest.approx(8.0, abs=1e-3)
    assert score.decision is GraderDecision.PASS


def test_raw_json_response_parsed() -> None:
    """``"""
    rubric = Rubric.from_yaml(DEFAULT_RUBRIC)
    # 코드 블록 없이 raw JSON 으로 응답
    payload = {
        "criteria": _criteria_payload(1.0, 1.0, 1.0, 1.0, 1.0),
        "weighted_score": 1.0,
        "decision_hint": "fail",
        "dominant_failure": "not_forced",
    }
    text_block = MagicMock()
    text_block.text = json.dumps(payload)
    resp = MagicMock()
    resp.content = [text_block]
    resp.usage = MagicMock(
        input_tokens=100, output_tokens=50,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    client = _mock_client(resp)

    grader = Grader(rubric=rubric, client=client)
    score = grader.evaluate(_payload())
    assert score.decision is GraderDecision.FAIL


def test_dominant_failure_inferred_when_model_omits_it() -> None:
    rubric = Rubric.from_yaml(DEFAULT_RUBRIC)
    # 명백한 fail (평균 ~2) — stage2 skip 됨. proxy_strength 가 최저.
    sift_resp = _mock_response({
        "criteria": _criteria_payload(3.0, 1.0, 3.0, 3.0, 3.0),
        "weighted_score": 2.6,
        "decision_hint": "fail",
        # dominant_failure 누락
    })
    client = _mock_client(sift_resp)
    grader = Grader(rubric=rubric, client=client)
    score = grader.evaluate(_payload())

    # 모델이 None 으로 줘도 grader 가 최저 점수 항목 (proxy_strength=1.0) 자동 선정
    assert score.dominant_failure == "proxy_strength"


def test_high_score_pass_decision() -> None:
    rubric = Rubric.from_yaml(DEFAULT_RUBRIC)
    # sift 평균 5.4 (fail_threshold-1=4.0 보다 큼 → precision 진입), precision 평균 9.0
    sift_resp = _mock_response({
        "criteria": _criteria_payload(5.4, 5.4, 5.4, 5.4, 5.4),
        "weighted_score": 5.4,
        "decision_hint": "retry",
    })
    precision_resp = _mock_response({
        "criteria": _criteria_payload(9.0, 9.0, 9.0, 9.0, 9.0),
        "weighted_score": 9.0,
        "decision_hint": "pass",
        "dominant_failure": None,
    })
    client = _mock_client(sift_resp, precision_resp)
    grader = Grader(rubric=rubric, client=client)
    score = grader.evaluate(_payload())
    assert score.decision is GraderDecision.PASS
    assert score.dominant_failure is None
