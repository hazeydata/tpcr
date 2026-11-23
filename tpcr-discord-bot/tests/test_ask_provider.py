"""/ask provider selection: OpenRouter first with Qwen fallback, TPCR_DATA_ROOT required."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402

QWEN_REPLY = {"choices": [{"message": {"role": "assistant", "content": "qwen answer"}}]}


def _ask(api_key="sk-test", claude=None, chat=None):
    claude = claude or AsyncMock(return_value="claude answer")
    chat = chat or AsyncMock(return_value=QWEN_REPLY)
    with patch.object(aa, "_openrouter_loop", claude), patch.object(aa, "_chat", chat), \
            patch.object(aa, "track_usage", return_value=(1, -1)), \
            patch.object(aa, "log_question") as log, patch.object(aa, "_trigger_self_heal"):
        answer = asyncio.run(aa.ask_agent("q", "u", api_key=api_key))
    return answer, claude, chat, log


def test_data_path_raises_when_unset(monkeypatch):
    monkeypatch.delenv("TPCR_DATA_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="TPCR_DATA_ROOT is not set"):
        aa.data_path()
    monkeypatch.setenv("TPCR_DATA_ROOT", "  ")
    with pytest.raises(RuntimeError, match="TPCR_DATA_ROOT is not set"):
        aa.data_path("x")


def test_data_path_uses_env_with_tilde_expansion(monkeypatch):
    monkeypatch.setenv("TPCR_DATA_ROOT", "~/hazeydata/pipeline")
    expected = os.path.join(os.path.expanduser("~"), "hazeydata", "pipeline")
    assert aa.data_path() == expected
    assert aa.data_path("wti", "wti.parquet") == os.path.join(expected, "wti", "wti.parquet")


@pytest.mark.parametrize("provider", ["openrouter", "anthropic"])
def test_openrouter_with_key_uses_claude_and_not_qwen(monkeypatch, provider):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", provider)
    answer, claude, chat, log = _ask()
    assert answer == "claude answer"
    claude.assert_awaited_once()
    chat.assert_not_called()
    assert log.call_args.args[-1] == "anthropic/claude-sonnet-4.6"


def test_claude_timeout_falls_back_to_qwen(monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openrouter")
    answer, claude, chat, _ = _ask(claude=AsyncMock(side_effect=asyncio.TimeoutError()))
    assert answer == "qwen answer"
    claude.assert_awaited_once()
    chat.assert_awaited()


def test_openrouter_without_api_key_goes_to_qwen(monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openrouter")
    answer, claude, chat, _ = _ask(api_key=None)
    assert answer == "qwen answer"
    claude.assert_not_called()
    chat.assert_awaited()


def test_qwen_provider_skips_claude():
    answer, claude, chat, _ = _ask()
    assert answer == "qwen answer"
    claude.assert_not_called()


def test_default_provider_is_openrouter(monkeypatch):
    monkeypatch.delenv("TPCR_ASK_PROVIDER", raising=False)
    answer, claude, chat, _ = _ask()
    assert answer == "claude answer"
    chat.assert_not_called()
