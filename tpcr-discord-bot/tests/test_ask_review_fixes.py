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

_REAL_LOG_QUESTION = aa.log_question

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


# 5a/5b: conservative for tool parsing, balanced-only for the visible answer
def test_ac26_stray_close_keeps_text():
    assert aa.strip_think("The answer is 42 minutes.</think>") == "The answer is 42 minutes."
    assert aa.strip_think("<think>t</think>The answer is 42 minutes.</think>") == "The answer is 42 minutes."
    assert aa.strip_think("Intro. <think>t</think>Real answer 42.</think> tail") == "Intro. Real answer 42. tail"
    answer, q = _run_text("The answer is 42 minutes.</think>")
    assert answer == "The answer is 42 minutes."


def test_tool_parse_conservative_for_stray_and_nested():
    for text in (f"a<think>b</think>{CALL}</think>", f"x{CALL}</think>",
                 f"<think>a<think>b</think>{CALL}</think>", f"ok<think>{CALL}", f"<Think>{CALL}</THINK>"):
        assert aa.parse_text_tool_calls(aa.strip_think_for_tools(text)) == [], text
        _, q = _run_text(text)
        q.assert_not_called()


def test_tool_call_after_closed_think_still_runs():
    assert aa.parse_text_tool_calls(aa.strip_think_for_tools(f"<think>t</think>{CALL}"))


def test_ac11_unmatched_open_reply():
    text = 'Ok<think><tool_call>{"name":"run_query","arguments":{"sql":"SELECT 1"}}</tool_call>'
    answer, q = _run_text(text)
    q.assert_not_called()
    assert answer == "Ok"


# 2. case-insensitive
def test_strip_think_uppercase():
    assert aa.strip_think(f"<THINK>{CALL}</Think>Done") == "Done"
    answer, q = _run_text(f"<THINK>{CALL}</THINK>Done")
    q.assert_not_called()
    assert answer == "Done"


# 3. invalid provider
def test_invalid_provider_fails_at_startup(monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openai")
    with pytest.raises(RuntimeError, match="anthropic or qwen") as ei:
        aa.validate_config()
    assert "'openai'" in str(ei.value)
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


# AC4: no-data message only when rounds exhausted, final answer empty, >=1 SELECT ran, all empty
def _ac4(final_content, results, sqls=None):
    sqls = list(sqls or ["SELECT 1"] * 5)
    calls = iter(sqls)

    async def chat(messages, use_tools):
        if use_tools:
            return chat_reply(tool_calls=[{"id": "c", "type": "function", "function": {
                "name": "run_query", "arguments": json.dumps({"sql": next(calls)})}}])
        if isinstance(final_content, Exception):
            raise final_content
        return chat_reply(final_content)

    res = iter(results)

    def query(sql, *a, **k):
        if "min(d)" in sql:
            return "min_date | max_date\n-----\n2026-10-21 | 2026-10-22"
        return next(res)

    with patch("ask_agent._chat", AsyncMock(side_effect=chat)), \
         patch("ask_agent.run_duckdb_query", side_effect=query) as q:
        return asyncio.run(aa.ask_agent("q", "u")), q


def test_ac4b_nonempty_final_answer_is_kept():
    answer, _ = _ac4("Sorry, nothing found.", [aa.NO_RESULTS_MSG] * 5)
    assert answer == "Sorry, nothing found."


def test_ac4_all_empty_and_final_empty_gives_no_data_range():
    answer, _ = _ac4("", [aa.NO_RESULTS_MSG] * 5)
    assert "don't have any wait data" in answer and "2026-10-21" in answer


def test_ac4b_final_call_raising_gives_no_data():
    answer, _ = _ac4(RuntimeError("boom"), [aa.NO_RESULTS_MSG] * 5)
    assert "don't have any wait data" in answer


def test_ac4d_one_nonempty_result_is_not_no_data():
    # kills the all->any mutant
    answer, _ = _ac4("", [aa.NO_RESULTS_MSG] * 4 + ["a | b\n---\n1 | 2"])
    assert "don't have any wait data" not in answer
    assert "wasn't able to fully answer" in answer


def test_ac4c_no_select_ran_is_not_no_data():
    # kills the drop-"a SELECT ran" mutant: every query refused as non-SELECT
    answer, q = _ac4("", [], sqls=["DELETE FROM wti"] * 5)
    q.assert_not_called()
    assert "don't have any wait data" not in answer
    assert "wasn't able to fully answer" in answer


# AC18: non-SELECT refused (Qwen and Claude)
def test_ac18_qwen_delete_refused():
    answer, q = _ac4("done", [], sqls=["DELETE FROM wti", "DROP TABLE wti", "UPDATE wti SET wti=1",
                                       "INSERT INTO wti VALUES (1)", "DELETE FROM x"])
    q.assert_not_called()


def test_ac18_claude_delete_refused(monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    sent = []
    blocks = [
        [types.SimpleNamespace(type="tool_use", id="t1", input={"sql": "DELETE FROM x"})],
        [types.SimpleNamespace(type="text", text="ok")],
    ]

    class Msgs:
        async def create(self, **kw):
            sent.append(kw["messages"][-1])
            return types.SimpleNamespace(content=blocks.pop(0))

    fake = types.ModuleType("anthropic")
    fake.AsyncAnthropic = lambda **kw: types.SimpleNamespace(messages=Msgs())
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    with patch("ask_agent.run_duckdb_query") as q:
        assert asyncio.run(aa.ask_agent("q", "u", api_key="sk-test")) == "ok"
    q.assert_not_called()
    assert sent[-1]["content"][0]["content"] == "Error: Only SELECT queries are allowed."


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


# AC15/AC24/AC27: fallback logs the class (and status for auth) only; no secret anywhere
SECRET = "sk-ant-TESTSECRET123"  # fake key mandated by spec AC24; prepush-scan: allow


def _planted_errors():
    import httpx
    try:
        import anthropic
    except ImportError:  # stand-ins with the same names/attributes
        anthropic = None
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    errs = [ConnectionError(f"refused for key {SECRET}")]
    if anthropic is not None:
        errs.append(anthropic.APIConnectionError(message=f"conn failed {SECRET}", request=req))
        for cls, code in ((anthropic.AuthenticationError, 401), (anthropic.PermissionDeniedError, 403),
                          (anthropic.InternalServerError, 500)):
            errs.append(cls(f"bad x-api-key {SECRET}", response=httpx.Response(code, request=req),
                            body={"error": SECRET}))
    else:
        for name, code in (("APIConnectionError", None), ("AuthenticationError", 401),
                           ("PermissionDeniedError", 403), ("InternalServerError", 500)):
            e = type(name, (Exception,), {})(f"bad x-api-key {SECRET}")
            if code:
                e.status_code = code
            errs.append(e)
    return errs


@pytest.mark.parametrize("err", _planted_errors(), ids=lambda e: type(e).__name__)
def test_ac24_planted_key_never_logged_or_replied(err, monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    qlog = tmp_path / "q.jsonl"
    monkeypatch.setenv("TPCR_ASK_QUESTION_LOG", str(qlog))
    with patch("ask_agent._claude_loop", AsyncMock(side_effect=err)), \
         patch("ask_agent._chat", AsyncMock(return_value=chat_reply("qwen answer"))), \
         patch("ask_agent.log_question", _REAL_LOG_QUESTION), \
         caplog.at_level(logging.DEBUG):
        answer = asyncio.run(aa.ask_agent("q", "u", api_key=SECRET))
    assert answer == "qwen answer"
    logged = caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert qlog.exists()
    stored = qlog.read_text()
    for blob in (logged, stored, answer):
        assert "TESTSECRET123" not in blob and "sk-ant-" not in blob
    assert type(err).__name__ in caplog.text


@pytest.mark.parametrize("err", [e for e in _planted_errors() if getattr(e, "status_code", None) in (401, 403)],
                         ids=lambda e: type(e).__name__)
def test_ac27_auth_errors_log_error_with_class_and_status_only(err, monkeypatch, caplog):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    with patch("ask_agent._claude_loop", AsyncMock(side_effect=err)), \
         patch("ask_agent._chat", AsyncMock(return_value=chat_reply("qwen answer"))), \
         caplog.at_level(logging.DEBUG, logger=aa.logger.name):
        asyncio.run(aa.ask_agent("q", "u", api_key="sk-test"))
    recs = [r for r in caplog.records if "Claude" in r.getMessage()]
    assert recs and all(r.levelno == logging.ERROR for r in recs)
    msg = recs[0].getMessage()
    assert type(err).__name__ in msg and str(err.status_code) in msg
    assert "x-api-key" not in msg and "TESTSECRET123" not in msg


def test_ac15_non_auth_errors_warn_with_class_only(monkeypatch, caplog):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    for err in (asyncio.TimeoutError(), KeyError("leaky-detail"), ConnectionError("leaky-detail")):
        caplog.clear()
        with patch("ask_agent._claude_loop", AsyncMock(side_effect=err)), \
             patch("ask_agent._chat", AsyncMock(return_value=chat_reply("qwen answer"))), \
             caplog.at_level(logging.DEBUG, logger=aa.logger.name):
            assert asyncio.run(aa.ask_agent("q", "u", api_key="sk-test")) == "qwen answer"
        rec = [r for r in caplog.records if "Claude" in r.getMessage()][0]
        assert rec.levelno == logging.WARNING
        assert rec.getMessage() == f"Claude /ask failed ({type(err).__name__}); falling back to Qwen"


@pytest.mark.parametrize("mode", ["claude_success", "no_key", "qwen_only"])
def test_ac24_other_paths_never_leak_key(mode, monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "qwen" if mode == "qwen_only" else "anthropic")
    qlog = tmp_path / "q.jsonl"
    monkeypatch.setenv("TPCR_ASK_QUESTION_LOG", str(qlog))
    key = None if mode == "no_key" else SECRET
    with patch("ask_agent._claude_loop", AsyncMock(return_value=("claude answer", 1))), \
         patch("ask_agent._chat", AsyncMock(return_value=chat_reply("qwen answer"))), \
         patch("ask_agent.log_question", _REAL_LOG_QUESTION), caplog.at_level(logging.DEBUG):
        answer = asyncio.run(aa.ask_agent("q", "u", api_key=key))
    for blob in (caplog.text, qlog.read_text(), answer):
        assert "TESTSECRET123" not in blob and "sk-ant-" not in blob


def test_ac24_key_never_passed_to_logger_or_log_question():
    import ast
    src = open(os.path.join(os.path.dirname(__file__), "..", "ask_agent.py")).read()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        is_log = (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "logger") or \
                 (isinstance(f, ast.Name) and f.id == "log_question") or \
                 any(isinstance(a, ast.Name) and a.id == "log_question" for a in node.args)
        if is_log:
            names = {n.id for a in node.args + [k.value for k in node.keywords] for n in ast.walk(a)
                     if isinstance(n, ast.Name)}
            assert not names & {"api_key", "ANTHROPIC_API_KEY"}, ast.unparse(node)


@pytest.mark.parametrize("claude_result", [(None, 5), ("", 2)])
def test_ac16_claude_no_text_falls_back_to_qwen(claude_result, monkeypatch):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    with patch("ask_agent._claude_loop", AsyncMock(return_value=claude_result)), \
         patch("ask_agent._chat", AsyncMock(return_value=chat_reply("qwen answer"))):
        assert asyncio.run(aa.ask_agent("q", "u", api_key="sk-test")) == "qwen answer"


def test_ac7_range_lookup_error_gives_first_sentence_and_warns(caplog):
    async def chat(messages, use_tools):
        if use_tools:
            return chat_reply(tool_calls=[{"id": "c", "type": "function", "function": {
                "name": "run_query", "arguments": json.dumps({"sql": "SELECT 1"})}}])
        return chat_reply("")

    def query(sql, *a, **k):
        if "min(d)" in sql:
            raise RuntimeError("range broke")
        return aa.NO_RESULTS_MSG

    with patch("ask_agent._chat", AsyncMock(side_effect=chat)), \
         patch("ask_agent.run_duckdb_query", side_effect=query), \
         caplog.at_level(logging.WARNING, logger=aa.logger.name):
        answer = asyncio.run(aa.ask_agent("q", "u"))
    assert answer == "I don't have any wait data for that date."
    assert "date range lookup failed" in caplog.text


def test_ac21_no_mnt_data_literals():
    here = os.path.join(os.path.dirname(__file__), "..")
    for name in ("ask_agent.py", "bot.py", "calendar_image.py"):
        assert "/mnt/data" not in open(os.path.join(here, name)).read(), name
