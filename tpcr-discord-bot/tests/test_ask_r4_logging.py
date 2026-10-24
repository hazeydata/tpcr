"""Spec R4: /ask server logs carry the exception class plus ", status N" only.

Behavior is checked through caplog on the real ask_command path: never the raw
message, never a traceback, and status only when it is a real int.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .test_ask_qwen import bot, call_ask, make_interaction  # noqa: F401

PLANTED = "sk-or-v1-PLANTEDr4secret0000"


class EditDenied(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code


class CodeOnlyError(Exception):
    """Carries .status_code but no .status (e.g. httpx/openai style)."""

    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.status_code = status_code


def _interaction_with_failing_edit(events, exc):
    interaction = make_interaction(events)
    placeholder = MagicMock()

    async def edit(**kw):
        events.append(("edit", kw))
        raise exc

    placeholder.edit = edit

    async def send(*a, **k):
        events.append(("send", a, k))
        return placeholder

    interaction.followup.send = send
    return interaction


def _all_log_text(caplog):
    return "\n".join(caplog.text.splitlines() + [r.getMessage() for r in caplog.records])


@pytest.mark.parametrize("status_code,want", [(403, "EditDenied, status 403"), (None, "EditDenied")])
def test_placeholder_edit_failure_logs_class_status_only(bot, caplog, status_code, want):
    events = []
    exc = EditDenied(f"edit failed Authorization: Bearer {PLANTED}", status_code)
    interaction = _interaction_with_failing_edit(events, exc)
    with patch.object(bot, "ask_agent", AsyncMock(return_value="Answer")):
        with caplog.at_level("DEBUG"):
            call_ask(bot, interaction)

    warns = [r for r in caplog.records if r.getMessage().startswith("/ask placeholder edit failed")]
    assert [r.getMessage() for r in warns] == [f"/ask placeholder edit failed ({want}); sending new message"]
    assert not any(r.exc_info for r in caplog.records)
    assert PLANTED not in _all_log_text(caplog)
    assert "Bearer" not in _all_log_text(caplog)
    assert "Traceback" not in caplog.text


def _ask_failed_messages(bot, caplog, exc):
    events = []
    interaction = make_interaction(events)
    with patch.object(bot, "ask_agent", AsyncMock(side_effect=exc)):
        with caplog.at_level("DEBUG"):
            call_ask(bot, interaction)
    assert PLANTED not in _all_log_text(caplog)
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("/ask failed")]


def test_status_code_attribute_is_logged(bot, caplog):
    exc = CodeOnlyError(f"unauthorized {PLANTED}", 401)
    assert not hasattr(exc, "status")
    assert _ask_failed_messages(bot, caplog, exc) == ["/ask failed (CodeOnlyError, status 401)"]


@pytest.mark.parametrize("bad", ["401", 401.0, True, b"401", [401]])
def test_non_int_status_code_is_not_appended(bot, caplog, bad):
    exc = CodeOnlyError(f"unauthorized {PLANTED}", bad)
    assert _ask_failed_messages(bot, caplog, exc) == ["/ask failed (CodeOnlyError)"]
