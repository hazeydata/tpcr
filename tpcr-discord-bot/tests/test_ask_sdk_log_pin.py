"""Spec R5/AC33-34: with root at DEBUG, a real anthropic SDK call (mocked transport)
that 401s must not leak the response request id or the key echoed in the body."""

import logging
import os
import sys
from unittest.mock import AsyncMock, patch

import anthropic

try:  # anthropic>=2 ships its own httpx fork and rejects plain httpx clients
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ask_agent as aa  # noqa: E402
from .test_ask_qwen import bot, make_interaction, call_ask  # noqa: F401,E402
from .test_ask_ac24_chain import _all_log_text  # noqa: E402

# Built at runtime so no secret-shaped literal lives in the source.
TAIL = "PLANTEDKEY" + "789"
SECRET = "sk-" + "ant-" + TAIL
REQ_ID = "req_" + "PLANTEDREQID" + "4242"


def _handler(request):
    return httpx.Response(
        401,
        headers={"request-id": REQ_ID, "content-type": "application/json"},
        json={"type": "error", "request_id": REQ_ID,
              "error": {"type": "authentication_error", "message": f"invalid x-api-key {SECRET}"}},
    )


def test_ac33_sdk_debug_lines_do_not_leak_request_id_or_key(bot, monkeypatch, caplog):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    monkeypatch.setattr(bot, "ANTHROPIC_API_KEY", SECRET)
    real = anthropic.AsyncAnthropic
    seen = []

    def client(*a, **k):
        k["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        c = real(*a, **k)
        seen.append(c)
        return c

    with patch("anthropic.AsyncAnthropic", client), \
         patch("ask_agent._chat", AsyncMock(side_effect=aa.QwenHTTPError(500, "down"))), \
         patch("ask_agent.track_usage", return_value=(1, -1)), \
         patch("ask_agent.log_question"), \
         caplog.at_level(logging.DEBUG):
        assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
        call_ask(bot, make_interaction([]))
    assert seen, "the anthropic SDK path was not exercised"
    text = _all_log_text(caplog)
    assert "Claude /ask auth failed (AuthenticationError, status 401)" in text
    assert REQ_ID not in text and "PLANTEDREQID" not in text
    assert TAIL not in text and "sk-ant-" not in text
    http_lines = [r.getMessage() for r in caplog.records if r.name.split(".")[0] in ("httpx", "httpx2", "httpcore", "httpcore2")]
    assert http_lines == [], http_lines
    for name in ("anthropic", "httpx", "httpcore", "httpx2", "httpcore2"):
        assert logging.getLogger(name).level == logging.WARNING, name


def test_ac33_anthropic_log_env_cannot_reenable_sdk_debug(bot, monkeypatch, caplog):
    """ANTHROPIC_LOG=debug is read by the SDK at import; a fresh import must not re-enable DEBUG."""
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    monkeypatch.setattr(bot, "ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    for name in [m for m in sys.modules if m == "anthropic" or m.startswith("anthropic.")]:
        monkeypatch.delitem(sys.modules, name)
    orig_init = httpx.AsyncClient.__init__

    def init(self, *a, **k):
        k["transport"] = httpx.MockTransport(_handler)
        orig_init(self, *a, **k)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    with patch("ask_agent._chat", AsyncMock(side_effect=aa.QwenHTTPError(500, "down"))), \
         patch("ask_agent.track_usage", return_value=(1, -1)), \
         patch("ask_agent.log_question"), \
         caplog.at_level(logging.DEBUG):
        call_ask(bot, make_interaction([]))
    assert "anthropic" in sys.modules, "the anthropic SDK was not freshly imported"
    assert "ANTHROPIC_LOG" not in os.environ
    assert logging.getLogger("anthropic").getEffectiveLevel() >= logging.WARNING
    leaked = [(r.name, r.getMessage()) for r in caplog.records
              if r.levelno == logging.DEBUG
              and r.name.split(".")[0] in ("anthropic", "httpx", "httpcore", "httpx2", "httpcore2")]
    assert leaked == [], leaked


def test_bot_pops_anthropic_log_after_load_dotenv_before_ask_agent_import():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    dotenv = src.index('load_dotenv(os.path.expanduser("~/.env"))')
    pop = src.index('os.environ.pop("ANTHROPIC_LOG", None)')
    imp = src.index("from ask_agent import")
    assert dotenv < pop < imp


_PINNED = ("anthropic", "httpx", "httpcore", "httpx2", "httpcore2")


def test_bot_import_pins_all_sdk_loggers_without_ask_path(bot):
    """Import-time pin only: reset levels, reload bot, never call /ask."""
    import importlib
    saved = {n: logging.getLogger(n).level for n in _PINNED}
    try:
        for n in _PINNED:
            logging.getLogger(n).setLevel(logging.NOTSET)
        importlib.reload(bot)
        for n in _PINNED:
            assert logging.getLogger(n).level == logging.WARNING, n
    finally:
        for n, lvl in saved.items():
            logging.getLogger(n).setLevel(lvl)


def test_ac33_claude_loop_repins_anthropic_logger(bot, monkeypatch, caplog):
    """Something lowering the anthropic logger after bot import must not survive an /ask call."""
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    monkeypatch.setattr(bot, "ANTHROPIC_API_KEY", SECRET)
    real = anthropic.AsyncAnthropic
    seen = []

    def client(*a, **k):
        k["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        c = real(*a, **k)
        seen.append(c)
        return c

    saved = logging.getLogger("anthropic").level
    try:
        logging.getLogger("anthropic").setLevel(logging.DEBUG)
        with patch("anthropic.AsyncAnthropic", client), \
             patch("ask_agent._chat", AsyncMock(side_effect=aa.QwenHTTPError(500, "down"))), \
             patch("ask_agent.track_usage", return_value=(1, -1)), \
             patch("ask_agent.log_question"), \
             caplog.at_level(logging.DEBUG):
            call_ask(bot, make_interaction([]))
        assert seen, "the anthropic SDK path was not exercised"
        assert logging.getLogger("anthropic").level == logging.WARNING
        leaked = [(r.name, r.getMessage()) for r in caplog.records
                  if r.levelno == logging.DEBUG and r.name.split(".")[0] in _PINNED]
        assert leaked == [], leaked
    finally:
        logging.getLogger("anthropic").setLevel(saved)
