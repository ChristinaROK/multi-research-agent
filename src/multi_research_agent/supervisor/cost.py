"""Cost circuit breaker — hourly / daily / cumulative 4중 안전장치 (핸드오버 §6, §10-4).

cost_tracker 테이블에 기록된 호출만 본다. 메모리 캐시는 짧은 기간만.
누적은 DB에서 sum.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from multi_research_agent.config import get_settings
from multi_research_agent.db.models import CostEntry
from multi_research_agent.supervisor.alerts import alert

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CostStatus:
    last_hour_usd: float
    last_day_usd: float
    cumulative_usd: float
    can_proceed: bool
    reason: str | None


def assess(session: Session) -> CostStatus:
    """현재 사용량 vs 한도 평가. 한도 초과 시 alert 발사."""
    s = get_settings()
    now = dt.datetime.now(dt.UTC)
    hour_ago = now - dt.timedelta(hours=1)
    day_ago = now - dt.timedelta(days=1)

    rows = session.execute(select(CostEntry.cost_usd, CostEntry.created_at)).all()

    def _aware(ts: dt.datetime | None) -> dt.datetime | None:
        # SQLite 는 datetime 을 ISO 문자열로 저장 → SQLAlchemy read 시 naive 로 복원.
        # 모든 비교는 UTC 로 통일.
        if ts is None:
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)

    last_hour = sum(c for c, ts in rows if (a := _aware(ts)) and a >= hour_ago)
    last_day = sum(c for c, ts in rows if (a := _aware(ts)) and a >= day_ago)
    cumulative = sum(c for c, _ in rows)

    reason: str | None = None
    can_proceed = True

    if cumulative >= s.max_cost_usd_cumulative:
        reason = (
            f"누적 한도 초과 ${cumulative:.2f}/${s.max_cost_usd_cumulative}"
        )
        can_proceed = False
        alert(f"누적 비용 한도 초과 — 영구 정지. {reason}", level="urgent")
    elif last_day >= s.max_cost_usd_per_day:
        reason = f"일일 한도 초과 ${last_day:.2f}/${s.max_cost_usd_per_day}"
        can_proceed = False
        alert(f"일일 비용 한도 — 다음 날 새벽까지 정지. {reason}", level="urgent")
    elif last_hour >= s.max_cost_usd_per_hour:
        reason = (
            f"시간당 한도 초과 ${last_hour:.2f}/${s.max_cost_usd_per_hour}"
        )
        can_proceed = False
        alert(f"시간당 비용 한도 — 30분 cooldown. {reason}", level="warn")

    return CostStatus(
        last_hour_usd=last_hour,
        last_day_usd=last_day,
        cumulative_usd=cumulative,
        can_proceed=can_proceed,
        reason=reason,
    )


def record(
    session: Session,
    *,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_usd: float,
    estimated_cost_usd: float | None = None,
    task_ref: str | None = None,
) -> None:
    """비용 1건 기록. 추정 vs 실제 비교는 §8-4."""
    entry = CostEntry(
        agent=agent,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cost_usd=cost_usd,
        estimated_cost_usd=estimated_cost_usd,
        task_ref=task_ref,
    )
    session.add(entry)
    session.flush()

    if estimated_cost_usd is not None:
        diff = abs(cost_usd - estimated_cost_usd)
        rel = diff / max(estimated_cost_usd, 0.001)
        if rel > 0.5:
            log.warning(
                "[%s] 비용 추정 오차 큼: 예상 $%.4f vs 실제 $%.4f (%.0f%%)",
                agent, estimated_cost_usd, cost_usd, rel * 100,
            )
