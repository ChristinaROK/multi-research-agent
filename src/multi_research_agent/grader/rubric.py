"""Rubric grader 코어.

configs/rubric.yaml 을 로딩해 finding 1건을 5개 항목으로 채점한다.

설계 원칙:
- **2단계 호출** (sift → precision) — Haiku 가 명백한 실패를 1차로 거르고, borderline 만
  Opus 로 정밀. 비용 5~10배 절감.
- **Prompt caching** — rubric + reference_cases 는 매 호출 동일하므로 cache_control 적용.
- **마이그레이션 친화 인터페이스** — Phase 3 에서 Managed Agents 의 ``beta.outcomes`` 로 갈아끼울
  때 ``Grader.evaluate(payload) -> RubricScore`` 시그니처만 유지하면 caller (cycle.py 등) 는
  무수정.
- **결정 규칙은 yaml 의 aggregate 가 단일 source** — 임계값을 hard-code 하지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)


# ─── 데이터 모델 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RubricCriterion:
    """yaml 의 criteria[i] 1건."""

    name: str
    weight: float
    label_ko: str
    description: str
    scoring_guide: dict[str, str]
    failure_anchor: str = ""


@dataclass(frozen=True)
class Rubric:
    """configs/rubric.yaml 전체."""

    version: int
    name: str
    description: str
    pass_threshold: float
    retry_threshold: float
    fail_threshold: float
    retry_max: int
    criteria: tuple[RubricCriterion, ...]
    reference_cases: tuple[dict[str, Any], ...]
    stage1_model: str
    stage2_model: str
    stage1_max_tokens: int
    stage2_max_tokens: int
    prompt_cache: bool

    @classmethod
    def from_yaml(cls, path: Path) -> Rubric:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        agg = raw["aggregate"]
        crits = tuple(
            RubricCriterion(
                name=c["name"],
                weight=float(c["weight"]),
                label_ko=c["label_ko"],
                description=c["description"].strip(),
                scoring_guide={k: v.strip() for k, v in c["scoring_guide"].items()},
                failure_anchor=(c.get("failure_anchor") or "").strip(),
            )
            for c in raw["criteria"]
        )
        # 가중치 합 검증 — yaml 변경 시 합이 1.0 에서 벗어나면 즉시 알림
        total = sum(c.weight for c in crits)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"criteria weight sum must equal 1.0, got {total}")

        policy = raw["grading_policy"]
        return cls(
            version=int(raw["version"]),
            name=str(raw["name"]),
            description=str(raw["description"]).strip(),
            pass_threshold=float(agg["pass_threshold"]),
            retry_threshold=float(agg["retry_threshold"]),
            fail_threshold=float(agg["fail_threshold"]),
            retry_max=int(agg["retry_max"]),
            criteria=crits,
            reference_cases=tuple(raw.get("reference_cases") or ()),
            stage1_model=str(policy["stage1_sift"]["model"]),
            stage2_model=str(policy["stage2_precision"]["model"]),
            stage1_max_tokens=int(policy["stage1_sift"]["max_tokens"]),
            stage2_max_tokens=int(policy["stage2_precision"]["max_tokens"]),
            prompt_cache=bool(policy.get("prompt_cache", True)),
        )


class GraderDecision(StrEnum):
    PASS = "pass"
    RETRY = "retry"
    FAIL = "fail"


@dataclass(frozen=True)
class CriterionScore:
    name: str
    score: float          # 0~10
    reasoning: str


@dataclass(frozen=True)
class RubricScore:
    """5개 항목 채점 + 결정 + 메타.

    cycle.py 가 이 객체를 받아 hub.db 의 rubric_scores 테이블에 INSERT.
    """

    criterion_scores: tuple[CriterionScore, ...]
    weighted_score: float
    decision: GraderDecision
    dominant_failure: str | None       # 최저 점수 항목 name (디버그/패턴 추출용)
    stage: Literal["sift", "precision"]
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    raw_response: str                  # 파싱 실패 시 디버그


@dataclass(frozen=True)
class FindingPayload:
    """grader 에 넘기는 1건 input. cycle.py 가 hub.db 에서 끌어와 조립."""

    finding_id: str
    hypothesis_id: str
    factor_category: str               # esg / media / advertising / governance
    factor_name: str
    causal_claim: str
    brief_text: str                    # 01_hypothesis_brief.md 내용
    verdict_text: str                  # 04_verdict.md 내용
    finding_text: str                  # 05_finding.md 내용
    analysis_metrics: dict[str, Any] = field(default_factory=dict)


# ─── 프롬프트 빌더 ────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """당신은 SK 지주사 외부 평판/브랜드 분석 finding 을 객관적으로 채점하는 grader 입니다.

핵심 원칙:
1. 첨부된 rubric 의 scoring_guide 의 anchor 와 비교해서 점수를 정합니다. 주관적 인상이 아니라
   anchor 매칭으로.
2. 독립 관찰자라고 가정합니다 — SK 직원 동료가 봐도 동의할 만한가.
3. 친절보다 정확. borderline 은 borderline 으로 채점하고, 명백한 실패는 명백히 낮게.
4. failure_anchor 와 패턴이 일치하면 그 항목은 5점 미만으로 강하게 감점.
5. 응답은 반드시 지정된 JSON 스키마. 그 외 텍스트는 절대 출력하지 않습니다.
"""


def _format_criteria_block(rubric: Rubric) -> str:
    """criteria 5개를 grader prompt 에 inject."""
    parts: list[str] = []
    for c in rubric.criteria:
        guide = "\n".join(f"      {band}: {anchor}" for band, anchor in c.scoring_guide.items())
        anchor = f"\n  failure_anchor:\n    {c.failure_anchor}" if c.failure_anchor else ""
        parts.append(
            f"- {c.name} ({c.label_ko}, weight={c.weight}):\n"
            f"  description: {c.description}\n"
            f"  scoring_guide:\n{guide}{anchor}"
        )
    return "\n\n".join(parts)


def _format_reference_block(rubric: Rubric) -> str:
    if not rubric.reference_cases:
        return "(reference_cases 없음)"
    parts: list[str] = []
    for case in rubric.reference_cases:
        low, high = case.get("expected_score_range", [0, 10])
        parts.append(
            f"- case_id: {case.get('case_id')}\n"
            f"  summary: {case.get('summary', '').strip()}\n"
            f"  expected_score_range: [{low}, {high}]\n"
            f"  why: {case.get('why', '').strip()}"
        )
    return "\n\n".join(parts)


def _build_user_message(rubric: Rubric, payload: FindingPayload, *, include_reference: bool) -> str:
    """User 메시지 — finding 본문 + 응답 스키마."""
    schema_names = [c.name for c in rubric.criteria]
    schema_obj = {
        "criteria": {n: {"score": "<number 0-10>", "reasoning": "<2-4문장>"} for n in schema_names},
        "weighted_score": "<number, criteria.score 의 가중평균>",
        "decision_hint": (
            f"<pass | retry | fail — pass_threshold={rubric.pass_threshold}, "
            f"retry_threshold={rubric.retry_threshold}>"
        ),
        "dominant_failure": "<criterion name | null — 최저 점수 항목>",
    }
    schema_str = json.dumps(schema_obj, ensure_ascii=False, indent=2)

    ref_block = ""
    if include_reference:
        ref_block = (
            "\n## Calibration (reference cases — 이 점수 분포에 맞춰 채점하세요)\n"
            f"{_format_reference_block(rubric)}\n"
        )

    return (
        "## 채점 대상 finding\n"
        f"finding_id: {payload.finding_id}\n"
        f"hypothesis_id: {payload.hypothesis_id}\n"
        f"factor_category: {payload.factor_category}\n"
        f"factor_name: {payload.factor_name}\n"
        f"causal_claim: {payload.causal_claim}\n"
        f"analysis_metrics: {json.dumps(payload.analysis_metrics, ensure_ascii=False)}\n\n"
        "### brief (가설 작성 단계)\n"
        f"{payload.brief_text.strip()}\n\n"
        "### verdict (통계 검증 결과)\n"
        f"{payload.verdict_text.strip()}\n\n"
        "### finding (최종 요약)\n"
        f"{payload.finding_text.strip()}\n"
        f"{ref_block}\n"
        "## 응답 형식 (반드시 이 스키마 그대로, JSON 외 텍스트 출력 금지)\n"
        f"```json\n{schema_str}\n```"
    )


# ─── Grader 본체 ─────────────────────────────────────────────────────────


# stage1 (sift) 평균 추정이 이 값보다 낮으면 stage2 skip — yaml 의 fail_threshold 와 같이 움직임.
_SIFT_PRECISION_FLOOR_GAP = 1.0   # sift 점수가 fail_threshold 보다 1.0 이상 낮으면 명백한 fail


class Grader:
    """5개 항목 rubric 기반 finding 채점기.

    Phase 1 에서는 Anthropic SDK 를 직접 호출한다. ``client`` 인자는 테스트에서 주입 가능
    (Mock 으로 단위 테스트). 프로덕션에서는 None 으로 두면 모듈 load 시 lazy import.

    Phase 3 에서 Managed Agents 의 beta.outcomes 로 마이그레이션 시:
    ``_call_stage(...)`` 메서드 1곳만 hosted endpoint 호출로 교체. 외부 시그니처
    (``evaluate(payload) -> RubricScore``) 는 그대로.
    """

    def __init__(
        self,
        rubric: Rubric | None = None,
        *,
        rubric_path: Path | None = None,
        client: Any = None,                      # anthropic.Anthropic (lazy)
        skip_stage2_if_clear_fail: bool = True,
    ) -> None:
        if rubric is None:
            if rubric_path is None:
                # default: 프로젝트 configs/rubric.yaml
                from multi_research_agent.config import get_settings

                rubric_path = get_settings().project_root / "configs" / "rubric.yaml"
            rubric = Rubric.from_yaml(rubric_path)
        self.rubric = rubric
        self._client = client
        self.skip_stage2_if_clear_fail = skip_stage2_if_clear_fail

    # ── 외부 인터페이스 ────────────────────────────────────────────────
    def evaluate(self, payload: FindingPayload) -> RubricScore:
        """1건 finding 채점. sift → (필요 시) precision 2단계."""
        sift_score = self._call_stage(
            payload,
            stage="sift",
            model=self.rubric.stage1_model,
            max_tokens=self.rubric.stage1_max_tokens,
            include_reference=False,
        )

        # 명백한 실패 (sift 평균 점수가 fail_threshold 보다 GAP 이상 낮으면 stage2 skip)
        if (
            self.skip_stage2_if_clear_fail
            and sift_score.weighted_score < (self.rubric.fail_threshold - _SIFT_PRECISION_FLOOR_GAP)
        ):
            logger.info(
                "grader: %s sift=%.2f < %.2f, stage2 skip (clear fail)",
                payload.finding_id, sift_score.weighted_score,
                self.rubric.fail_threshold - _SIFT_PRECISION_FLOOR_GAP,
            )
            return sift_score

        precision_score = self._call_stage(
            payload,
            stage="precision",
            model=self.rubric.stage2_model,
            max_tokens=self.rubric.stage2_max_tokens,
            include_reference=True,
        )
        logger.info(
            "grader: %s sift=%.2f -> precision=%.2f decision=%s",
            payload.finding_id,
            sift_score.weighted_score,
            precision_score.weighted_score,
            precision_score.decision.value,
        )
        return precision_score

    # ── 내부: 1단계 호출 ──────────────────────────────────────────────
    def _call_stage(
        self,
        payload: FindingPayload,
        *,
        stage: Literal["sift", "precision"],
        model: str,
        max_tokens: int,
        include_reference: bool,
    ) -> RubricScore:
        client = self._ensure_client()
        system_blocks = self._system_blocks(include_reference=include_reference)
        user_message = _build_user_message(self.rubric, payload, include_reference=include_reference)

        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = self._extract_text(response)
        parsed = self._parse_response(raw_text)

        usage = getattr(response, "usage", None)
        return self._build_score(
            parsed=parsed,
            raw_text=raw_text,
            stage=stage,
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) if usage else 0,
        )

    # ── system message — cache 활용 ──────────────────────────────────
    def _system_blocks(self, *, include_reference: bool) -> list[dict[str, Any]]:
        """system 을 여러 블록으로 나눠 prompt cache (block 단위로 cache_control 지정)."""
        rubric_text = (
            "## Rubric (이 정의에 맞춰 채점)\n\n"
            f"{_format_criteria_block(self.rubric)}\n\n"
            f"## Aggregate 결정 규칙\n"
            f"- pass_threshold: {self.rubric.pass_threshold} (가중평균 ≥ 이면 pass)\n"
            f"- retry_threshold: {self.rubric.retry_threshold} (이상이면 retry, 미만이면 fail)\n"
            f"- fail_threshold: {self.rubric.fail_threshold}\n"
        )
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": _SYSTEM_PROMPT},
            {
                "type": "text",
                "text": rubric_text,
                # rubric 본문은 매 호출 동일 → cache
                **(
                    {"cache_control": {"type": "ephemeral"}}
                    if self.rubric.prompt_cache
                    else {}
                ),
            },
        ]
        if include_reference:
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        "## Reference cases (calibration)\n\n"
                        f"{_format_reference_block(self.rubric)}"
                    ),
                    **(
                        {"cache_control": {"type": "ephemeral"}}
                        if self.rubric.prompt_cache
                        else {}
                    ),
                }
            )
        return blocks

    # ── client lazy import ───────────────────────────────────────────
    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic  # 지연 import — 테스트에서 mock 주입이 우선

            self._client = anthropic.Anthropic()
        return self._client

    # ── 응답 파싱 ────────────────────────────────────────────────────
    @staticmethod
    def _extract_text(response: Any) -> str:
        """anthropic.messages.create 응답에서 text content 추출."""
        content = getattr(response, "content", None)
        if not content:
            return ""
        parts: list[str] = []
        for block in content:
            t = getattr(block, "text", None)
            if t:
                parts.append(t)
        return "".join(parts)

    @staticmethod
    def _parse_response(text: str) -> dict[str, Any]:
        """JSON 블록 추출 — ```json ... ``` 또는 raw JSON 둘 다 허용."""
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = m.group(1) if m else text.strip()
        # raw 가 JSON 이 아니면 첫 { 부터 마지막 } 까지 시도
        if not candidate.startswith("{"):
            first = candidate.find("{")
            last = candidate.rfind("}")
            if first >= 0 and last > first:
                candidate = candidate[first : last + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            raise ValueError(f"grader: failed to parse JSON response: {e!r}\nraw={text[:500]!r}") from e

    # ── 점수 -> 결정 ────────────────────────────────────────────────
    def _decision_from_score(self, weighted: float) -> GraderDecision:
        if weighted >= self.rubric.pass_threshold:
            return GraderDecision.PASS
        if weighted >= self.rubric.retry_threshold:
            return GraderDecision.RETRY
        return GraderDecision.FAIL

    def _build_score(
        self,
        *,
        parsed: dict[str, Any],
        raw_text: str,
        stage: Literal["sift", "precision"],
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
    ) -> RubricScore:
        criteria_raw = parsed.get("criteria") or {}
        scores: list[CriterionScore] = []
        for c in self.rubric.criteria:
            cell = criteria_raw.get(c.name) or {}
            score = float(cell.get("score", 0.0))
            reasoning = str(cell.get("reasoning", "")).strip()
            scores.append(CriterionScore(name=c.name, score=score, reasoning=reasoning))

        weighted = sum(s.score * c.weight for s, c in zip(scores, self.rubric.criteria, strict=True))
        # 모델이 계산해준 weighted_score 가 합리적이면 그걸 사용 (반올림 어긋남 대비)
        model_weighted = parsed.get("weighted_score")
        if isinstance(model_weighted, int | float) and abs(float(model_weighted) - weighted) < 0.5:
            weighted = float(model_weighted)

        # dominant_failure — 모델이 지정 안 했으면 우리가 최저 항목 계산
        dominant = parsed.get("dominant_failure")
        if not isinstance(dominant, str) or not dominant.strip():
            min_score = min(scores, key=lambda s: s.score)
            dominant = min_score.name if min_score.score < self.rubric.retry_threshold else None
        elif dominant.strip().lower() in ("null", "none"):
            dominant = None

        decision = self._decision_from_score(weighted)
        return RubricScore(
            criterion_scores=tuple(scores),
            weighted_score=round(weighted, 3),
            decision=decision,
            dominant_failure=dominant,
            stage=stage,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            raw_response=raw_text,
        )
