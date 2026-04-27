"""환경변수 로드 + 설정 객체. .env 가 있으면 읽고, 없으면 OS env."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


class Settings(BaseSettings):
    """모든 운영 파라미터의 단일 입구. 코드에서 ``get_settings()`` 로 접근."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # API
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # 모델 분담
    model_supervisor: str = Field(default="claude-haiku-4-5", alias="MODEL_SUPERVISOR")
    model_hypothesis: str = Field(default="claude-opus-4-7", alias="MODEL_HYPOTHESIS")
    model_collector: str = Field(default="claude-sonnet-4-6", alias="MODEL_COLLECTOR")
    model_analyzer: str = Field(default="claude-opus-4-7", alias="MODEL_ANALYZER")
    model_verifier: str = Field(default="claude-sonnet-4-6", alias="MODEL_VERIFIER")
    model_monitor: str = Field(default="claude-haiku-4-5", alias="MODEL_MONITOR")

    # Telegram
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_info: str | None = Field(default=None, alias="TELEGRAM_CHAT_INFO")
    telegram_chat_warn: str | None = Field(default=None, alias="TELEGRAM_CHAT_WARN")
    telegram_chat_urgent: str | None = Field(default=None, alias="TELEGRAM_CHAT_URGENT")

    # Cost / Time circuit breaker
    max_cost_usd_per_hour: float = Field(default=10.0, alias="MAX_COST_USD_PER_HOUR")
    max_cost_usd_per_day: float = Field(default=50.0, alias="MAX_COST_USD_PER_DAY")
    max_cost_usd_cumulative: float = Field(default=200.0, alias="MAX_COST_USD_CUMULATIVE")
    max_run_hours: float = Field(default=8.0, alias="MAX_RUN_HOURS")

    # Diminishing returns
    diminishing_returns_window: int = Field(default=10, alias="DIMINISHING_RETURNS_WINDOW")
    diminishing_returns_reject_threshold: int = Field(
        default=8, alias="DIMINISHING_RETURNS_REJECT_THRESHOLD"
    )

    # Hypothesis 폭발 방지
    max_hypothesis_depth: int = Field(default=3, alias="MAX_HYPOTHESIS_DEPTH")
    max_new_hypotheses_per_cycle: int = Field(default=5, alias="MAX_NEW_HYPOTHESES_PER_CYCLE")

    # Deadlock 방지
    max_task_age_hours: float = Field(default=4.0, alias="MAX_TASK_AGE_HOURS")

    # Polling
    poll_interval_sec: int = Field(default=30, alias="POLL_INTERVAL_SEC")

    # 실행 모드
    run_mode: Literal["dry_run", "live"] = Field(default="dry_run", alias="RUN_MODE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # 경로
    project_root: Path = PROJECT_ROOT
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    agents_dir: Path = PROJECT_ROOT / "agents"
    reference_dir: Path = PROJECT_ROOT / "reference"
    logs_dir: Path = PROJECT_ROOT / "workspace" / "logs"
    stop_file: Path = PROJECT_ROOT / "workspace" / "STOP"
    status_file: Path = PROJECT_ROOT / "workspace" / "STATUS.md"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return _settings
