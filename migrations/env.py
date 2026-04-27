"""Alembic env — multi-research-agent hub.db.

DB URL은 환경변수 ``HUB_DB_URL`` 또는 ``HUB_DB_PATH`` 에서 읽고,
sqlalchemy.url alembic.ini 값은 그저 placeholder.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from multi_research_agent.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    if url := os.environ.get("HUB_DB_URL"):
        return url
    raw_path = os.environ.get("HUB_DB_PATH", "workspace/hub.db")
    db_path = Path(raw_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config_section = config.get_section(config.config_ini_section, {}) or {}
    config_section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        config_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # SQLite ALTER 한계 → batch 모드로 모든 마이그레이션 처리
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
