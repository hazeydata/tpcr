"""Post-deploy /ask fixes: Claude payload validity, safe 400 logging, prompt/schema
consistency, run_query retry cap, single-statement guard, final no-tools log line."""

import asyncio
import json
import logging
import os
import re
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import duckdb
import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ask_agent as aa  # noqa: E402

SECRET = "sk-ant-TESTSECRET123"  # fake planted key; prepush-scan: allow
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def chat_reply(content="", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


# ---------- (1) Claude payload ----------

def _validate_openrouter_request(payload):
    assert payload["model"] == "anthropic/claude-sonnet-4.6"
    assert isinstance(payload["max_tokens"], int) and 0 < payload["max_tokens"] <= 64000
    for t in payload["tools"]:
        assert t["type"] == "function"
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["function"]["name"])
        assert t["function"]["parameters"]["type"] == "object"
    msgs = payload["messages"]
    assert msgs[0]["role"] == "system" and msgs[0]["content"].strip()
    assert msgs[1]["role"] == "user"
    pending = set()
    for m in msgs[2:]:
        if m["role"] == "assistant":
            assert not pending
            pending = {c["id"] for c in m["tool_calls"]}
        else:
            assert m["role"] == "tool" and m["tool_call_id"] in pending
            assert isinstance(m["content"], str) and m["content"]
            pending.discard(m["tool_call_id"])


def test_openrouter_payload_valid_across_tool_rounds(monkeypatch):
    calls = []

    def call(cid, sql):
        return {"id": cid, "type": "function", "function": {"name": "run_query", "arguments": json.dumps({"sql": sql})}}

    replies = [
        chat_reply(tool_calls=[call("tu1", "SELECT 1")]),
        chat_reply(tool_calls=[call("tu2", "SELECT 2"), call("tu3", "DROP TABLE x")]),
        chat_reply("Seven Dwarfs averaged 51 min."),
    ]

    async def post(api_key, payload):
        calls.append(json.loads(json.dumps(payload)))
        _validate_openrouter_request(calls[-1])
        return replies[len(calls) - 1]

    monkeypatch.setattr(aa, "_post_openrouter", post)
    monkeypatch.setattr(aa, "run_duckdb_query", lambda sql: "x\n-\n1")
    answer, rounds = asyncio.run(aa._openrouter_loop("k", "system prompt", "q"))
    assert answer == "Seven Dwarfs averaged 51 min." and rounds == 3
    assert len(calls) == 3


def test_requirements_do_not_pin_anthropic_sdk():
    req = open(os.path.join(ROOT, "requirements.txt")).read()
    assert not re.search(r"^anthropic", req, re.M)


@pytest.mark.parametrize("code", [400, 429])
def test_ac27_http_error_logs_class_and_status_only(code, monkeypatch, caplog):
    """AC27/AC24: class name + HTTP status only; no message text, no key."""
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "openrouter")
    err = aa.OpenRouterHTTPError(code)
    with patch("ask_agent._openrouter_loop", AsyncMock(side_effect=err)), \
         patch("ask_agent._chat", AsyncMock(return_value=chat_reply("qwen answer"))), \
         caplog.at_level(logging.DEBUG):
        assert asyncio.run(aa.ask_agent("q", "u", api_key=SECRET)) == "qwen answer"
    lines = [r.getMessage() for r in caplog.records if "OpenRouter /ask failed" in r.getMessage()]
    assert lines == [f"OpenRouter /ask failed (OpenRouterHTTPError, status {code}); falling back to Qwen"]
    assert "TESTSECRET123" not in caplog.text
    for fragment in ("credit", "content blocks", "invalid_request_error", "Error code"):
        assert fragment not in caplog.text


# ---------- (2) prompt vs schema ----------

KNOWN_SCHEMA = {
    "wti": {"park_code", "park_date", "time_slot", "wti", "source", "updated_at"},
    "forecasts": {"entity_code", "park_date", "time_slot", "predicted_actual", "predicted_posted",
                  "model_version", "prediction_method"},
    "entities": {"entity_code", "entity_name", "short_name", "park_code", "property_code", "category",
                 "has_wait_times", "has_posted", "wait_time_type", "is_extinct", "updated_at"},
    "live_waits": {"entity_code", "observed_at", "wait_time_type", "wait_time_minutes", "park_date",
                   "inserted_at"},
    "fact": {"entity_code", "park_date", "observed_at", "observed_at_ts", "wait_time_type",
             "wait_time_minutes"},
}


def _init_schema_columns():
    """Columns in scripts/init_live_duckdb.py CREATE TABLEs (source of truth for DuckDB)."""
    src = open(os.path.join(ROOT, "scripts", "init_live_duckdb.py")).read()
    out = {}
    for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\s*\)\s*\"\"\"", src, re.S):
        out[table] = {m for m in re.findall(r"^\s*([a-z_]+)\s+[A-Z]", body, re.M) if m != "PRIMARY"}
    return out


def test_known_schema_matches_init_script():
    init = _init_schema_columns()
    for t in ("wti", "entities", "live_waits"):
        assert KNOWN_SCHEMA[t] == init[t], t
    assert KNOWN_SCHEMA["forecasts"] <= init["forecasts"]


def test_every_prompt_table_column_exists():
    prompt = aa.SCHEMA_CONTEXT
    for table, body in re.findall(r"\*\*(\w+)\*\* —[^\n]*\n((?:- [^\n]*\n)+)", prompt):
        cols = re.findall(r"^- (\w+):", body, re.M)
        assert cols, table
        assert set(cols) <= KNOWN_SCHEMA[table], (table, set(cols) - KNOWN_SCHEMA[table])
    fact = re.search(r"Fact-table parquet columns \(ONLY these exist\): ([^\n.]+)\.", prompt).group(1)
    assert {c.strip() for c in fact.split(",")} == KNOWN_SCHEMA["fact"]


def test_prompt_example_sql_binds(tmp_path, monkeypatch):
    """Every example query in the prompt binds against the known schema (no BinderException)."""
    root = tmp_path
    (root / "wti").mkdir(); (root / "curves" / "forecast_parquet").mkdir(parents=True)
    (root / "fact_tables" / "parquet").mkdir(parents=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE w (park_code VARCHAR, park_date DATE, time_slot VARCHAR, wti DOUBLE, source VARCHAR)")
    con.execute(f"COPY w TO '{root}/wti/wti.parquet' (FORMAT PARQUET)")
    con.execute("CREATE TABLE f (entity_code VARCHAR, park_date DATE, time_slot VARCHAR, predicted_actual DOUBLE)")
    con.execute(f"COPY f TO '{root}/curves/forecast_parquet/all_forecasts.parquet' (FORMAT PARQUET)")
    con.execute("CREATE TABLE t (entity_code VARCHAR, park_date VARCHAR, observed_at VARCHAR, "
                "observed_at_ts TIMESTAMPTZ, wait_time_type VARCHAR, wait_time_minutes INTEGER)")
    con.execute("INSERT INTO t VALUES ('MK141','2026-10-21','x',NULL,'POSTED',50)")
    con.execute(f"COPY t TO '{root}/fact_tables/parquet/p.parquet' (FORMAT PARQUET)")
    prompt = aa.SCHEMA_CONTEXT.format(today="2026-10-22", today_dow="Thursday", duckdb_path="x", data_root=root)
    examples = re.findall(r"`(SELECT [^`]+)`", prompt)
    assert len(examples) == 3
    for sql in examples:
        con.execute(sql).fetchall()
    # the dayname guidance for VARCHAR park_date binds; the naive form does not
    fact = f"read_parquet('{root}/fact_tables/parquet/*.parquet')"
    con.execute(f"SELECT dayname(TRY_CAST(park_date AS DATE)) FROM {fact}").fetchall()
    with pytest.raises(duckdb.BinderException):
        con.execute(f"SELECT dayname(park_date) FROM {fact}").fetchall()
    with pytest.raises(duckdb.BinderException):
        con.execute(f"SELECT park_code FROM {fact}").fetchall()


# ---------- (2b) retry cap ----------

def test_binder_error_not_retried_and_returned_to_model(tmp_path, monkeypatch, caplog):
    db = tmp_path / "live.duckdb"
    duckdb.connect(str(db)).execute("CREATE TABLE live_waits (entity_code VARCHAR)").close()
    monkeypatch.setenv(aa.DUCKDB_PATH_ENV, str(db))
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    with caplog.at_level(logging.WARNING):
        res = aa.run_duckdb_query("SELECT park_code FROM live_waits")
    assert res.startswith("Error: SQL error BinderException") and "park_code" in res
    assert sleeps == []
    assert "run_query failed after 1 attempts: BinderException" in caplog.text
    assert aa.tool_result_status(res) == "error"


def test_non_lock_error_retried_once_only(monkeypatch):
    n = []

    def boom(path, read_only):
        n.append(1)
        raise RuntimeError("weird")

    monkeypatch.setattr(aa.duckdb, "connect", boom)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert aa.run_duckdb_query("SELECT 1") == aa.QUERY_GLITCH_MSG
    assert len(n) == 2


def test_lock_error_still_retried_up_to_max(monkeypatch):
    n = []

    def boom(path, read_only):
        n.append(1)
        raise RuntimeError("Could not set lock on file")

    monkeypatch.setattr(aa.duckdb, "connect", boom)
    monkeypatch.setattr("time.sleep", lambda s: None)
    aa.run_duckdb_query("SELECT 1")
    assert len(n) == 8


def test_sql_error_result_redacts_key():
    e = duckdb.BinderException(f'Referenced column "{SECRET}" not found')
    assert "TESTSECRET123" not in aa.sql_error_result(e)


# ---------- follow-ups ----------

@pytest.mark.parametrize("sql,ok", [
    ("SELECT 1", True), ("select 1;", True), ("SELECT 1;;", False), ("SELECT 'a;b' AS x;", True), ("WITH a AS (SELECT 1) SELECT * FROM a", True),
    ("SELECT ';' AS x", True), ("SELECT 1 -- ; comment", True),
    ("SELECT 1; DROP TABLE x", False), ("SELECT 1;ATTACH 'x.db'", False), ("DROP TABLE x", False),
    ("", False), ("SELECT 1; SELECT 2", False),
])
def test_single_select_guard(sql, ok):
    assert aa.is_single_select(sql) is ok


def test_multi_statement_never_reaches_duckdb(monkeypatch):
    ran = []
    monkeypatch.setattr(aa, "run_duckdb_query", lambda sql: ran.append(sql) or "x")
    replies = [chat_reply(tool_calls=[{"id": "c1", "type": "function", "function": {
        "name": "run_query", "arguments": '{"sql": "SELECT 1; DROP TABLE wti"}'}}]), chat_reply("done")]
    with patch("ask_agent._chat", AsyncMock(side_effect=replies)):
        assert asyncio.run(aa.ask_agent("q", "u")) == "done"
    assert ran == []


def test_final_no_tools_failure_log_has_no_planted_key(monkeypatch, caplog):
    tool = chat_reply(tool_calls=[{"id": "c", "type": "function",
                                   "function": {"name": "run_query", "arguments": '{"sql": "SELECT 1"}'}}])
    monkeypatch.setattr(aa, "run_duckdb_query", lambda sql: "x\n-\n1")
    chat = AsyncMock(side_effect=[tool] * 5 + [RuntimeError(f"upstream echoed {SECRET}")])
    with patch("ask_agent._chat", chat), patch("ask_agent._trigger_self_heal"), caplog.at_level(logging.DEBUG):
        asyncio.run(aa.ask_agent("q", "u"))
    assert "final no-tools answer failed: RuntimeError" in caplog.text
    assert "TESTSECRET123" not in caplog.text


# ---------- AC28-AC32 (spec R2) ----------

def _tool(sql, cid="c1"):
    return chat_reply(tool_calls=[{"id": cid, "type": "function",
                                   "function": {"name": "run_query", "arguments": json.dumps({"sql": sql})}}])


def test_ac28_sql_error_message_goes_to_model_and_round_counts(tmp_path, monkeypatch, caplog):
    db = tmp_path / "live.duckdb"
    duckdb.connect(str(db)).execute("CREATE TABLE live_waits (entity_code VARCHAR)").close()
    monkeypatch.setenv(aa.DUCKDB_PATH_ENV, str(db))
    monkeypatch.setattr("time.sleep", lambda s: pytest.fail("AC28: SQL errors must not sleep/retry"))
    seen = []

    async def chat(messages, use_tools):
        seen.append([dict(m) for m in messages])
        return _tool("SELECT park_code FROM live_waits") if len(seen) == 1 else chat_reply("fixed answer")

    with patch("ask_agent._chat", chat), caplog.at_level(logging.INFO):
        assert asyncio.run(aa.ask_agent("q", "u")) == "fixed answer"
    tool_msg = [m for m in seen[1] if m["role"] == "tool"][0]["content"]
    assert tool_msg.startswith("Error: SQL error BinderException") and "park_code" in tool_msg
    assert "after 2 rounds" in caplog.text


@pytest.mark.parametrize("exc", ["CatalogException", "ParserException", "BinderException"])
def test_ac28_each_sql_error_class_single_attempt(exc, monkeypatch):
    n = []
    cls = getattr(duckdb, exc)

    def boom(path, read_only):
        n.append(1)
        raise cls("bad sql")

    monkeypatch.setattr(aa.duckdb, "connect", boom)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert aa.run_duckdb_query("SELECT 1").startswith(f"Error: SQL error {exc}")
    assert len(n) == 1


def test_ac29_other_error_exactly_twice_then_error_to_model(monkeypatch):
    n = []

    def boom(path, read_only):
        n.append(1)
        raise RuntimeError("weird")

    monkeypatch.setattr(aa.duckdb, "connect", boom)
    monkeypatch.setattr("time.sleep", lambda s: None)
    res = aa.run_duckdb_query("SELECT 1")
    assert len(n) == 2 and aa.tool_result_status(res) == "error"


def test_ac29_other_error_then_success(monkeypatch):
    real = aa.duckdb.connect
    n = []

    def flaky(path, read_only):
        n.append(1)
        if len(n) == 1:
            raise RuntimeError("weird")
        return real(":memory:")

    monkeypatch.setattr(aa.duckdb, "connect", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert aa.run_duckdb_query("SELECT 42 AS x").endswith("42") and len(n) == 2


@pytest.mark.parametrize("msg", ["Could not set lock on file", "IO Error: database is locked", "Conflicting lock"])
def test_ac30_lock_error_stops_at_8_then_error_to_model(msg, monkeypatch):
    n = []

    def boom(path, read_only):
        n.append(1)
        raise duckdb.IOException(msg)

    monkeypatch.setattr(aa.duckdb, "connect", boom)
    monkeypatch.setattr("time.sleep", lambda s: None)
    res = aa.run_duckdb_query("SELECT 1")
    assert len(n) == 8 and aa.tool_result_status(res) == "error"


@pytest.mark.parametrize("sql,expect", [
    ("SELECT 1; DROP TABLE wti", aa.MULTI_STATEMENT_MSG),
    ("SELECT 1; SELECT 2", aa.MULTI_STATEMENT_MSG),
    ("DROP TABLE wti", aa.NON_SELECT_MSG),
    ("SELECT 'a;b' FROM wti;", None),
    ("SELECT 1;", None),
])
def test_ac31_guard_rejects_before_execution_with_clear_error(sql, expect, monkeypatch):
    ran = []
    monkeypatch.setattr(aa, "run_duckdb_query", lambda q: ran.append(q) or "x\n-\n1")
    seen = []

    async def chat(messages, use_tools):
        seen.append([dict(m) for m in messages])
        return _tool(sql) if len(seen) == 1 else chat_reply("done")

    with patch("ask_agent._chat", chat):
        asyncio.run(aa.ask_agent("q", "u"))
    tool_msg = [m for m in seen[1] if m["role"] == "tool"][0]["content"]
    if expect:
        assert tool_msg == expect and ran == []
    else:
        assert ran == [sql]


@pytest.mark.parametrize("kind", ["lock", "other", "sql"])
def test_ac32_retry_paths_never_log_key_or_rows(kind, tmp_path, monkeypatch, caplog):
    db = tmp_path / "live.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t (v VARCHAR)")
    con.execute("INSERT INTO t VALUES ('ROWVALUE777')")
    con.close()
    monkeypatch.setenv(aa.DUCKDB_PATH_ENV, str(db))
    monkeypatch.setattr("time.sleep", lambda s: None)
    real = aa.duckdb.connect
    calls = []

    def connect(path, read_only):
        calls.append(1)
        if kind == "lock" and len(calls) < 3:
            raise duckdb.IOException(f"Could not set lock {SECRET} ROWVALUE777")
        if kind == "other" and len(calls) < 2:
            raise RuntimeError(f"weird {SECRET} ROWVALUE777")
        return real(path, read_only=read_only)

    monkeypatch.setattr(aa.duckdb, "connect", connect)
    sql = "SELECT v FROM t" if kind != "sql" else f"SELECT nope_{'x'} FROM t WHERE v = 'ROWVALUE777'"
    with caplog.at_level(logging.DEBUG):
        res = aa.run_duckdb_query(sql)
        if kind == "sql":
            aa.run_duckdb_query(f"SELECT \"{SECRET}\" FROM t")
    if kind != "sql":
        assert "ROWVALUE777" in res  # rows reach the model ...
    assert "ROWVALUE777" not in caplog.text  # ... but never the log
    assert "TESTSECRET123" not in caplog.text


# ---------- Kitty v2 findings ----------

@pytest.mark.parametrize("sql", [
    "SELECT blocked_untl FROM live_waits",
    "SELECT busy_flag FROM live_waits",
    "SELECT * FROM clock_log",
    "SELECT locked FROM live_waits",
])
def test_f1_lock_word_in_identifier_real_duckdb_single_attempt(sql, tmp_path, monkeypatch):
    db = tmp_path / "live.duckdb"
    duckdb.connect(str(db)).execute("CREATE TABLE live_waits (entity_code VARCHAR)").close()
    monkeypatch.setenv(aa.DUCKDB_PATH_ENV, str(db))
    n, real = [], duckdb.connect
    monkeypatch.setattr(aa.duckdb, "connect", lambda *a, **k: n.append(1) or real(*a, **k))
    monkeypatch.setattr("time.sleep", lambda s: pytest.fail("SQL error must not sleep"))
    res = aa.run_duckdb_query(sql)
    assert res.startswith("Error: SQL error ") and len(n) == 1


@pytest.mark.parametrize("exc,msg", [
    ("BinderException", 'Referenced column "busy_flag" not found'),
    ("CatalogException", "Table with name clock_log does not exist"),
    ("ParserException", "syntax error at or near lock"),
])
def test_f1_sql_error_with_lock_words_not_lock_retried(exc, msg, monkeypatch):
    n = []

    def boom(path, read_only):
        n.append(1)
        raise getattr(duckdb, exc)(msg)

    monkeypatch.setattr(aa.duckdb, "connect", boom)
    monkeypatch.setattr("time.sleep", lambda s: pytest.fail("no sleep"))
    assert aa.run_duckdb_query("SELECT 1").startswith(f"Error: SQL error {exc}")
    assert len(n) == 1


@pytest.mark.parametrize("msg,lock", [
    ("IO Error: Could not set lock on file", True),
    ("database is locked", True),
    ("database busy", True),
    ("blocked_untl missing", False),
    ("clock_log broke", False),
    ("busy_flag", False),
])
def test_f1_lock_check_word_boundary(msg, lock):
    assert aa._is_lock_error(RuntimeError(msg)) is lock


@pytest.mark.parametrize("sql", [
    "SELECT 1 -- it's\n; DROP TABLE t; SELECT 'a'",
    "SELECT 1 /* it's */; DROP TABLE t; SELECT 'a'",
    "SELECT $$'$$; DROP TABLE t; SELECT '$$'",
    "SELECT $q$'$q$; DROP TABLE t; SELECT '$q$'",
    "SELECT \"it's\"; DROP TABLE t; SELECT 'a'",
    "SELECT 1;;",
])
def test_f2_guard_rejects_comment_literal_bypasses(sql, monkeypatch):
    assert aa.sql_guard_error(sql) == aa.MULTI_STATEMENT_MSG


@pytest.mark.parametrize("sql", [
    "SELECT 1;",
    "SELECT 'it''s; x'",
    "SELECT \"a;b\" FROM t;",
    "SELECT $$a;b$$",
    "SELECT $t$a;'b$t$;",
    "SELECT 1 /* ; */ -- ;\n",
])
def test_f2_guard_allows_single_statement(sql):
    assert aa.sql_guard_error(sql) is None


@pytest.mark.parametrize("text", [
    "col sk\\u002dant\\u002dapi03\\u002dABCDEFGHIJ not found",
    "col sk\\u002Dant\\u002DABCDEFGHIJ not found",
    "Authorization: Bearer abc.DEF-123_xyz not found",
    'x-api-key: opaqueTOKEN999 not found',
    '"x-api-key": "opaqueTOKEN999" not found',
])
def test_f3_escaped_and_header_tokens_stripped_for_model(text, monkeypatch):
    def boom(path, read_only):
        raise duckdb.BinderException(text)

    monkeypatch.setattr(aa.duckdb, "connect", boom)
    res = aa.run_duckdb_query("SELECT 1")
    assert res.startswith("Error: SQL error BinderException") and "[REDACTED]" in res
    for tok in ("ABCDEFGHIJ", "abc.DEF", "opaqueTOKEN999"):
        assert tok not in res
    assert "not found" in res


def test_prompt_uses_dayname_try_cast_for_park_date():
    assert "dayname(TRY_CAST(park_date AS DATE))" in aa.SCHEMA_CONTEXT
    assert not re.search(r"dayname\(\s*park_date\s*\)", aa.SCHEMA_CONTEXT)



BYPASS_SQL = [
    "SELECT E'\\''; DROP TABLE t; -- '",
    "SELECT 1 /* /* */ '; */ ; DROP TABLE t; --'",
]


@pytest.mark.parametrize("sql", BYPASS_SQL)
def test_parser_guard_bypass_never_reaches_duckdb_qwen(sql, monkeypatch):
    ran = []
    monkeypatch.setattr(aa, "run_duckdb_query", lambda q: ran.append(q) or "x")
    monkeypatch.setattr(aa.duckdb, "connect", lambda *a, **k: pytest.fail("connected to DuckDB"))
    replies = [_tool(sql), chat_reply("done")]
    with patch("ask_agent._chat", AsyncMock(side_effect=replies)):
        assert asyncio.run(aa.ask_agent("q", "u")) == "done"
    assert ran == []


@pytest.mark.parametrize("sql", BYPASS_SQL)
def test_parser_guard_bypass_never_reaches_duckdb_openrouter(sql, monkeypatch):
    call = {"id": "tu1", "type": "function", "function": {"name": "run_query", "arguments": json.dumps({"sql": sql})}}
    replies = [chat_reply(tool_calls=[call]), chat_reply("done")]

    async def post(api_key, payload):
        return replies.pop(0)

    monkeypatch.setattr(aa, "_post_openrouter", post)
    monkeypatch.setattr(aa.duckdb, "connect", lambda *a, **k: pytest.fail("connected to DuckDB"))
    answer, _ = asyncio.run(aa._openrouter_loop("k", "system prompt", "q"))
    assert answer == "done"


@pytest.mark.parametrize("sql", BYPASS_SQL)
def test_parser_guard_rejects_bypass(sql):
    assert aa.sql_guard_error(sql) is not None


@pytest.mark.parametrize("sql", [
    "SELECT $tag$a;b$tag$; DROP TABLE t",
    "SELECT $tag$ x; DROP TABLE t",
    "SELECT $$ x; DROP TABLE t",
    "SELECT 'a''b'; DROP TABLE t",
    "SELECT 'a\"b'; DROP TABLE t; SELECT '\"'",
    "SELECT \"a'b\"; DROP TABLE t; SELECT 'c'",
    "SELECT 'unclosed; DROP TABLE t",
    "SELECT 1 /* unclosed; DROP TABLE t",
    "SELECT U&'a'; DROP TABLE t",
    "SELECT 1;;",
    "SELECT 1;\rDROP TABLE t",
    "SELECT 1;\r\nDROP TABLE t",
])
def test_parser_guard_rejects(sql):
    assert aa.sql_guard_error(sql) is not None


@pytest.mark.parametrize("sql", [
    "SELECT $tag$a;b$tag$",
    "SELECT $tag$a;b$tag$;",
    "SELECT 'it''s; x'",
    "SELECT U&'a;b'",
    "SELECT \"a;b\" FROM t",
    "SELECT \"a;b\" FROM t;",
    "SELECT 'a\"b;c'",
    "SELECT 1;",
    "WITH a AS (SELECT 1) SELECT * FROM a;",
])
def test_parser_guard_allows(sql):
    assert aa.sql_guard_error(sql) is None


def test_guard_rejects_non_select_statement_type():
    assert aa.sql_guard_error("SELECT 1; ATTACH 'x.db'") == aa.MULTI_STATEMENT_MSG
    assert aa.sql_guard_error("DELETE FROM t") == aa.NON_SELECT_MSG



KEY = SECRET + "456789ABCDEFG"


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


PARSE_ERROR_SQL = [
    "SELECT FROM WHERE",
    "SELECT 1 +",
    "SELECT * FROM t2 WHERE",
    "SELECT 'unterminated; DROP TABLE t",
    "SELECT 1 /* unterminated; DROP TABLE t",
]


def _parser_error_text(sql):
    with pytest.raises(duckdb.ParserException) as ei:
        duckdb.extract_statements(sql)
    return aa.sql_error_result(ei.value)


@pytest.mark.parametrize("sql", PARSE_ERROR_SQL)
def test_ac28_parse_error_returns_duckdb_text_from_guard(sql):
    out = aa.sql_guard_error(sql)
    assert out == _parser_error_text(sql)
    assert out.startswith("Error: SQL error ParserException: Parser Error")
    assert "Only one SQL statement" not in out


@pytest.mark.parametrize("sql", PARSE_ERROR_SQL)
def test_ac28_parse_error_text_reaches_model_qwen(sql, monkeypatch):
    monkeypatch.setattr(aa.duckdb, "connect", lambda *a, **k: pytest.fail("connected to DuckDB"))
    chat = AsyncMock(side_effect=[_tool(sql), chat_reply("done")])
    with patch("ask_agent._chat", chat):
        assert asyncio.run(aa.ask_agent("q", "u")) == "done"
    tool_msgs = [m["content"] for m in chat.call_args_list[-1].args[0] if m.get("role") == "tool"]
    assert tool_msgs == [_parser_error_text(sql)]
    assert "Only one SQL statement" not in tool_msgs[0]


@pytest.mark.parametrize("sql", PARSE_ERROR_SQL)
def test_ac28_parse_error_text_reaches_model_claude(sql, monkeypatch):
    replies = [
        NS(content=[NS(type="tool_use", id="tu1", name="run_query", input={"sql": sql})]),
        NS(content=[NS(type="text", text="done")]),
    ]
    sent = []

    async def create(**kw):
        sent.append(kw["messages"])
        return replies.pop(0)

    fake = NS(AsyncAnthropic=lambda **k: NS(messages=NS(create=create)))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(aa.duckdb, "connect", lambda *a, **k: pytest.fail("connected to DuckDB"))
    answer, _ = asyncio.run(aa._claude_loop("k", "system prompt", "q"))
    assert answer == "done"
    results = [b["content"] for m in sent[-1] if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"]
    assert results == [_parser_error_text(sql)]
    assert "Only one SQL statement" not in results[0]


@pytest.mark.parametrize("sql", [
    "WITH x AS (SELECT 1) DELETE FROM t",
    "WITH x AS (SELECT 1) INSERT INTO t SELECT 1",
    "WITH x AS (SELECT 1) UPDATE t SET a = 1",
    "WITH x AS (SELECT 1) DELETE FROM t;",
])
def test_guard_rejects_non_select_behind_with_prefix(sql):
    statements = duckdb.extract_statements(sql)
    assert len(statements) == 1 and statements[0].type != duckdb.StatementType.SELECT
    assert aa.sql_guard_error(sql) == aa.NON_SELECT_MSG
    assert aa.is_single_select(sql) is False


@pytest.mark.parametrize("sql", [
    "SELECT 1;;",
    "SELECT 1 ; ; -- c",
    "SELECT 1;\n;",
    "SELECT 1; /* c */ ;",
    "WITH a AS (SELECT 1) SELECT * FROM a;;",
])
def test_guard_rejects_empty_statements(sql):
    assert aa.sql_guard_error(sql) == aa.MULTI_STATEMENT_MSG


@pytest.mark.parametrize("sql", ["SELECT 1; -- c", "SELECT ';;' AS x;", "SELECT 1 /* ;; */", "SELECT $t$;;$t$;"])
def test_guard_allows_one_trailing_semicolon_and_literal_semicolons(sql):
    assert aa.sql_guard_error(sql) is None


@pytest.mark.parametrize("text", [
    "task-scheduler-identifier",
    "DISK-CACHE-DIRECTORY-PATH",
    "column risk-assessment-score-value missing",
    "bearer 2",
    "Bearer abc123",
    "bearer short-token",
])
def test_redact_keeps_words_that_only_contain_key_prefixes(text):
    assert aa.redact(text) == text


@pytest.mark.parametrize("text,gone", [
    ("token sk-" + "abcdefghijklmnop" + "qrst here", "abcdefghijklmnopqrst"),
    ("Authorization: Bearer " + "abcdefgh" * 2, "abcdefgh" * 2),
    ("Authorization: Bearer abc123def456", "abc123def456"),
    ("x=" + SECRET, "TESTSECRET"),
])
def test_redact_still_strips_keys_at_word_boundaries(text, gone):
    out = aa.redact(text)
    assert gone not in out and "[REDACTED]" in out
