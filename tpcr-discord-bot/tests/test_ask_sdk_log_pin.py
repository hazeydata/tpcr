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
    for name in ("anthropic", "httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING, name
