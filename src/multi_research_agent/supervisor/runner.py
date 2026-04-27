"""Agent runner — Claude Code CLI 헤드리스 호출.

dry-run 모드에선 실제 호출 없이 mock 응답 반환 (핸드오버 §7-1).
prompt 는 stdin (§2-3 핸드오버), --add-dir variadic 함정 회피.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from multi_research_agent.config import get_settings

log = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class AgentResult:
    agent: str
    success: bool
    stdout: str
    stderr: str
    duration_sec: float
    cost_usd: float = 0.0
    estimated_cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    task_ref: str | None = None
    raw_meta: dict[str, Any] | None = None


def _claude_bin() -> str | None:
    return shutil.which("claude")


def run_agent(
    *,
    agent: str,
    prompt: str,
    model: str,
    add_dirs: list[Path] | None = None,
    allowed_tools: list[str] | None = None,
    max_turns: int = 30,
    timeout_sec: int = 1800,
    task_ref: str | None = None,
) -> AgentResult:
    """``claude --print`` 헤드리스 호출. dry_run 모드는 mock."""
    s = get_settings()
    started = dt.datetime.now(dt.UTC)

    if s.run_mode == "dry_run":
        log.info("[dry_run] %s 호출 mock — task=%s, model=%s", agent, task_ref, model)
        mock_payload = {
            "agent": agent,
            "task_ref": task_ref,
            "result": "dry_run",
            "model": model,
        }
        return AgentResult(
            agent=agent,
            success=True,
            stdout=json.dumps(mock_payload),
            stderr="",
            duration_sec=0.0,
            cost_usd=0.0,
            estimated_cost_usd=0.0,
            task_ref=task_ref,
            raw_meta=mock_payload,
        )

    claude = _claude_bin()
    if not claude:
        return AgentResult(
            agent=agent,
            success=False,
            stdout="",
            stderr="claude CLI not found in PATH",
            duration_sec=0.0,
            task_ref=task_ref,
        )

    cwd = s.agents_dir / agent
    if not cwd.exists():
        return AgentResult(
            agent=agent,
            success=False,
            stdout="",
            stderr=f"agent dir missing: {cwd}",
            duration_sec=0.0,
            task_ref=task_ref,
        )

    cmd: list[str] = [
        claude,
        "--print",
        "--model", model,
        "--max-turns", str(max_turns),
        "--dangerously-skip-permissions",
        "--output-format", "json",
        # cwd/env/git 같은 per-machine 섹션을 system prompt 에서 제거 →
        # 같은 에이전트 + 같은 모델 호출 시 cache hit ↑ (비용 -50% 효과적).
        "--exclude-dynamic-system-prompt-sections",
    ]
    for d in add_dirs or []:
        cmd += ["--add-dir", str(d)]
    if allowed_tools:
        cmd += ["--allowed-tools", ",".join(allowed_tools)]

    log.info("[%s] CLI 호출 시작 task=%s model=%s", agent, task_ref, model)
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return AgentResult(
            agent=agent,
            success=False,
            stdout=exc.stdout or "",
            stderr=f"timeout after {timeout_sec}s",
            duration_sec=timeout_sec,
            task_ref=task_ref,
        )

    duration = (dt.datetime.now(dt.UTC) - started).total_seconds()
    meta = _parse_meta(proc.stdout)

    # 디버그: 전체 stdout/stderr 보존 — 도구 호출 trace 추적용
    debug_dir = s.logs_dir / "agent-runs"
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe_ref = (task_ref or f"{agent}-{started:%Y%m%d%H%M%S}").replace("/", "_")
    (debug_dir / f"{safe_ref}.stdout.txt").write_text(proc.stdout or "", encoding="utf-8")
    if proc.stderr:
        (debug_dir / f"{safe_ref}.stderr.txt").write_text(proc.stderr, encoding="utf-8")

    return AgentResult(
        agent=agent,
        success=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_sec=duration,
        cost_usd=float(meta.get("total_cost_usd") or 0.0),
        input_tokens=int(meta.get("input_tokens") or 0),
        output_tokens=int(meta.get("output_tokens") or 0),
        cache_read_tokens=int(meta.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(meta.get("cache_creation_input_tokens") or 0),
        task_ref=task_ref,
        raw_meta=meta,
    )


def _parse_meta(stdout: str) -> dict[str, Any]:
    """Claude Code --output-format json 응답에서 사용량/비용 메타 추출.

    응답 스키마는 버전마다 변할 수 있어 best-effort.
    """
    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {}
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage") or {}
    return {
        "total_cost_usd": payload.get("total_cost_usd") or payload.get("cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "session_id": payload.get("session_id"),
    }
