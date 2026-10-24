"""Spec R5/AC33-34: with root at DEBUG, the /ask HTTP path (OpenRouter via httpx, mocked
transport) that 401s must not leak request ids, URLs or the key through the
httpx/httpcore loggers. All five SDK/HTTP loggers stay pinned at WARNING.
No anthropic SDK needed; the anthropic-specific case skips when it is absent."""

import logging
import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ask_agent as aa  # noqa: E402
from .test_ask_qwen import bot, make_interaction, call_ask  # noqa: F401,E402
from .test_ask_ac24_chain import _all_log_text  # noqa: E402

PINNED = ("anthropic", "httpx", "httpcore", "httpx2", "httpcore2")
HTTP_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")
# Built at runtime so no secret-shaped literal lives in the source.
TAIL = "PLANTEDKEY" + "789"
SECRET = "sk-" + "or-v1-" + TAIL
REQ_ID = "req_" + "PLANTEDREQID" + "4242"


def _handler(request):
    return httpx.Response(
        401,
        headers={"x-request-id": REQ_ID, "content-type": "application/json"},
        json={"error": {"code": 401, "message": f"invalid key {SECRET}", "request_id": REQ_ID}},
    )


def test_all_five_loggers_pinned_at_warning(bot):
    for name in PINNED:
        assert logging.getLogger(name).level == logging.WARNING, name


def test_ac33_openrouter_http_debug_lines_do_not_leak(bot, monkeypatch, caplog):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openrouter")
    monkeypatch.setattr(bot, "OPENROUTER_API_KEY", SECRET)
    real = httpx.AsyncClient
    seen = []

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(_handler)
        seen.append(1)
        return real(*a, **k)

    with patch.object(aa.httpx, "AsyncClient", client), \
         patch("ask_agent._chat", AsyncMock(side_effect=aa.QwenHTTPError(500, "down"))), \
         patch("ask_agent.track_usage", return_value=(1, -1)), \
         patch("ask_agent.log_question"), \
         caplog.at_level(logging.DEBUG):
        assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
        call_ask(bot, make_interaction([]))
    assert seen, "the OpenRouter HTTP path was not exercised"
    text = _all_log_text(caplog)
    assert "OpenRouter /ask auth failed (OpenRouterHTTPError, status 401)" in text
    assert REQ_ID not in text and "PLANTEDREQID" not in text
    assert TAIL not in text and "sk-or-" not in text
    assert "openrouter.ai" not in text
    http_lines = [r.getMessage() for r in caplog.records
                  if r.name.split(".")[0] in HTTP_LOGGERS and r.levelno < logging.WARNING]
    assert http_lines == [], http_lines
    for name in PINNED:
        assert logging.getLogger(name).level == logging.WARNING, name


def test_anthropic_sdk_logger_pinned_when_installed(bot):
    pytest.importorskip("anthropic")
    assert logging.getLogger("anthropic").level == logging.WARNING
    assert not logging.getLogger("anthropic").isEnabledFor(logging.INFO)


def test_ac33_anthropic_log_env_cannot_reenable_sdk_debug(bot, monkeypatch, caplog):
    """ANTHROPIC_LOG=debug must not re-enable SDK DEBUG: bot.py drops it before any SDK import."""
    import importlib
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    monkeypatch.delitem(sys.modules, "bot")  # restored to the fixture's module afterwards
    bot = importlib.import_module("bot")  # fresh import with ANTHROPIC_LOG=debug set
    assert "ANTHROPIC_LOG" not in os.environ
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openrouter")
    monkeypatch.setattr(bot, "OPENROUTER_API_KEY", SECRET)
    real = httpx.AsyncClient

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(_handler)
        return real(*a, **k)

    with patch.object(aa.httpx, "AsyncClient", client), \
         patch("ask_agent._chat", AsyncMock(side_effect=aa.QwenHTTPError(500, "down"))), \
         patch("ask_agent.track_usage", return_value=(1, -1)), \
         patch("ask_agent.log_question"), \
         caplog.at_level(logging.DEBUG):
        try:  # a fresh SDK import (if installed) must see no ANTHROPIC_LOG
            for name in [m for m in sys.modules if m == "anthropic" or m.startswith("anthropic.")]:
                monkeypatch.delitem(sys.modules, name)
            import anthropic  # noqa: F401
        except ImportError:
            pass
        call_ask(bot, make_interaction([]))
    assert logging.getLogger("anthropic").getEffectiveLevel() >= logging.WARNING
    leaked = [(r.name, r.getMessage()) for r in caplog.records
              if r.levelno == logging.DEBUG and r.name.split(".")[0] in PINNED]
    assert leaked == [], leaked


def test_bot_pops_anthropic_log_after_load_dotenv_before_ask_agent_import():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    dotenv = src.index('load_dotenv(os.path.expanduser("~/.env"))')
    pop = src.index('os.environ.pop("ANTHROPIC_LOG", None)')
    imp = src.index("from ask_agent import")
    assert dotenv < pop < imp
