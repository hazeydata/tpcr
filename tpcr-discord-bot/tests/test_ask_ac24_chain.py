"""AC24 / Spec R4: a provider error carrying the API key, followed by a Qwen error whose
body echoes the key, must never reach any log record, traceback or the question log."""

import asyncio
import logging
import os
import sys
import traceback
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ask_agent as aa  # noqa: E402
from .test_ask_qwen import bot, make_interaction, call_ask  # noqa: F401,E402

# Built at runtime so no secret-shaped literal lives in the source.
TAIL = "TESTSECRET" + "123"
SECRET = "sk-" + "ant-" + TAIL
_REAL_LOG_QUESTION = aa.log_question


class AuthenticationError(Exception):
    """Stand-in for anthropic.AuthenticationError (401) whose message echoes the key."""
    status_code = 401


def _all_log_text(caplog):
    out = [caplog.text]
    for r in caplog.records:
        out.append(r.getMessage())
        if r.exc_info:
            out.append("".join(traceback.format_exception(*r.exc_info)))
        if r.exc_text:
            out.append(r.exc_text)
        if r.stack_info:
            out.append(r.stack_info)
    return "\n".join(out)


def _chained_qwen_failure():
    async def chat(*a, **k):
        try:
            raise AuthenticationError(f"Error code: 401 invalid x-api-key {SECRET}")
        except AuthenticationError:
            # raised while handling: the Claude error becomes __context__
            raise aa.QwenHTTPError(500, f'{{"error": "bad key {SECRET}"}}')
    return chat


def test_qwen_http_error_message_is_redacted():
    e = aa.QwenHTTPError(502, f"upstream said Authorization: Bearer {SECRET} api_key={SECRET}")
    assert TAIL not in str(e) and "sk-ant-" not in str(e)
    assert str(e).startswith("HTTP 502: ")
    assert e.status == 502


def test_ac24_claude_401_then_qwen_key_body_never_logged(bot, monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    qlog = tmp_path / "q.jsonl"
    monkeypatch.setenv("TPCR_ASK_QUESTION_LOG", str(qlog))
    monkeypatch.setattr(bot, "ANTHROPIC_API_KEY", SECRET)
    claude = AsyncMock(side_effect=AuthenticationError(f"Error code: 401 invalid x-api-key {SECRET}"))
    events = []
    with patch("ask_agent._claude_loop", claude), \
         patch("ask_agent._chat", _chained_qwen_failure()), \
         patch("ask_agent.log_question", _REAL_LOG_QUESTION), \
         patch("ask_agent.track_usage", return_value=(1, -1)), \
         caplog.at_level(logging.DEBUG):
        call_ask(bot, make_interaction(events))
    claude.assert_awaited_once()
    text = _all_log_text(caplog)
    stored = qlog.read_text() if qlog.exists() else ""
    for blob in (text, stored):
        assert TAIL not in blob and "sk-ant-" not in blob
    assert not any(r.exc_info for r in caplog.records)
    assert "Claude /ask auth failed (AuthenticationError, status 401)" in text


def test_ask_failure_log_line_has_no_message_text(bot, caplog):
    """Spec R4: '/ask failed' carries class + integer status only."""
    err = aa.QwenHTTPError(503, "upstream overloaded: request-id req_abc123 detail-xyz")
    with patch.object(bot, "ask_agent", AsyncMock(side_effect=err)), caplog.at_level(logging.DEBUG):
        call_ask(bot, make_interaction([]))
    lines = [r for r in caplog.records if r.getMessage().startswith("/ask failed")]
    assert [r.getMessage() for r in lines] == ["/ask failed (QwenHTTPError, status 503)"]
    assert not lines[0].exc_info
    for frag in ("overloaded", "req_abc123", "detail-xyz", "HTTP 503"):
        assert frag not in _all_log_text(caplog)


@pytest.mark.parametrize("exc,want", [
    (asyncio.TimeoutError("read timed out at upstream-host-detail"), "TimeoutError"),
    (ConnectionError("refused by upstream-host-detail"), "ConnectionError"),
])
def test_ask_failure_timeouts_log_class_only(bot, caplog, exc, want):
    with patch.object(bot, "ask_agent", AsyncMock(side_effect=exc)), caplog.at_level(logging.DEBUG):
        call_ask(bot, make_interaction([]))
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("/ask failed")] == \
        [f"/ask failed ({want})"]
    assert "upstream-host-detail" not in _all_log_text(caplog)


def test_deliver_failure_has_no_chained_traceback(bot, caplog):
    events = []
    inter = make_interaction(events, edit_raises=True)
    err = aa.QwenHTTPError(500, f"echo {SECRET}")
    with patch.object(bot, "ask_agent", AsyncMock(side_effect=err)), caplog.at_level(logging.DEBUG):
        call_ask(bot, inter)
    text = _all_log_text(caplog)
    assert "/ask could not deliver error message (RuntimeError)" in text
    assert not any(r.exc_info for r in caplog.records)
    assert TAIL not in text and "secret-internal-detail" not in text
