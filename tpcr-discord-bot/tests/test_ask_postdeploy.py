"""Post-deploy /ask fixes: Claude payload validity, safe 400 logging, prompt/schema
consistency, run_query retry cap, single-statement guard, final no-tools log line."""

import asyncio
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

def _validate_claude_request(kw):
    """Structural checks the Messages API enforces (subset that 400s)."""
    assert re.fullmatch(r"claude-[a-z]+-\d+(-\d+)?(-\d{8})?", kw["model"])
    assert isinstance(kw["max_tokens"], int) and 0 < kw["max_tokens"] <= 64000
    assert isinstance(kw["system"], str) and kw["system"].strip()
    for t in kw["tools"]:
        assert set(t) == {"name", "description", "input_schema"}
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["name"])
        assert t["input_schema"]["type"] == "object"
    msgs = kw["messages"]
    assert msgs[0]["role"] == "user"
    pending = set()
    for i, m in enumerate(msgs):
        assert m["role"] == ("user" if i % 2 == 0 else "assistant")
        content = m["content"]
        if isinstance(content, str):
            assert content.strip()
            continue
        assert content, "empty content list"
        for b in content:
            assert isinstance(b, dict), "blocks must be plain dicts"
            if b["type"] == "text":
                assert b["text"].strip(), "empty text block"
            elif b["type"] == "tool_use":
                pending.add(b["id"])
            elif b["type"] == "tool_result":
                assert b["tool_use_id"] in pending, "tool_result without tool_use"
                assert isinstance(b["content"], str) and b["content"]
                pending.discard(b["tool_use_id"])
        if m["role"] == "user":
            assert not pending, "tool_use not answered by the next user turn"


def test_claude_payload_valid_across_tool_rounds(monkeypatch):
    calls = []
    replies = [
        NS(content=[NS(type="text", text=""),  # empty text block alongside a tool_use
                    NS(type="tool_use", id="tu1", name="run_query", input={"sql": "SELECT 1"})]),
        NS(content=[NS(type="tool_use", id="tu2", name="run_query", input={"sql": "SELECT 2"}),
                    NS(type="tool_use", id="tu3", name="run_query", input={"sql": "DROP TABLE x"})]),
        NS(content=[NS(type="text", text="Seven Dwarfs averaged 51 min.")]),
    ]

    async def create(**kw):
        calls.append({**kw, "messages": [dict(m) for m in kw["messages"]]})
        _validate_claude_request(calls[-1])
        return replies[len(calls) - 1]

    fake = NS(AsyncAnthropic=lambda **k: NS(messages=NS(create=create)))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(aa, "run_duckdb_query", lambda sql: "x\n-\n1")
    answer, rounds = asyncio.run(aa._claude_loop("k", "system prompt", "q"))
    assert answer == "Seven Dwarfs averaged 51 min." and rounds == 3
    assert len(calls) == 3
    assert calls[0]["model"] == "claude-sonnet-4-6"


def test_requirements_pin_anthropic_sdk():
    req = open(os.path.join(ROOT, "requirements.txt")).read()
    assert re.search(r"^anthropic>=", req, re.M)


def _status_error(code, body):
    anthropic = pytest.importorskip("anthropic")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(f"Error code: {code} {SECRET}", response=httpx.Response(code, request=req),
                                     body=body)


@pytest.mark.parametrize("message,expect", [
    ("Your credit balance is too low to access the Anthropic API.", "credit balance is too low"),
    ("messages: text content blocks must be non-empty", "text content blocks must be non-empty"),
])
def test_400_logs_status_type_and_redacted_message(message, expect, monkeypatch, caplog):
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "anthropic")
    err = _status_error(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                         "message": f"{message} key={SECRET}"}})
    with patch("ask_agent._claude_loop", AsyncMock(side_effect=err)), \
         patch("ask_agent._chat", AsyncMock(return_value=chat_reply("qwen answer"))), \
         caplog.at_level(logging.DEBUG):
        assert asyncio.run(aa.ask_agent("q", "u", api_key=SECRET)) == "qwen answer"
    line = [r.getMessage() for r in caplog.records if "Claude /ask failed" in r.getMessage()][0]
    assert "BadRequestError" in line and "status=400" in line and "type=invalid_request_error" in line
    assert expect in line
    assert "TESTSECRET123" not in caplog.text


def test_error_summary_redacts_non_prefixed_key():
    e = NS(status_code=400, body={"error": {"type": "invalid_request_error", "message": "bad KEYVALUE99"}})
    assert "KEYVALUE99" not in aa.anthropic_error_summary(e, "KEYVALUE99")
    assert aa.anthropic_error_summary(ValueError("x")) == ""


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
    ("SELECT 1", True), ("select 1;", True), ("WITH a AS (SELECT 1) SELECT * FROM a", True),
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
