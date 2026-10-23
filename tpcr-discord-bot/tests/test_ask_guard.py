"""SQL guard and secret redaction for /ask tool calls (Qwen and OpenRouter paths)."""

import asyncio
import json
import os
import sys
import types
from unittest.mock import patch

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402

SECRET = "sk-ant-TESTSECRET123"  # fake planted key; prepush-scan: allow
KEY = SECRET + "456789ABCDEFG"

BYPASS_SQL = [
    "SELECT E'\\''; DROP TABLE t; -- '",
    "SELECT 1 /* /* */ '; */ ; DROP TABLE t; --'",
]


def chat_reply(content="", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


def tool_call(sql, call_id="tu1"):
    return {"id": call_id, "type": "function",
            "function": {"name": "run_query", "arguments": json.dumps({"sql": sql})}}


class FakeResp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def run_qwen(sql, monkeypatch):
    replies = [chat_reply("", [tool_call(sql)]), chat_reply("done")]
    seen = []

    def post(url, json=None, **kw):
        seen.append([dict(m) for m in json["messages"]])
        return FakeResp(replies.pop(0))

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    answer = aa._qwen_loop("system prompt", "q")
    return answer, seen


def run_openrouter(sql, monkeypatch):
    replies = [chat_reply("", [tool_call(sql)]), chat_reply("done")]
    seen = []

    async def post(api_key, payload):
        seen.append([dict(m) for m in payload["messages"]])
        return replies.pop(0)

    monkeypatch.setattr(aa, "_post_openrouter", post)
    answer = asyncio.run(aa._openrouter_loop("k", "system prompt", "q"))
    return answer, seen


RUNNERS = [run_qwen, run_openrouter]


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("sql,expect", [
    ("SELECT 1; DROP TABLE wti", aa.MULTI_STATEMENT_MSG),
    ("SELECT 1; SELECT 2", aa.MULTI_STATEMENT_MSG),
    ("SELECT 1;;", aa.MULTI_STATEMENT_MSG),
    ("DROP TABLE wti", aa.NON_SELECT_MSG),
    ("SELECT 'a;b' FROM wti;", None),
    ("SELECT 1;", None),
])
def test_guard_rejects_before_execution_with_clear_error(runner, sql, expect, monkeypatch):
    ran = []
    monkeypatch.setattr(aa, "run_duckdb_query", lambda q: ran.append(q) or "x\n-\n1")
    assert runner(sql, monkeypatch)[0] == "done"
    if expect:
        assert ran == []
    else:
        assert ran == [sql]


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("sql", BYPASS_SQL)
def test_parser_guard_bypass_never_reaches_duckdb(runner, sql, monkeypatch):
    ran = []
    monkeypatch.setattr(aa, "run_duckdb_query", lambda q: ran.append(q) or "x")
    monkeypatch.setattr(aa.duckdb, "connect", lambda *a, **k: pytest.fail("connected to DuckDB"))
    answer, seen = runner(sql, monkeypatch)
    assert answer == "done"
    assert ran == []
    assert [m for m in seen[1] if m["role"] == "tool"][0]["content"] == aa.MULTI_STATEMENT_MSG


@pytest.mark.parametrize("sql,ok", [
    ("SELECT 1", True), ("select 1;", True), ("SELECT 1;;", False), ("SELECT 'a;b' AS x;", True),
    ("WITH a AS (SELECT 1) SELECT * FROM a", True), ("SELECT ';' AS x", True),
    ("SELECT 1 -- ; comment", True), ("SELECT 1; DROP TABLE x", False),
    ("SELECT 1;ATTACH 'x.db'", False), ("DROP TABLE x", False), ("", False),
    ("SELECT 1; SELECT 2", False),
])
def test_single_select_guard(sql, ok):
    assert aa.is_single_select(sql) is ok


@pytest.mark.parametrize("sql", BYPASS_SQL)
def test_parser_guard_rejects_bypass(sql):
    assert aa.sql_guard_error(sql) is not None


@pytest.mark.parametrize("sql", [
    "SELECT 1 -- it's\n; DROP TABLE t; SELECT 'a'",
    "SELECT 1 /* it's */; DROP TABLE t; SELECT 'a'",
    "SELECT $$'$$; DROP TABLE t; SELECT '$$'",
    "SELECT $q$'$q$; DROP TABLE t; SELECT '$q$'",
    "SELECT \"it's\"; DROP TABLE t; SELECT 'a'",
    "SELECT 'unterminated; DROP TABLE t",
    "SELECT 1 /* unterminated; DROP TABLE t",
    "SELECT $tag$a;b$tag$; DROP TABLE t",
    "SELECT 'a''b'; DROP TABLE t",
    "SELECT U&'a'; DROP TABLE t",
    "SELECT 1;;",
    "SELECT 1;\rDROP TABLE t",
    "SELECT 1;\r\nDROP TABLE t",
])
def test_parser_guard_rejects(sql):
    assert aa.sql_guard_error(sql) is not None


@pytest.mark.parametrize("sql", [
    "SELECT 1;",
    "SELECT 'it''s; x'",
    "SELECT \"a;b\" FROM t;",
    "SELECT $$a;b$$",
    "SELECT $t$a;'b$t$;",
    "SELECT $tag$a;b$tag$",
    "SELECT U&'a;b'",
    "SELECT 1 /* ; */ -- ;\n",
    "WITH a AS (SELECT 1) SELECT * FROM a;",
])
def test_parser_guard_allows(sql):
    assert aa.sql_guard_error(sql) is None


def test_guard_rejects_non_select_statement_type():
    assert aa.sql_guard_error("SELECT 1; ATTACH 'x.db'") == aa.MULTI_STATEMENT_MSG
    assert aa.sql_guard_error("DELETE FROM t") == aa.NON_SELECT_MSG


def test_kitty_multi_statement_after_comment_and_literal():
    assert aa.sql_guard_error("SELECT 1 -- it's\n; DROP TABLE t; SELECT 'a'") == aa.MULTI_STATEMENT_MSG
    assert aa.sql_guard_error("SELECT 'a;b'; DROP TABLE t") == aa.MULTI_STATEMENT_MSG
    assert aa.sql_guard_error("  select 1 ; ") is None


def test_redact_caps_length_at_300():
    assert len(aa.redact("a" * 1000)) == 300


def test_redact_key_straddling_cap_leaves_no_fragment():
    out = aa.redact("x" * 290 + KEY + "tail")
    assert len(out) <= 300
    assert "TESTSECRET" not in out and "sk-ant" not in out
    assert not any(KEY[i:i + 4] in out for i in range(len(KEY) - 3))


def test_redact_runs_before_truncation():
    out = aa.redact("x" * 295 + KEY)
    assert out == "x" * 295 + "[REDA"
    assert "sk-a" not in out


@pytest.mark.parametrize("text", [
    "SK-ANT-API03-ABCDEFGHIJ",
    "Sk-ant-api03-ABCDEFGHIJ",
    "SK-ABCDEFGHIJKLMNOPQRSTUV",
    "Sk-abcdefghijklmnopqrstuv",
    "X-API-KEY: opaqueTOKEN999",
    "api_key=opaqueTOKEN999",
    "API_KEY: opaqueTOKEN999",
    "api-key = opaqueTOKEN999",
    '"api_key": "opaqueTOKEN999"',
    "BEARER abc.DEF-123_xyz",
    "Bearer abc.DEF-123_xyz",
    "bearer " + "abcdefgh" * 2,
    "sk\\u002dant\\u002dapi03\\u002dABCDEFGHIJ",
])
def test_redact_case_insensitive_and_api_key_forms(text):
    out = aa.redact(f"col {text} not found")
    assert "[REDACTED]" in out
    for tok in ("ABCDEFGHIJ", "opaqueTOKEN999", "abc.DEF", "abcdefghijklmnop", "KLMNOPQRSTUV", "klmnopqrstuv"):
        assert tok not in out


@pytest.mark.parametrize("text", [
    "column bearer not found",
    "Referenced column Bearer does not exist",
    "column bearer authorization missing",
])
def test_redact_leaves_plain_bearer_word(text):
    assert aa.redact(text) == text


@pytest.mark.parametrize("provider", ["openrouter", "qwen"])
def test_planted_key_in_exception_never_in_logs(provider, monkeypatch, capsys, caplog):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", provider)

    async def post(api_key, payload):
        raise RuntimeError(f"connect failed for Bearer {KEY} api_key={KEY}")

    def qwen_post(*a, **k):
        raise RuntimeError(f"qwen failed {KEY}")

    monkeypatch.setattr(aa, "_post_openrouter", post)
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=qwen_post))
    with patch("ask_agent.track_usage", return_value=(1, -1)), \
         patch("ask_agent.log_question"), patch("ask_agent._trigger_self_heal"):
        answer = asyncio.run(aa.ask_agent("q", "u", api_key=KEY))
    assert "wasn't able to fully answer" in answer
    out = capsys.readouterr()
    logged = out.out + out.err + caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert "TESTSECRET123" not in logged and "456789ABCDEFG" not in logged
    assert "[REDACTED]" in logged
