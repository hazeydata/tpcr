"""Regression tests for review findings on /ask (think stripping, config, no-data, Claude fallback)."""

import asyncio
import json
import logging
import os
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402

CALL = '<tool_call>{"name": "run_query", "arguments": {"sql": "SELECT 1"}}</tool_call>'


def chat_reply(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


@pytest.fixture(autouse=True)
def no_side_effects():
    with patch("ask_agent.track_usage", return_value=(1, -1)), \
         patch("ask_agent.log_question"), patch("ask_agent._trigger_self_heal"):
        yield


def _run_text(text):
    with patch("ask_agent._post_chat", AsyncMock(return_value=chat_reply(text))), \
         patch("ask_agent.run_duckdb_query") as q:
        answer = asyncio.run(aa.ask_agent("q", "u"))
    return answer, q


# 1. nested / unmatched think
def test_nested_think_tool_call_not_executed():
    answer, q = _run_text(f"a<think>b</think>{CALL}</think>")
    q.assert_not_called()
    assert answer == "a"
    assert aa.strip_think(f"a<think>b</think>{CALL}</think>") == "a"


def test_strip_think_keeps_text_between_separate_blocks():
    assert aa.strip_think("<think>x</think>keep<think>y</think>end") == "keepend"


def test_strip_think_nested_balanced():
    assert aa.strip_think(f"x<think>a<think>b</think>{CALL}</think>y") == "xy"


def test_strip_think_unmatched_open_hides_rest():
    assert aa.strip_think(f"keep<think>a<think>b</think>{CALL}") == "keep"


# 2. case-insensitive
def test_strip_think_uppercase():
    assert aa.strip_think(f"<THINK>{CALL}</Think>Done") == "Done"
    answer, q = _run_text(f"<THINK>{CALL}</THINK>Done")
    q.assert_not_called()
    assert answer == "Done"


# 3. invalid provider
def test_invalid_provider_fails_at_startup(monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openai")
    with pytest.raises(RuntimeError, match="anthropic or qwen"):
        aa.validate_config()
    with pytest.raises(RuntimeError, match="anthropic or qwen"):
        asyncio.run(aa.ask_agent("q", "u"))


def test_valid_providers_accepted(monkeypatch):
    for p in ("anthropic", "QWEN", " qwen "):
        monkeypatch.setenv("TPCR_ASK_PROVIDER", p)
        aa.validate_config()


# 4. missing data root
def test_missing_data_root_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("TPCR_DATA_ROOT", str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="does not exist"):
        aa.validate_config()


def test_bot_calls_validate_config_at_import():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    assert "\nvalidate_config()\n" in src


# 5. no-data message wins over a non-empty final answer
def test_no_data_message_even_when_final_answer_nonempty():
    async def chat(messages, use_tools):
        if use_tools:
            return chat_reply(tool_calls=[{"id": "c", "type": "function", "function": {
                "name": "run_query", "arguments": json.dumps({"sql": "SELECT 1"})}}])
        return chat_reply("Sorry, nothing found.")

    def query(sql, *a, **k):
        if "min(d)" in sql:
            return "min_date | max_date\n-----\n2026-10-21 | 2026-10-22"
        return aa.NO_RESULTS_MSG

    with patch("ask_agent._chat", AsyncMock(side_effect=chat)), \
         patch("ask_agent.run_duckdb_query", side_effect=query):
        answer = asyncio.run(aa.ask_agent("q", "u"))
    assert "don't have any wait data" in answer
    assert "2026-10-21" in answer and "2026-10-22" in answer


# 6. any Claude exception falls back and is logged
def test_claude_keyerror_falls_back_to_qwen_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    with patch("ask_agent._claude_loop", AsyncMock(side_effect=KeyError("content"))), \
         patch("ask_agent._chat", AsyncMock(return_value=chat_reply("qwen answer"))), \
         caplog.at_level(logging.WARNING, logger=aa.logger.name):
        answer = asyncio.run(aa.ask_agent("q", "u", api_key="sk-test"))
    assert answer == "qwen answer"
    assert "KeyError" in caplog.text and "falling back to Qwen" in caplog.text


# 7. Claude log records actual rounds
def test_claude_log_records_actual_rounds(monkeypatch, caplog):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    blocks = [
        [types.SimpleNamespace(type="tool_use", id="t1", input={"sql": "SELECT 1"})],
        [types.SimpleNamespace(type="text", text="claude done")],
    ]

    class Msgs:
        async def create(self, **kw):
            return types.SimpleNamespace(content=blocks.pop(0))

    fake = types.ModuleType("anthropic")
    fake.AsyncAnthropic = lambda **kw: types.SimpleNamespace(messages=Msgs())
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    with patch("ask_agent.run_duckdb_query", return_value="x"), \
         caplog.at_level(logging.INFO, logger=aa.logger.name):
        answer = asyncio.run(aa.ask_agent("q", "u", api_key="sk-test"))
    assert answer == "claude done"
    assert "after 2 rounds" in caplog.text
    assert "after 5 rounds" not in caplog.text
