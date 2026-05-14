#!/usr/bin/env python3
"""Weekly Review — Dreaming 의 DIY 대체.

매주 금요일 한 번 (또는 수동) 실행. 지난 7일간 hub.db 에 쌓인 verdict + rubric_score
패턴을 추출해 `workspace/learnings/weekly-{YYYY-MM-DD}.md` 로 저장한다.

SK 담당자가 30분 검토 후 승인한 항목만 루트 CLAUDE.md 의 'core_findings' 섹션에
수동으로 옮긴다 (auto-update 금지 — echo chamber 회피).

사용법:
    uv run python scripts/weekly_review.py
    uv run python scripts/weekly_review.py --days 14         # 기간 변경
    uv run python scripts/weekly_review.py --dry-run         # SQL/통계만, LLM 호출 X

Phase 3 마이그레이션: Anthropic Managed Agents 의 Dreaming 이 승인되면 본 스크립트
삭제 후 dreaming-config.yaml 로 이전. SK 담당자 검토 단계는 그대로 유지.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 패키지 import 를 위해 src 디렉토리를 path 에 추가 (uv run 컨텍스트)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str((HERE.parent / "src").resolve()))

from sqlalchemy import text  # noqa: E402

from multi_research_agent.config import get_settings  # noqa: E402
from multi_research_agent.db import get_session  # noqa: E402

logger = logging.getLogger("weekly_review")


# ─── 통계 추출 ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class WeeklyStats:
    period_start: dt.datetime
    period_end: dt.datetime
    total_verdicts: int
    decision_counts: dict[str, int]                    # significant/rejected/needs_refinement
    rubric_total: int
    rubric_decision_counts: dict[str, int]             # pass/retry/fail
    dominant_failures: list[tuple[str, int]]           # 항목별 빈도 desc
    factor_pass_rate: dict[str, dict[str, float]]      # factor -> {pass, total, rate}
    rounds_completed: list[str]
    findings_promoted: list[dict[str, Any]]            # rubric_pass=True
    cost_total_usd: float


def collect_stats(days: int) -> WeeklyStats:
    end = dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(days=days)

    with get_session() as session:
        verdicts = session.execute(text(
            """SELECT decision, COUNT(*) FROM verdicts
               WHERE created_at >= :start GROUP BY decision"""
        ), {"start": start}).all()
        decision_counts = {row[0]: row[1] for row in verdicts}

        rubrics = session.execute(text(
            """SELECT decision, COUNT(*) FROM rubric_scores
               WHERE created_at >= :start AND stage='precision'
               GROUP BY decision"""
        ), {"start": start}).all()
        rubric_decision_counts = {row[0]: row[1] for row in rubrics}

        failures = session.execute(text(
            """SELECT dominant_failure, COUNT(*) FROM rubric_scores
               WHERE created_at >= :start AND stage='precision'
                     AND dominant_failure IS NOT NULL
               GROUP BY dominant_failure ORDER BY 2 DESC"""
        ), {"start": start}).all()
        dominant_failures = [(row[0], row[1]) for row in failures]

        # factor 별 통과율
        factor_rows = session.execute(text(
            """SELECT h.factor_category, rs.decision, COUNT(*)
               FROM rubric_scores rs
               JOIN hypotheses h ON rs.hypothesis_id = h.hypothesis_id
               WHERE rs.created_at >= :start AND rs.stage='precision'
               GROUP BY h.factor_category, rs.decision"""
        ), {"start": start}).all()
        bucket: dict[str, Counter[str]] = defaultdict(Counter)
        for factor, decision, count in factor_rows:
            bucket[factor][decision] += count
        factor_pass_rate: dict[str, dict[str, float]] = {}
        for factor, counts in bucket.items():
            total = sum(counts.values())
            factor_pass_rate[factor] = {
                "pass": counts.get("pass", 0),
                "retry": counts.get("retry", 0),
                "fail": counts.get("fail", 0),
                "total": total,
                "rate": counts.get("pass", 0) / total if total else 0.0,
            }

        rounds = session.execute(text(
            """SELECT DISTINCT round_id FROM hypotheses
               WHERE round_id IS NOT NULL AND updated_at >= :start
               ORDER BY round_id DESC"""
        ), {"start": start}).all()
        rounds_completed = [r[0] for r in rounds if r[0]]

        promoted = session.execute(text(
            """SELECT f.finding_id, f.hypothesis_id, h.factor_category, h.factor_name,
                      f.summary, f.significance
               FROM findings f JOIN hypotheses h ON f.hypothesis_id = h.hypothesis_id
               WHERE f.rubric_pass = 1 AND f.created_at >= :start"""
        ), {"start": start}).all()
        findings_promoted = [
            {
                "finding_id": r[0],
                "hypothesis_id": r[1],
                "factor_category": r[2],
                "factor_name": r[3],
                "summary": r[4],
                "significance": r[5],
            }
            for r in promoted
        ]

        cost_row = session.execute(text(
            """SELECT COALESCE(SUM(cost_usd), 0) FROM cost_tracker
               WHERE created_at >= :start"""
        ), {"start": start}).first()
        cost_total = float(cost_row[0]) if cost_row else 0.0

    return WeeklyStats(
        period_start=start,
        period_end=end,
        total_verdicts=sum(decision_counts.values()),
        decision_counts=decision_counts,
        rubric_total=sum(rubric_decision_counts.values()),
        rubric_decision_counts=rubric_decision_counts,
        dominant_failures=dominant_failures,
        factor_pass_rate=factor_pass_rate,
        rounds_completed=rounds_completed,
        findings_promoted=findings_promoted,
        cost_total_usd=cost_total,
    )


# ─── LLM 호출 (Anthropic SDK 직접) ──────────────────────────────────────


_SYSTEM_PROMPT = """당신은 SK 지주사 외부 평판/브랜드 분석 프로젝트의 메타 리뷰어입니다.
지난 한 주 hub.db 의 통계를 보고 다음 4가지를 추출하세요:

1. 반복적으로 통과한 가설의 공통 메커니즘 (rubric_pass=True 인 finding 들에서)
2. 반복적으로 실패한 rubric 항목과 그 원인 (dominant_failures 빈도가 높은 항목)
3. ESG/미디어/광고/거버넌스 4개 채널 중 가장 강한 신호 (factor_pass_rate 비교)
4. 다음 주 우선 탐색해야 할 영역

응답은 다음 3개 섹션의 markdown:
- ## A. 패턴 추출 (1번과 2번)
- ## B. 채널 비교 (3번, 표 형식)
- ## C. 다음 주 권고 (4번, 3~5개 bullet)

핵심 원칙:
- 통계가 약하면 "약함" 으로 명시. 추측 강요 금지.
- 사람이 30분 안에 검토할 수 있는 분량 (전체 4~6KB).
- SK 담당자 승인 전엔 어떤 결론도 final 이 아님 — 반드시 "초안" 톤.
"""


def call_reviewer(stats: WeeklyStats, *, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    user_msg = (
        "## 입력 통계\n\n"
        f"기간: {stats.period_start.isoformat()} ~ {stats.period_end.isoformat()}\n"
        f"verdict 합계: {stats.total_verdicts} {stats.decision_counts}\n"
        f"rubric precision 채점 합계: {stats.rubric_total} {stats.rubric_decision_counts}\n\n"
        "### dominant_failure 빈도 (상위)\n"
        f"{json.dumps(stats.dominant_failures, ensure_ascii=False, indent=2)}\n\n"
        "### factor 별 rubric 통과율\n"
        f"{json.dumps(stats.factor_pass_rate, ensure_ascii=False, indent=2)}\n\n"
        "### 이번 주 완료/진행 라운드\n"
        f"{stats.rounds_completed}\n\n"
        "### rubric_pass=True 로 승격된 finding\n"
        f"{json.dumps(stats.findings_promoted, ensure_ascii=False, indent=2)}\n\n"
        f"### 비용 누적 (이번 주): ${stats.cost_total_usd:.2f}\n\n"
        "위 통계만으로 §A~§C 작성. 추가 SQL 호출 없음."
    )

    response = client.messages.create(
        model=model,
        max_tokens=4500,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(block.text for block in response.content if getattr(block, "text", None))


# ─── 출력 파일 작성 ──────────────────────────────────────────────────────


def render_markdown(stats: WeeklyStats, reviewer_body: str) -> str:
    factor_table = (
        "| factor | pass | retry | fail | total | pass_rate |\n"
        "|--------|------|-------|------|-------|-----------|\n"
    )
    for factor, row in sorted(stats.factor_pass_rate.items()):
        factor_table += (
            f"| {factor} | {row['pass']} | {row['retry']} | {row['fail']} "
            f"| {row['total']} | {row['rate']:.0%} |\n"
        )
    if not stats.factor_pass_rate:
        factor_table += "| _(없음)_ | 0 | 0 | 0 | 0 | 0% |\n"

    failure_table = "| dominant_failure | count |\n|---|---|\n"
    for name, count in stats.dominant_failures[:10]:
        failure_table += f"| {name} | {count} |\n"
    if not stats.dominant_failures:
        failure_table += "| _(없음)_ | 0 |\n"

    return (
        f"# Weekly Review — {stats.period_end.strftime('%Y-%m-%d')}\n\n"
        f"> 기간: `{stats.period_start.strftime('%Y-%m-%d')}` ~ `{stats.period_end.strftime('%Y-%m-%d')}`\n"
        f"> 라운드: {stats.rounds_completed or '_(없음)_'}\n"
        f"> 비용: ${stats.cost_total_usd:.2f}\n\n"
        "## 1. 통계 요약 (auto-extract)\n\n"
        f"- verdict: total={stats.total_verdicts}, "
        f"significant={stats.decision_counts.get('significant', 0)}, "
        f"rejected={stats.decision_counts.get('rejected', 0)}, "
        f"needs_refinement={stats.decision_counts.get('needs_refinement', 0)}\n"
        f"- rubric (precision): total={stats.rubric_total}, "
        f"pass={stats.rubric_decision_counts.get('pass', 0)}, "
        f"retry={stats.rubric_decision_counts.get('retry', 0)}, "
        f"fail={stats.rubric_decision_counts.get('fail', 0)}\n"
        f"- 승격된 finding (rubric_pass=True): {len(stats.findings_promoted)}건\n\n"
        f"### factor 별 통과율\n\n{factor_table}\n"
        f"### dominant_failure 분포\n\n{failure_table}\n"
        "## 2. 메타 리뷰어 초안 (LLM)\n\n"
        f"{reviewer_body.strip()}\n\n"
        "---\n\n"
        "## 3. SK 담당자 검토 체크리스트 (수동)\n\n"
        "- [ ] §1 통계가 직관과 일치하는가\n"
        "- [ ] §2-A 의 통과 메커니즘이 합리적인가 (echo chamber 의심 X)\n"
        "- [ ] §2-B 채널 비교에서 정책적 함의가 있는가\n"
        "- [ ] §2-C 다음 주 권고 중 채택할 항목 표시 후 CLAUDE.md core_findings 섹션에 수동 반영\n"
        "- [ ] 거부한 권고는 `workspace/learnings/rejected/` 에 이유와 함께 보관\n"
    )


# ─── CLI ────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="회고 기간 (일)")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 통계만 출력")
    parser.add_argument("--model", default=None, help="reviewer 모델 (기본 MODEL_HYPOTHESIS)")
    parser.add_argument(
        "--out", type=Path, default=None,
        help="출력 경로 (기본 workspace/learnings/weekly-{date}.md)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    s = get_settings()

    stats = collect_stats(args.days)
    logger.info(
        "stats: verdicts=%d rubric=%d findings_promoted=%d rounds=%s",
        stats.total_verdicts, stats.rubric_total, len(stats.findings_promoted), stats.rounds_completed,
    )

    if args.dry_run:
        print(json.dumps({
            "period_start": stats.period_start.isoformat(),
            "period_end": stats.period_end.isoformat(),
            "decision_counts": stats.decision_counts,
            "rubric_decision_counts": stats.rubric_decision_counts,
            "dominant_failures": stats.dominant_failures,
            "factor_pass_rate": stats.factor_pass_rate,
            "rounds_completed": stats.rounds_completed,
            "findings_promoted_count": len(stats.findings_promoted),
            "cost_total_usd": stats.cost_total_usd,
        }, ensure_ascii=False, indent=2))
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY 미설정 — --dry-run 으로 통계만 확인하거나 .env 로딩 확인")
        return 2

    model = args.model or s.model_hypothesis
    body = call_reviewer(stats, model=model)
    out_path = args.out or (
        s.workspace_dir / "learnings" / f"weekly-{stats.period_end.strftime('%Y-%m-%d')}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(stats, body), encoding="utf-8")
    logger.info("작성 완료: %s", out_path)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
