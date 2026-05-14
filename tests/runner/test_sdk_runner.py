"""SDK 기반 runner 단위 테스트.

claude_agent_sdk.query 를 monkeypatch 로 가짜 async generator 로 교체. 실제 SDK 호출은 안 함.

검증 포인트:
1. live 모드에서 ResultMessage 가 정상 도착하면 AgentResult 의 cost/token 매핑 정확
2. ResultMessage.is_error=True → success=False + stderr 채워짐
3. ResultMessage 없이 stream 종료 → success=False + "no ResultMessage in stream"
4. timeout → AgentResult(success=False, stderr="timeout after Xs")
5. dry_run 모드는 SDK 호출 안 함 (기존 동작 보존)
6. agent dir 누락 → success=False (SDK 호출 안 함)
7. debug jsonl 파일 생성됨 + ResultMessage record 포함
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

import multi_research_agent.supervisor.runner as runner_mod
from multi_research_agent.config import get_settings
from multi_research_agent.supervisor.runner import run_agent

# ─── helpers ─────────────────────────────────────────────────────────────


def _result_message(
    *,
    is_error: bool = False,
    stop_reason: str | None = "end_turn",
    total_cost_usd: float = 0.012,
    input_tokens: int = 1200,
    output_tokens: int = 400,
    cache_read: int = 800,
    cache_creation: int = 200,
    result_text: str = "ok",
    subtype: str = "success",
    session_id: str = "sess-test-1",
) -> MagicMock:
    """SDK 의 ResultMessage 형태 모의 (dataclass 가 아니라 attr 만 맞춤)."""
    m = MagicMock()
    m.__class__.__name__ = "ResultMessage"
    m.subtype = subtype
    m.duration_ms = 1234
    m.duration_api_ms = 1100
    m.is_error = is_error
    m.num_turns = 3
    m.session_id = session_id
    m.stop_reason = stop_reason
    m.total_cost_usd = total_cost_usd
    m.usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    m.result = result_text
    m.structured_output = None
    m.model_usage = {"claude-sonnet-4-6": m.usage}
    return m


def _assistant_message(text: str) -> MagicMock:
    text_block = MagicMock()
    text_block.text = text
    m = MagicMock()
    m.__class__.__name__ = "AssistantMessage"
    m.content = [text_block]
    return m


def _fake_query_factory(messages: list[Any]):
    """``query(prompt=..., options=...)`` 가 반환할 async generator factory."""

    async def fake_query(*_args: Any, **_kwargs: Any):
        for msg in messages:
            await asyncio.sleep(0)
            yield msg

    return fake_query


def _patch_query(monkeypatch, messages):
    """ResultMessage 가 우리 mock 이라 isinstance 체크가 깨짐 → 클래스 자체도 swap."""
    monkeypatch.setattr(runner_mod, "query", _fake_query_factory(messages))
    # ResultMessage / AssistantMessage 의 isinstance 체크가 mock 인스턴스를 받아들이도록
    # 두 심볼을 우리 mock 의 클래스로 교체. (가장 단순한 트릭)
    if messages:
        # 마지막이 result 일 가능성이 높음 → 그것의 type 을 ResultMessage 로
        result_like = next(
            (m for m in messages if type(m).__name__ == "ResultMessage" or
             getattr(m.__class__, "__name__", "") == "ResultMessage"),
            None,
        )
        if result_like is not None:
            monkeypatch.setattr(runner_mod, "ResultMessage", type(result_like))
        assistant_like = next(
            (m for m in messages if type(m).__name__ == "AssistantMessage" or
             getattr(m.__class__, "__name__", "") == "AssistantMessage"),
            None,
        )
        if assistant_like is not None:
            monkeypatch.setattr(runner_mod, "AssistantMessage", type(assistant_like))


@pytest.fixture
def live_mode(monkeypatch):
    """run_mode=live + 임시 logs_dir + agents_dir 의 hypothesis 디렉토리 보장."""
    monkeypatch.setenv("RUN_MODE", "live")
    # cached settings 무효화
    import multi_research_agent.config as cfg
    monkeypatch.setattr(cfg, "_settings", None)
    s = get_settings()
    assert s.run_mode == "live"
    # hypothesis dir 은 실제 존재 (agents/hypothesis/CLAUDE.md). cwd 체크 통과용.
    assert (s.agents_dir / "hypothesis").exists()
    yield s


# ─── tests ──────────────────────────────────────────────────────────────


def test_dry_run_skips_sdk(monkeypatch):
    """dry_run 모드는 SDK 호출 자체를 하지 않는다 — query 가 호출되면 실패해야 함."""

    called: dict[str, int] = {"n": 0}

    async def fake_query(*_a, **_kw):
        called["n"] += 1
        yield _result_message()

    monkeypatch.setattr(runner_mod, "query", fake_query)
    monkeypatch.setenv("RUN_MODE", "dry_run")
    import multi_research_agent.config as cfg
    monkeypatch.setattr(cfg, "_settings", None)

    result = run_agent(
        agent="hypothesis",
        prompt="dry",
        model="claude-haiku-4-5",
        task_ref="dry-1",
    )
    assert result.success is True
    assert result.cost_usd == 0.0
    assert called["n"] == 0


def test_live_success_extracts_cost_and_tokens(monkeypatch, live_mode):
    msgs = [
        _assistant_message("doing work..."),
        _result_message(
            is_error=False,
            stop_reason="end_turn",
            total_cost_usd=0.234,
            input_tokens=2000,
            output_tokens=600,
            cache_read=1500,
            cache_creation=300,
            result_text="completed",
        ),
    ]
    _patch_query(monkeypatch, msgs)

    result = run_agent(
        agent="hypothesis",
        prompt="generate hypothesis",
        model="claude-opus-4-7",
        max_turns=10,
        task_ref="live-1",
    )

    assert result.success is True
    assert result.cost_usd == pytest.approx(0.234)
    assert result.input_tokens == 2000
    assert result.output_tokens == 600
    assert result.cache_read_tokens == 1500
    assert result.cache_creation_tokens == 300
    assert "completed" in result.stdout
    assert result.raw_meta is not None
    assert result.raw_meta["stop_reason"] == "end_turn"
    assert result.raw_meta["session_id"] == "sess-test-1"


def test_live_error_result_marks_failure(monkeypatch, live_mode):
    msgs = [_result_message(is_error=True, stop_reason="max_tokens", result_text=None)]
    _patch_query(monkeypatch, msgs)

    result = run_agent(
        agent="hypothesis",
        prompt="x",
        model="claude-opus-4-7",
        task_ref="live-err-1",
    )
    assert result.success is False
    assert "is_error=True" in result.stderr
    assert "max_tokens" in result.stderr


def test_live_no_result_message_marks_failure(monkeypatch, live_mode):
    msgs = [_assistant_message("started but never finished")]
    _patch_query(monkeypatch, msgs)

    result = run_agent(
        agent="hypothesis",
        prompt="x",
        model="claude-opus-4-7",
        task_ref="live-noresult-1",
    )
    assert result.success is False
    assert "no ResultMessage" in result.stderr
    assert "started but never finished" in result.stdout


def test_live_timeout_returns_failed_result(monkeypatch, live_mode):
    async def slow_query(*_a, **_kw):
        await asyncio.sleep(5.0)  # > timeout
        yield _result_message()

    monkeypatch.setattr(runner_mod, "query", slow_query)

    result = run_agent(
        agent="hypothesis",
        prompt="x",
        model="claude-opus-4-7",
        timeout_sec=1,
        task_ref="live-timeout-1",
    )
    assert result.success is False
    assert "timeout after 1s" in result.stderr


def test_missing_agent_dir_returns_failure(monkeypatch, live_mode):
    """SDK 호출 전 cwd 검사 단계에서 fail — query 가 호출되면 안 됨."""

    called = {"n": 0}

    async def fake_query(*_a, **_kw):
        called["n"] += 1
        yield _result_message()

    monkeypatch.setattr(runner_mod, "query", fake_query)

    result = run_agent(
        agent="nonexistent_agent_xyz",
        prompt="x",
        model="claude-opus-4-7",
        task_ref="live-missing-dir",
    )
    assert result.success is False
    assert "agent dir missing" in result.stderr
    assert called["n"] == 0


def test_debug_jsonl_dumped(monkeypatch, live_mode, tmp_path):
    """messages 가 jsonl 로 디버그 디렉토리에 기록되어야 한다."""
    msgs = [
        _assistant_message("step 1"),
        _result_message(result_text="done"),
    ]
    _patch_query(monkeypatch, msgs)

    # logs_dir 를 임시 디렉토리로 우회 (전역 settings 객체 mutation)
    import multi_research_agent.config as cfg
    s = cfg.get_settings()
    monkeypatch.setattr(s, "logs_dir", tmp_path / "logs", raising=False)

    result = run_agent(
        agent="hypothesis",
        prompt="x",
        model="claude-haiku-4-5",
        task_ref="debug-jsonl-1",
    )
    assert result.success is True

    debug_path = tmp_path / "logs" / "agent-runs" / "debug-jsonl-1.messages.jsonl"
    assert debug_path.exists(), f"debug file not created at {debug_path}"
    lines = [json.loads(line) for line in debug_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    types = [line["type"] for line in lines]
    assert "AssistantMessage" in types
    assert "ResultMessage" in types
