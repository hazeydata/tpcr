"""/ask provider selection: OpenRouter first with Qwen fallback."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402


def _ask(api_key="sk-test", claude=None, qwen="qwen answer"):
    claude = claude or AsyncMock(return_value="claude answer")
    with patch.object(aa, "_openrouter_loop", claude), \
            patch.object(aa, "_qwen_loop", return_value=qwen) as qwen_loop, \
            patch.object(aa, "track_usage", return_value=(1, -1)), \
            patch.object(aa, "log_question") as log, patch.object(aa, "_trigger_self_heal"):
        answer = asyncio.run(aa.ask_agent("q", "u", api_key=api_key))
    return answer, claude, qwen_loop, log


@pytest.mark.parametrize("provider", ["openrouter", "anthropic", " OpenRouter "])
def test_openrouter_with_key_uses_claude_and_not_qwen(monkeypatch, provider):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", provider)
    answer, claude, qwen, log = _ask()
    assert answer == "claude answer"
    claude.assert_awaited_once()
    qwen.assert_not_called()
    assert log.call_args.args[-1] == "anthropic/claude-sonnet-4.6"


def test_default_provider_is_openrouter(monkeypatch):
    monkeypatch.delenv("TPCR_ASK_PROVIDER", raising=False)
    assert aa.ask_provider() == "openrouter"
    answer, _, qwen, _ = _ask()
    assert answer == "claude answer"
    qwen.assert_not_called()


def test_qwen_provider_skips_claude(monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "qwen")
    answer, claude, qwen, log = _ask()
    assert answer == "qwen answer"
    claude.assert_not_called()
    assert log.call_args.args[-1] == aa.QWEN_LABEL


@pytest.mark.parametrize("exc", [asyncio.TimeoutError(), RuntimeError("boom"), KeyError("choices")])
def test_any_claude_exception_falls_back_to_qwen(monkeypatch, exc):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openrouter")
    answer, claude, qwen, log = _ask(claude=AsyncMock(side_effect=exc))
    assert answer == "qwen answer"
    qwen.assert_called_once()
    assert log.call_args.args[-1] == aa.QWEN_LABEL


def test_empty_claude_answer_falls_back_to_qwen(monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openrouter")
    answer, _, qwen, _ = _ask(claude=AsyncMock(return_value=None))
    assert answer == "qwen answer"
    qwen.assert_called_once()


def test_openrouter_without_api_key_goes_to_qwen(monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openrouter")
    answer, claude, qwen, _ = _ask(api_key=None)
    assert answer == "qwen answer"
    claude.assert_not_called()


@pytest.mark.parametrize("bad", ["gpt", "ollama", "openai"])
def test_invalid_provider_raises(monkeypatch, bad):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", bad)
    with pytest.raises(RuntimeError, match="TPCR_ASK_PROVIDER"):
        aa.ask_provider()
