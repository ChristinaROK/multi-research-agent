#!/usr/bin/env python3
"""Supervisor entry point.

사용법:
    uv run python scripts/supervisor.py                 # 무한 polling
    uv run python scripts/supervisor.py --once          # 1 사이클 후 종료 (라이브 첫 호출 시)
    RUN_MODE=dry_run uv run python scripts/supervisor.py
    RUN_MODE=live    uv run python scripts/supervisor.py --once
"""

from __future__ import annotations

import argparse
import sys

from multi_research_agent.supervisor.main import run

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="1 사이클 후 종료")
    args = parser.parse_args()
    sys.exit(run(once=args.once))
