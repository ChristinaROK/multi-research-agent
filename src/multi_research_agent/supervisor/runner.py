"""Agent runner — Claude Agent SDK (Python) 헤드리스 호출.

이전 버전은 ``subprocess.run(['claude', '--print', ...])`` + JSON 마지막 줄 파싱이었으나,
v0.2 부터 ``claude-agent-sdk`` Python 패키지로 교체. 인터페이스 (``run_agent``, ``AgentResult``)
는 그대로 → cycle.py / orchestrator/lead.py / monitor.py 호출부 무수정.

설계 결정:
- **SDK 가 CLI 를 번들** — PATH 의 ``claude`` 바이너리에 의존하지 않음 (v0.1.81 alpha 기준).
- **동기 시그니처 유지** — caller 가 동기 (cycle.py polling loop). 내부에서 ``asyncio.run`` 으로
  async ``query()`` 를 감쌈. orchestrator/lead.py 의 ThreadPoolExecutor 4-worker 병렬은 각
  thread 가 자체 event loop 를 가지므로 안전.
- **dry_run 모드는 SDK 호출 X** — 기존 mock 그대로 유지 (기존 dry_run 테스트 12건 무수정 통과).
- **timeout** — SDK 의 ``query()`` 는 timeout 옵션 없음. ``asyncio.wait_for`` 로 감싼다.
- **prompt cache hit 보존** — system_prompt 를 dict preset 형태로 넘기고 ``exclude_dynamic_sections=True``
  로 cwd/git/OS 컨텍스트를 system prompt 에서 제외 → CLI 의 ``--exclude-dynamic-system-prompt-sections``
  플래그와 동일 효과.
- **디버그 trace** — 이전엔 stdout/stderr 텍스트 dump. 이제는 message stream 을 jsonl 로 dump
  (turn 별 tool 호출 trace 까지 보존되어 오히려 더 풍부).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    UserMessage,
    query,
)

from multi_research_agent.config import get_settings

log = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class AgentResult:
    """이전 subprocess 기반 runner 와 시그니처 동일 — caller 무수정 보장."""

    agent: str
    success: bool
    stdout: str                            # ResultMessage.result 또는 마지막 assistant 텍스트
    stderr: str                            # 에러 메시지 (timeout / SDK 예외 / is_error)
    duration_sec: float
    cost_usd: float = 0.0
    estimated_cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    task_ref: str | None = None
    raw_meta: dict[str, Any] | None = None


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
    """SDK ``query()`` 동기 wrapper. dry_run 모드는 SDK 호출 없이 mock."""
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

    debug_dir = s.logs_dir / "agent-runs"
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe_ref = (task_ref or f"{agent}-{started:%Y%m%d%H%M%S}").replace("/", "_")
    debug_path = debug_dir / f"{safe_ref}.messages.jsonl"

    options = ClaudeAgentOptions(
        model=model,
        cwd=cwd,
        max_turns=max_turns,
        permission_mode="bypassPermissions",       # 구 --dangerously-skip-permissions
        allowed_tools=list(allowed_tools or []),
        add_dirs=[Path(d) for d in (add_dirs or [])],
        # cwd/git/OS 같은 dynamic 컨텍스트를 system prompt 에서 제외 →
        # 같은 에이전트 + 같은 모델 호출 시 cache hit ↑ (비용 -50% 효과적).
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "exclude_dynamic_sections": True,
        },
    )

    log.info("[%s] SDK query 시작 task=%s model=%s", agent, task_ref, model)

    try:
        result_data = asyncio.run(
            _query_with_timeout(
                prompt=prompt,
                options=options,
                debug_path=debug_path,
                timeout_sec=timeout_sec,
            )
        )
    except _SDKTimeoutError:
        duration = (dt.datetime.now(dt.UTC) - started).total_seconds()
        return AgentResult(
            agent=agent,
            success=False,
            stdout="",
            stderr=f"timeout after {timeout_sec}s",
            duration_sec=duration,
            task_ref=task_ref,
        )
    except Exception as exc:  # SDK / network / parsing 등 — 로그만 남기고 failed 반환
        duration = (dt.datetime.now(dt.UTC) - started).total_seconds()
        log.exception("[%s] SDK 호출 실패 task=%s", agent, task_ref)
        return AgentResult(
            agent=agent,
            success=False,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc!s}",
            duration_sec=duration,
            task_ref=task_ref,
        )

    duration = (dt.datetime.now(dt.UTC) - started).total_seconds()
    return AgentResult(
        agent=agent,
        success=result_data["success"],
        stdout=result_data["stdout"],
        stderr=result_data["stderr"],
        duration_sec=duration,
        cost_usd=result_data["cost_usd"],
        input_tokens=result_data["input_tokens"],
        output_tokens=result_data["output_tokens"],
        cache_read_tokens=result_data["cache_read_tokens"],
        cache_creation_tokens=result_data["cache_creation_tokens"],
        task_ref=task_ref,
        raw_meta=result_data["raw_meta"],
    )


# ─── 내부: async 호출 + timeout ─────────────────────────────────────────


class _SDKTimeoutError(RuntimeError):
    """asyncio.wait_for → TimeoutError 를 caller 가 구분하기 쉽게 명시 타입으로 wrap."""


async def _query_with_timeout(
    *,
    prompt: str,
    options: ClaudeAgentOptions,
    debug_path: Path,
    timeout_sec: int,
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(
            _consume_query(prompt=prompt, options=options, debug_path=debug_path),
            timeout=timeout_sec,
        )
    except TimeoutError as exc:  # asyncio.wait_for raises asyncio.TimeoutError == TimeoutError
        raise _SDKTimeoutError(f"SDK query timed out after {timeout_sec}s") from exc


async def _consume_query(
    *,
    prompt: str,
    options: ClaudeAgentOptions,
    debug_path: Path,
) -> dict[str, Any]:
    """message stream 소비 → jsonl dump + ResultMessage 추출."""
    last_assistant_text: list[str] = []
    result_msg: ResultMessage | None = None

    # 한 호출당 한 파일 — append 모드로 turn 별 record 누적
    with debug_path.open("w", encoding="utf-8") as fh:
        async for message in query(prompt=prompt, options=options):
            record = _serialize_message(message)
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()

            if isinstance(message, AssistantMessage):
                # text block 만 추려서 stdout 호환 — 디버그/로그용 (raw_meta 가 진실)
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        last_assistant_text.append(text)
            elif isinstance(message, ResultMessage):
                result_msg = message

    if result_msg is None:
        return {
            "success": False,
            "stdout": "".join(last_assistant_text),
            "stderr": "no ResultMessage in stream",
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "raw_meta": None,
        }

    usage = result_msg.usage or {}
    success = (not result_msg.is_error) and (result_msg.stop_reason in (None, "end_turn"))
    stdout_text = result_msg.result or "".join(last_assistant_text)
    stderr_text = ""
    if result_msg.is_error:
        stderr_text = f"is_error=True stop_reason={result_msg.stop_reason} subtype={result_msg.subtype}"

    raw_meta = {
        "subtype": result_msg.subtype,
        "stop_reason": result_msg.stop_reason,
        "num_turns": result_msg.num_turns,
        "duration_ms": result_msg.duration_ms,
        "duration_api_ms": result_msg.duration_api_ms,
        "session_id": result_msg.session_id,
        "total_cost_usd": result_msg.total_cost_usd,
        "usage": usage,
        "model_usage": result_msg.model_usage,
    }

    return {
        "success": success,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "cost_usd": float(result_msg.total_cost_usd or 0.0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "raw_meta": raw_meta,
    }


def _serialize_message(message: Any) -> dict[str, Any]:
    """message 객체를 jsonl 한 줄로 직렬화 — best-effort (SDK 가 dataclass 류면 asdict)."""
    type_name = type(message).__name__
    out: dict[str, Any] = {"type": type_name}

    if dataclasses.is_dataclass(message):
        try:
            payload = dataclasses.asdict(message)
        except TypeError:
            payload = {k: _safe_value(v) for k, v in vars(message).items()}
        out.update(_jsonable(payload))
        return out

    # SDK 가 TypedDict / 일반 클래스를 쓰는 경우
    if hasattr(message, "__dict__"):
        out.update({k: _safe_value(v) for k, v in vars(message).items()})
        return out
    out["repr"] = repr(message)
    return out


def _safe_value(value: Any) -> Any:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list | tuple):
        return [_safe_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _safe_value(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value):
        try:
            return _jsonable(dataclasses.asdict(value))
        except TypeError:
            return repr(value)
    return repr(value)


def _jsonable(obj: Any) -> Any:
    """dict/list 안에 있는 비직렬화 값을 repr 로 fallback."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, str | int | float | bool) or obj is None:
        return obj
    return repr(obj)


# 사용 안 하지만 message 타입 식별용으로 re-export
__all__ = [
    "AgentResult",
    "AssistantMessage",
    "ResultMessage",
    "SystemMessage",
    "UserMessage",
    "run_agent",
]
