"""Telegram 알림 — 등급별 채널 분리 (info/warn/urgent). 핸드오버 §6-2.

- 채널 미설정 시 stdout 로 fallback (dry-run 친화).
- HTTP 실패는 silent — 알림 자체가 전체 시스템을 막으면 안 됨.
"""

from __future__ import annotations

import logging
from typing import Literal

import requests

from multi_research_agent.config import get_settings

log = logging.getLogger(__name__)

Level = Literal["info", "warn", "urgent"]
_EMOJI: dict[Level, str] = {"info": "📊", "warn": "⚠️", "urgent": "🚨"}


def alert(message: str, level: Level = "info") -> None:
    s = get_settings()
    chat_id = {
        "info": s.telegram_chat_info,
        "warn": s.telegram_chat_warn,
        "urgent": s.telegram_chat_urgent,
    }[level]
    body = f"{_EMOJI[level]} {message}"

    if not s.telegram_bot_token or not chat_id:
        log.info("[alert/%s] %s", level, message)
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": body, "parse_mode": "Markdown"},
            timeout=10,
        )
    except requests.RequestException as exc:  # pragma: no cover
        log.warning("Telegram 송신 실패: %s", exc)
