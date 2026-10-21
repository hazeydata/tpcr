"""Tests for /ask running against a local OpenAI-compatible Qwen server (no network)."""

import asyncio
import inspect
import json
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def chat_reply(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


def tool_call(sql="SELECT 1", call_id="call_1"):
    return {"id": call_id, "type": "function",
            "function": {"name": "run_query", "arguments": json.dumps({"sql": sql})}}


@pytest.fixture(autouse=True)
def no_side_effects():
    with patch("ask_agent.track_usage", return_value=(1, -1)), \
         patch("ask_agent.log_question"), \
         patch("ask_agent._trigger_self_heal"):
        yield


# ---------------------------------------------------------------- request shape

class FakeAsyncClient:
    calls = []

    def __init__(self, timeout=None, **kw):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        FakeAsyncClient.calls.append({"url": url, "json": json, "timeout": self.timeout})
        return types.SimpleNamespace(
            status_code=200, text="", json=lambda: chat_reply("hello")
        )


def test_request_shape():
    FakeAsyncClient.calls = []
    fake_httpx = types.SimpleNamespace(AsyncClient=FakeAsyncClient)
    with patch("ask_agent.httpx", fake_httpx), \
         patch("ask_agent.QWEN_BASE_URL", "http://qwen.test:9000/"), \
         patch("ask_agent.QWEN_MODEL", "test-model"), \
         patch("ask_agent.QWEN_TIMEOUT", 42.0):
        answer = run(aa.ask_agent("How busy is MK?", "u1"))

    assert answer == "hello"
    call = FakeAsyncClient.calls[0]
    assert call["url"] == "http://qwen.test:9000/v1/chat/completions"
    assert call["timeout"] == 42.0
    body = call["json"]
    assert body["model"] == "test-model"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1] == {"role": "user", "content": "How busy is MK?"}
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "run_query"
    assert "sql" in body["tools"][0]["function"]["parameters"]["properties"]
    assert "stream" not in body or body["stream"] is False
    assert body["max_tokens"] == 1500


@pytest.mark.skipif(any(k in os.environ for k in ("QWEN_BASE_URL", "QWEN_MODEL", "QWEN_TIMEOUT")),
                    reason="QWEN_* env overrides set")
def test_env_defaults():
    assert aa.QWEN_BASE_URL == "http://localhost:8080"
    assert aa.QWEN_MODEL == "Qwen3.8-27B-8bit"
    assert aa.QWEN_TIMEOUT == 300


def test_api_key_not_required():
    sig = inspect.signature(aa.ask_agent)
    assert sig.parameters["api_key"].default is None


# ---------------------------------------------------------------- tool loop

def test_tool_loop_appends_tool_message_and_finishes():
    payloads = []
    replies = [chat_reply(tool_calls=[tool_call("SELECT 42")]), chat_reply("The answer is 42")]

    async def post(payload):
        payloads.append(json.loads(json.dumps(payload)))
        return replies[len(payloads) - 1]

    with patch("ask_agent._post_chat", post), \
         patch("ask_agent.run_duckdb_query", return_value="42 rows") as q:
        answer = run(aa.ask_agent("q", "u"))

    assert answer == "The answer is 42"
    q.assert_called_once_with("SELECT 42")
    second = payloads[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"]
    assert second[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "42 rows"}


@pytest.mark.parametrize("sql", [
    "DROP TABLE wti",
    "INSERT INTO wti VALUES (1)",
    "UPDATE wti SET x = 1",
    "DELETE FROM wti",
    "CREATE TABLE t AS SELECT 1",
    "ATTACH 'other.db' AS other",
    "COPY wti TO 'out.csv'",
])
def test_non_select_rejected(sql):
    payloads = []
    replies = [chat_reply(tool_calls=[tool_call(sql)]), chat_reply("ok")]

    async def post(payload):
        payloads.append(json.loads(json.dumps(payload)))
        return replies[len(payloads) - 1]

    with patch("ask_agent._post_chat", post), patch("ask_agent.run_duckdb_query") as q:
        run(aa.ask_agent("q", "u"))
    q.assert_not_called()
    assert "Only SELECT" in payloads[1]["messages"][-1]["content"]


def test_max_five_rounds():
    post = AsyncMock(side_effect=lambda p: chat_reply(tool_calls=[tool_call()]))
    with patch("ask_agent._post_chat", post), patch("ask_agent.run_duckdb_query", return_value="x"):
        answer = run(aa.ask_agent("q", "u"))
    assert post.await_count == 5
    assert "wasn't able to fully answer" in answer


def test_retry_without_tools_on_4xx():
    payloads = []

    async def post(payload):
        payloads.append(json.loads(json.dumps(payload)))
        if len(payloads) == 1:
            raise aa.QwenHTTPError(400, "tools are not supported")
        return chat_reply("plain answer")

    with patch("ask_agent._post_chat", post):
        answer = run(aa.ask_agent("q", "u"))
    assert answer == "plain answer"
    assert "tools" in payloads[0] and "tools" not in payloads[1]


def test_4xx_retries_exactly_once_without_tools():
    payloads = []

    async def post(payload):
        payloads.append(json.loads(json.dumps(payload)))
        if len(payloads) == 1:
            raise aa.QwenHTTPError(400, "bad request")
        return chat_reply("recovered")

    with patch("ask_agent._post_chat", post):
        answer = run(aa.ask_agent("q", "u"))
    assert answer == "recovered"
    assert len(payloads) == 2
    assert "tools" in payloads[0]
    assert "tools" not in payloads[1]


def test_no_retry_on_server_error_without_tools_hint():
    post = AsyncMock(side_effect=aa.QwenHTTPError(500, "boom"))
    with patch("ask_agent._post_chat", post):
        with pytest.raises(aa.QwenHTTPError):
            run(aa.ask_agent("q", "u"))
    assert post.await_count == 1


def test_think_blocks_stripped():
    post = AsyncMock(return_value=chat_reply("<think>secret\nreasoning</think>\nFinal answer"))
    with patch("ask_agent._post_chat", post):
        assert run(aa.ask_agent("q", "u")) == "Final answer"
    assert aa.strip_think("a<think>x</think>b<think>unterminated") == "ab"


# ---------------------------------------------------------------- 32K cap

LIMIT = aa.CONTEXT_LIMIT


def test_limits_are_literal_values():
    assert aa.CONTEXT_LIMIT == 32768
    assert aa.MAX_TOKENS == 1500
    params = inspect.signature(aa.trim_messages).parameters
    assert params["limit"].default == 32768
    assert params["max_tokens"].default == 1500


def total(msgs, tools=True):
    return aa.estimate_messages_tokens(msgs) + aa.MAX_TOKENS + (aa._TOOLS_RESERVE if tools else 0)


def test_estimator_is_conservative():
    assert aa.estimate_tokens("a" * 300) == 100
    assert aa.estimate_messages_tokens([{"role": "user", "content": "a" * 300}]) > 100


def test_small_conversation_untouched():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert aa.trim_messages(msgs) == msgs


def test_drops_oldest_history_first():
    big = "x" * 30000
    msgs = [{"role": "system", "content": "SYS"}]
    for i in range(8):
        msgs.append({"role": "user", "content": f"old{i} " + big})
        msgs.append({"role": "assistant", "content": f"a{i} " + big})
    msgs.append({"role": "user", "content": "LATEST"})
    out = aa.trim_messages(msgs)
    assert total(out) <= LIMIT
    assert out[0]["content"] == "SYS"
    assert out[-1]["content"] == "LATEST"
    kept = [m["content"][:5] for m in out[1:-1]]
    assert "old0 " not in kept            # oldest gone
    assert any(m["content"].startswith("a7") for m in out)  # newest history survives


def test_tool_group_dropped_without_orphans():
    big = "y" * 60000
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [tool_call()]},
        {"role": "tool", "tool_call_id": "call_1", "content": big},
        {"role": "assistant", "content": "", "tool_calls": [tool_call(call_id="call_2")]},
        {"role": "tool", "tool_call_id": "call_2", "content": "small"},
    ]
    out = aa.trim_messages(msgs)
    assert total(out) <= LIMIT
    ids_called = {c["id"] for m in out for c in m.get("tool_calls", [])}
    for m in out:
        if m["role"] == "tool":
            assert m["tool_call_id"] in ids_called


def test_truncates_oversized_tool_result_and_prompt():
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "z" * 200000},
        {"role": "assistant", "content": "", "tool_calls": [tool_call()]},
        {"role": "tool", "tool_call_id": "call_1", "content": "t" * 200000},
    ]
    out = aa.trim_messages(msgs)
    assert total(out) <= LIMIT
    assert out[0]["role"] == "system"
    assert any(m["role"] == "user" for m in out)

    huge_prompt = [{"role": "system", "content": "s" * 300000}, {"role": "user", "content": "u" * 300000}]
    out = aa.trim_messages(huge_prompt)
    assert total(out) <= LIMIT
    assert [m["role"] for m in out] == ["system", "user"]


def test_trim_applied_before_every_request():
    payloads = []
    replies = [chat_reply(tool_calls=[tool_call()]), chat_reply("done")]

    async def post(payload):
        payloads.append(payload)
        return replies[len(payloads) - 1]

    with patch("ask_agent._post_chat", post), \
         patch("ask_agent.run_duckdb_query", return_value="r" * 400000):
        run(aa.ask_agent("q" * 150000, "u"))

    assert len(payloads) == 2
    for p in payloads:
        assert aa.estimate_messages_tokens(p["messages"]) + p["max_tokens"] <= LIMIT


# ---------------------------------------------------------------- bot /ask

class FakeUser:
    id = 123
    display_avatar = types.SimpleNamespace(url="http://avatar.test/a.png")

    def __str__(self):
        return "tester"


class _Any:
    """Permissive stand-in for a missing third-party package attribute."""

    def __call__(self, *a, **k):
        if len(a) == 1 and not k and callable(a[0]):
            return a[0]  # used as a decorator
        return _Any()

    def __getattr__(self, name):
        return _Any()

    def __getitem__(self, item):
        return _Any()


def _stub_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: _Any()
    return mod


def _build_discord_stub():
    d = _stub_module("discord")

    class NotFound(Exception):
        pass

    class Embed:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        def set_author(self, **kw):
            self.author = kw

        def set_footer(self, **kw):
            self.footer = kw

        def __getattr__(self, name):
            return lambda *a, **k: None

    class View:
        def __init__(self, *a, **k):
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    ui = _stub_module("discord.ui")
    ui.View = View
    d.ui = ui
    d.Embed = Embed
    d.Interaction = object
    d.errors = types.SimpleNamespace(NotFound=NotFound)
    d.app_commands = _stub_module("discord.app_commands")
    d.ext = _stub_module("discord.ext")
    return {"discord": d, "discord.ui": ui, "discord.app_commands": d.app_commands,
            "discord.ext": d.ext, "discord.ext.commands": _stub_module("discord.ext.commands"),
            "discord.ext.tasks": _stub_module("discord.ext.tasks")}


@pytest.fixture(scope="module")
def bot():
    stubs = {}
    try:
        import discord  # noqa: F401
    except ImportError:
        stubs.update(_build_discord_stub())
    for name in ("playwright", "playwright.sync_api", "dotenv"):
        try:
            __import__(name)
        except ImportError:
            m = _stub_module(name)
            m.load_dotenv = lambda *a, **k: None
            stubs[name] = m
    # Pre-import heavy C-extension deps outside patch.dict so restoring
    # sys.modules afterwards does not unload them for later tests.
    for name in ("numpy", "pandas", "matplotlib"):
        try:
            __import__(name)
        except ImportError:
            pass
    with patch.dict(sys.modules, stubs):
        sys.modules.pop("bot", None)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_BOT_TOKEN", None)
            import bot as bot_module
        yield bot_module
        sys.modules.pop("bot", None)


def make_interaction(events, edit_raises=False):
    placeholder = MagicMock()

    async def edit(**kw):
        events.append(("edit", kw))
        if edit_raises:
            raise RuntimeError("edit failed: secret-internal-detail")

    placeholder.edit = edit

    async def defer(*a, **k):
        events.append(("defer", k))

    async def send(*a, **k):
        events.append(("send", a, k))
        return placeholder

    interaction = MagicMock()
    interaction.user = FakeUser()
    interaction.response.defer = defer
    interaction.followup.send = send
    return interaction


def call_ask(bot, interaction, question="How busy is MK?"):
    fn = getattr(bot.ask_command, "callback", bot.ask_command)
    run(fn(interaction, question))


def user_visible(events):
    out = []
    for ev in events:
        if ev[0] == "send":
            out += [str(x) for x in ev[1]] + [str(v) for v in ev[2].values()]
        elif ev[0] == "edit":
            out += [str(v) for v in ev[1].values()]
    return " ".join(out)


def test_ask_acks_then_placeholder_then_edit(bot):
    events = []
    interaction = make_interaction(events)
    with patch.object(bot, "ask_agent", AsyncMock(return_value="It will be busy.")) as agent:
        call_ask(bot, interaction)

    assert [e[0] for e in events] == ["defer", "send", "edit"]
    assert events[1][1] == ("Thinking…",)
    edit_kw = events[2][1]
    assert edit_kw["embed"].description == "It will be busy."
    agent.assert_awaited_once()
    # no api key passed
    assert len(agent.await_args.args) == 2


def test_ask_falls_back_to_followup_send_if_edit_fails(bot):
    events = []
    interaction = make_interaction(events, edit_raises=True)
    with patch.object(bot, "ask_agent", AsyncMock(return_value="Answer")):
        call_ask(bot, interaction)

    kinds = [e[0] for e in events]
    assert kinds == ["defer", "send", "edit", "send"]
    assert events[3][2]["embed"].description == "Answer"
    assert "secret-internal-detail" not in user_visible(events)


@pytest.mark.parametrize("exc", [
    asyncio.TimeoutError("read timed out at 127.0.0.1:8080"),
    ConnectionError("connection refused 127.0.0.1:8080"),
    aa.QwenHTTPError(500, "Traceback: secret stack"),
    RuntimeError("secret-internal-detail"),
])
def test_errors_show_only_friendly_message(bot, exc, caplog):
    events = []
    interaction = make_interaction(events)
    with patch.object(bot, "ask_agent", AsyncMock(side_effect=exc)):
        with caplog.at_level("ERROR"):
            call_ask(bot, interaction)

    visible = user_visible(events)
    assert bot.FRIENDLY_ERROR == "Sorry, I couldn't answer that right now. Please try again in a bit."
    assert bot.FRIENDLY_ERROR in visible
    for leak in ("secret", "127.0.0.1", "timed out", "refused", "Traceback", "HTTP 500"):
        assert leak not in visible
    # full details only in server logs
    assert any(r.exc_info for r in caplog.records)


def test_real_timeout_end_to_end_is_friendly(bot):
    """ask_agent with a timing-out HTTP call -> friendly message only."""
    events = []
    interaction = make_interaction(events)
    post = AsyncMock(side_effect=asyncio.TimeoutError("raw timeout text"))
    with patch("ask_agent._post_chat", post):
        call_ask(bot, interaction)
    visible = user_visible(events)
    assert bot.FRIENDLY_ERROR in visible
    assert "raw timeout text" not in visible


def test_ask_never_touches_anthropic(bot):
    class Boom(types.ModuleType):
        def __getattr__(self, name):
            raise AssertionError(f"anthropic.{name} used by /ask")

    events = []
    interaction = make_interaction(events)
    post = AsyncMock(return_value=chat_reply("Fine day"))
    with patch.dict(sys.modules, {"anthropic": Boom("anthropic")}), \
         patch.object(bot, "ANTHROPIC_API_KEY", None), \
         patch("ask_agent._post_chat", post):
        call_ask(bot, interaction)

    assert events[-1][0] == "edit"
    assert events[-1][1]["embed"].description == "Fine day"
    src = inspect.getsource(getattr(bot.ask_command, "callback", bot.ask_command))
    assert "ANTHROPIC" not in src and "anthropic" not in src
    assert "anthropic" not in inspect.getsource(aa)
