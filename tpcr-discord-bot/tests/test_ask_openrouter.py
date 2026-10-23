"""/ask via OpenRouter's OpenAI-compatible API with Qwen fallback (mocked HTTP, no network)."""

import asyncio
import json
import logging
import os
import sys
import types
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402

SECRET = "sk-or-v1-TESTSECRET123"  # fake planted key; prepush-scan: allow


def chat_reply(content="", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


def tool_call(sql="SELECT 1", call_id="call_1"):
    return {"id": call_id, "type": "function",
            "function": {"name": "run_query", "arguments": json.dumps({"sql": sql})}}


class FakeClient:
    openrouter_calls = []
    qwen_calls = []
    openrouter_script = []
    openrouter_error = None

    def __init__(self, timeout=None, **kw):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        if "openrouter.ai" in url:
            FakeClient.openrouter_calls.append(
                {"url": url, "json": json, "headers": headers, "timeout": self.timeout})
            err = FakeClient.openrouter_error
            if err is not None:
                raise err
            status, body = FakeClient.openrouter_script.pop(0)
            return types.SimpleNamespace(status_code=status, text=f"echo {SECRET}", json=lambda: body)
        FakeClient.qwen_calls.append({"url": url, "json": json})
        return types.SimpleNamespace(status_code=200, text="", json=lambda: chat_reply("qwen answer"))


@pytest.fixture(autouse=True)
def mocked_http(monkeypatch):
    FakeClient.openrouter_calls = []
    FakeClient.qwen_calls = []
    FakeClient.openrouter_script = []
    FakeClient.openrouter_error = None
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openrouter")
    with patch("ask_agent.httpx", types.SimpleNamespace(AsyncClient=FakeClient)), \
         patch("ask_agent.track_usage", return_value=(1, -1)), \
         patch("ask_agent.log_question") as log, \
         patch("ask_agent._trigger_self_heal"):
        FakeClient.log = log
        yield


def ask(api_key=SECRET):
    return asyncio.run(aa.ask_agent("How busy is MK?", "u1", api_key=api_key))


def test_success_parses_response_and_logs_model():
    FakeClient.openrouter_script = [(200, chat_reply("It will be busy."))]
    assert ask() == "It will be busy."
    assert FakeClient.qwen_calls == []
    assert FakeClient.log.call_args.args[-1] == "anthropic/claude-sonnet-4.6"


def test_request_url_model_and_headers():
    FakeClient.openrouter_script = [(200, chat_reply("ok"))]
    ask()
    call = FakeClient.openrouter_calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["timeout"] == 60.0
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert call["headers"]["X-Title"] == "TPCR /ask"
    body = call["json"]
    assert body["model"] == aa.OPENROUTER_MODEL == "anthropic/claude-sonnet-4.6"
    assert body["max_tokens"] == 1500
    assert body["tools"] == aa.TOOLS
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1] == {"role": "user", "content": "How busy is MK?"}


def test_tool_call_round_then_final_answer():
    FakeClient.openrouter_script = [
        (200, chat_reply("", [tool_call("SELECT 42 AS x", "call_a")])),
        (200, chat_reply("The answer is 42.")),
    ]
    with patch("ask_agent.run_duckdb_query", return_value="x\n-\n42") as q:
        assert ask() == "The answer is 42."
    q.assert_called_once_with("SELECT 42 AS x")
    assert len(FakeClient.openrouter_calls) == 2
    second = FakeClient.openrouter_calls[1]["json"]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "call_a"
    assert second[-1] == {"role": "tool", "tool_call_id": "call_a", "content": "x\n-\n42"}
    assert FakeClient.qwen_calls == []


def test_anthropic_alias_uses_openrouter(monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    FakeClient.openrouter_script = [(200, chat_reply("via alias"))]
    assert ask() == "via alias"
    assert len(FakeClient.openrouter_calls) == 1


def test_no_key_falls_back_to_qwen():
    assert ask(api_key=None) == "qwen answer"
    assert FakeClient.openrouter_calls == []
    assert len(FakeClient.qwen_calls) == 1


def test_http_500_falls_back_to_qwen():
    FakeClient.openrouter_script = [(500, {})]
    assert ask() == "qwen answer"
    assert len(FakeClient.qwen_calls) == 1


def test_timeout_falls_back_to_qwen():
    FakeClient.openrouter_error = httpx.ReadTimeout("timed out")
    assert ask() == "qwen answer"
    assert len(FakeClient.qwen_calls) == 1


def test_empty_answer_falls_back_to_qwen():
    FakeClient.openrouter_script = [(200, chat_reply(""))]
    assert ask() == "qwen answer"


@pytest.mark.parametrize("mode", ["http_500", "http_401", "network_error"])
def test_planted_key_never_logged(mode, caplog):
    if mode == "network_error":
        FakeClient.openrouter_error = httpx.ConnectError(f"failed for {SECRET}")
    else:
        FakeClient.openrouter_script = [(int(mode.split("_")[1]), {})]
    with caplog.at_level(logging.DEBUG):
        assert ask() == "qwen answer"
    logged = caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert "TESTSECRET123" not in logged
    assert "OpenRouter /ask" in logged
    assert "echo" not in logged
