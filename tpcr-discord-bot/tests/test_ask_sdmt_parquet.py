"""Fact-table parquet questions (e.g. posted wait at Seven Dwarfs Mine Train) end to end with real DuckDB."""

import asyncio
import json
import os
import re
import sys
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

import duckdb  # noqa: F401  required (requirements-test.txt); no skip
import pandas  # noqa: F401
import pyarrow  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402

QUESTION = "What ws the average posted wait at Seven Dwarfs Mine Train last Saturday?"


@pytest.fixture
def data(tmp_path, monkeypatch):
    parquet_dir = tmp_path / "fact_tables" / "parquet"
    parquet_dir.mkdir(parents=True)
    rows = []
    for minute, ts in zip((50, 51, 51, 51, 51), range(10, 15)):
        rows.append(("MK141", "2026-10-17", f"2026-10-17 {ts}:00:00", "POSTED", minute))
    rows += [("MK141", "2026-10-17", "2026-10-17 15:00:00", "ACTUAL", 20),
             ("MK141", "2026-10-17", "2026-10-17 16:00:00", "ACTUAL", 25),
             ("MK141", "NaT", "NaT", "POSTED", 99),
             ("MK141", "2026-10-10", "2026-10-10 10:00:00", "POSTED", 70),
             ("MK141", "2026-10-24", "2026-10-24 10:00:00", "POSTED", 80),
             ("MK01", "2026-10-17", "2026-10-17 10:00:00", "POSTED", 5)]
    con = duckdb.connect()
    con.execute("CREATE TABLE f (entity_code VARCHAR, park_date VARCHAR, observed_at VARCHAR, "
                "observed_at_ts TIMESTAMPTZ, wait_time_type VARCHAR, wait_time_minutes INTEGER)")
    for code, pd_, obs, typ, mins in rows:
        ts = "NULL" if obs == "NaT" else f"TIMESTAMPTZ '{obs}+00'"
        con.execute(f"INSERT INTO f VALUES ('{code}', '{pd_}', '{obs}', {ts}, '{typ}', {mins})")
    con.execute(f"COPY f TO '{parquet_dir / 'part.parquet'}' (FORMAT PARQUET)")
    con.close()

    live = tmp_path / "live.duckdb"
    con = duckdb.connect(str(live))
    con.execute("CREATE TABLE live_waits (entity_code VARCHAR, observed_at TIMESTAMPTZ, "
                "wait_time_type VARCHAR, wait_time_minutes INTEGER, park_date DATE)")
    con.close()
    monkeypatch.setenv("TPCR_DUCKDB_PATH", str(live))
    return tmp_path


def _system_prompt():
    today = date.today()
    return aa.SCHEMA_CONTEXT.format(today=today.isoformat(), today_dow=today.strftime("%A"),
                                    duckdb_path=aa._duckdb_path(), data_root=aa.data_path())


def test_average_posted_wait_from_parquet(data):
    sql = re.search(r"`(SELECT round\(avg.*?)`", _system_prompt()).group(1)
    assert "MK141" in sql and "'POSTED'" in sql and "park_date = '2026-10-17'" in sql

    async def chat(messages, use_tools):
        if messages[-1]["role"] != "tool":
            call = {"id": "c1", "type": "function",
                    "function": {"name": "run_query", "arguments": json.dumps({"sql": sql})}}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}]}
        return {"choices": [{"message": {"role": "assistant",
                                         "content": f"Average posted wait: {messages[-1]['content']}"}}]}

    with patch.object(aa, "_chat", AsyncMock(side_effect=chat)), \
            patch.object(aa, "track_usage", return_value=(1, -1)), \
            patch.object(aa, "log_question"), patch.object(aa, "_trigger_self_heal"):
        answer = asyncio.run(aa.ask_agent(QUESTION, "u"))
    assert "50.8" in answer


def test_prompt_documents_posted_and_nat(data):
    text = _system_prompt()
    assert "POSTED" in text and "NaT" in text


def test_no_data_range_tolerates_nat_rows(data):
    bounds = aa.run_duckdb_query(aa.date_range_sql())
    min_date, max_date = [v.strip() for v in bounds.split("\n")[2].split("|")]
    assert min_date <= "2026-10-17" <= max_date
    assert (min_date, max_date) == ("2026-10-10", "2026-10-24")
