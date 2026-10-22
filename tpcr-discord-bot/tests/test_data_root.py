"""TPCR_DATA_ROOT drives every pipeline path; Qwen request params come from env."""

import ast
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"
ENV = ("TPCR_DATA_ROOT", "TPCR_DUCKDB_PATH", "TPCR_QWEN_MAX_TOKENS",
       "TPCR_QWEN_TEMPERATURE", "TPCR_QWEN_THINKING")


def _clear(mp):
    for v in ENV:
        mp.delenv(v, raising=False)


def _prompt():
    captured = []

    async def fake_post(payload):
        captured.append(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    with patch.object(aa, "_post_chat", fake_post), patch.object(aa, "track_usage", return_value=(1, -1)), \
            patch.object(aa, "log_question"), patch.object(aa, "_trigger_self_heal"):
        asyncio.run(aa.ask_agent("q", "u"))
    return captured[0]


def test_default_root_unchanged(monkeypatch):
    _clear(monkeypatch)
    assert aa.data_path() == "/mnt/data/pipeline"
    assert aa._duckdb_path() == "/mnt/data/pipeline/tpcr_live.duckdb"
    sysmsg = _prompt()["messages"][0]["content"]
    assert "DuckDB at /mnt/data/pipeline/tpcr_live.duckdb" in sysmsg
    assert "read_parquet('/mnt/data/pipeline/wti/wti.parquet')" in sysmsg


def test_root_env_moves_all_paths_and_prompt(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("TPCR_DATA_ROOT", str(tmp_path))
    assert aa.data_path("wti", "wti.parquet") == str(tmp_path / "wti" / "wti.parquet")
    assert aa._duckdb_path() == str(tmp_path / "tpcr_live.duckdb")
    sysmsg = _prompt()["messages"][0]["content"]
    assert "/mnt/data" not in sysmsg
    assert f"DuckDB at {tmp_path}/tpcr_live.duckdb" in sysmsg
    for p in ("wti/wti.parquet", "curves/forecast_parquet/all_forecasts.parquet", "fact_tables/parquet/*.parquet"):
        assert f"'{tmp_path}/{p}'" in sysmsg


def test_explicit_duckdb_var_wins_over_root(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("TPCR_DATA_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("TPCR_DUCKDB_PATH", str(tmp_path / "x.duckdb"))
    assert aa._duckdb_path() == str(tmp_path / "x.duckdb")
    assert f"DuckDB at {tmp_path}/x.duckdb" in _prompt()["messages"][0]["content"]


def test_bot_py_has_no_hardcoded_host_paths():
    src = BOT_PY.read_text()
    assert "/mnt/data" not in src
    assert "wilma" not in src
    for name in ("WTI_PATH", "FORECASTS_PATH", "ENTITIES_PATH", "STAGING_DIR",
                 "FACT_PARQUET_DIR", "DIMPARKHOURS_PATH"):
        assert f"{name} = data_path(" in src
    assert "DUCKDB_PATH = _duckdb_path()" in src
    assert 'TPCR_ASK_FEEDBACK_LOG' in src


def _log_feedback_fn(feedback_log):
    tree = ast.parse(BOT_PY.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_log_feedback")
    ns = {"FEEDBACK_LOG": feedback_log, "datetime": datetime, "_json": json, "logging": logging}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(BOT_PY), "exec"), ns)
    return ns["_log_feedback"]


class _View:
    question, user_id, username = "q?", "1", "n"


def test_feedback_write_failure_logs_and_does_not_raise(tmp_path, caplog):
    fn = _log_feedback_fn(tmp_path / "missing" / "dir" / "f.jsonl")
    with caplog.at_level(logging.WARNING):
        fn(_View(), "up", 2, "r")
    assert "ask feedback log failed" in caplog.text


def test_feedback_write_ok(tmp_path):
    log = tmp_path / "f.jsonl"
    _log_feedback_fn(log)(_View(), "down", 2, "r")
    assert json.loads(log.read_text())["rating"] == "down"


# ---------------------------------------------------------------- Qwen params

def test_qwen_default_payload(monkeypatch):
    _clear(monkeypatch)
    p = _prompt()
    assert p["max_tokens"] == 700
    assert p["temperature"] == 0.2
    assert p["chat_template_kwargs"] == {"enable_thinking": False}


def test_qwen_env_overrides(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TPCR_QWEN_MAX_TOKENS", "1200")
    assert aa._qwen_params() == (1200, 0.2, False)
    monkeypatch.setenv("TPCR_QWEN_TEMPERATURE", "0.7")
    assert aa._qwen_params() == (1200, 0.7, False)
    monkeypatch.setenv("TPCR_QWEN_THINKING", "true")
    assert aa._qwen_params() == (1200, 0.7, True)
    p = _prompt()
    assert (p["max_tokens"], p["temperature"], p["chat_template_kwargs"]) == (1200, 0.7, {"enable_thinking": True})


def test_text_tool_call_parsing_and_malformed_ignored():
    content = ('<think>x</think><tool_call>{"name": "run_query", "arguments": {"sql": "SELECT 1"}}</tool_call>'
               '<tool_call>{not json}</tool_call><tool_call>{"arguments": {}}</tool_call><tool_call>[1]</tool_call>')
    calls = aa.parse_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "run_query"
    assert json.loads(calls[0]["function"]["arguments"]) == {"sql": "SELECT 1"}
    assert aa.parse_text_tool_calls("plain answer") == []
    assert aa.parse_text_tool_calls(None) == []


def test_text_tool_call_fallback_runs_query(monkeypatch):
    _clear(monkeypatch)
    replies = [
        {"choices": [{"message": {"content": '<tool_call>{"name": "run_query", "arguments": {"sql": "SELECT 1"}}</tool_call>'}}]},
        {"choices": [{"message": {"content": "final"}}]},
    ]

    async def fake_post(payload):
        return replies.pop(0)

    with patch.object(aa, "_post_chat", fake_post), patch.object(aa, "track_usage", return_value=(1, -1)), \
            patch.object(aa, "log_question"), patch.object(aa, "_trigger_self_heal"), \
            patch.object(aa, "run_duckdb_query", return_value="1") as q:
        assert asyncio.run(aa.ask_agent("q", "u")) == "final"
    q.assert_called_once_with("SELECT 1")


def test_question_log_has_model_field(monkeypatch, tmp_path):
    log = tmp_path / "q.jsonl"
    monkeypatch.setenv("TPCR_ASK_QUESTION_LOG", str(log))
    aa.log_question("u", "n", "q", "a", 5)
    assert json.loads(log.read_text())["model"] == aa.QWEN_MODEL


# ---------------------------------------------------------------- review fixes (v1)

def test_text_tool_call_deeply_nested_json_skipped():
    deep = "[" * 100000 + "]" * 100000
    content = (f'<tool_call>{{"name": "run_query", "arguments": {deep}}}</tool_call>'
               '<tool_call>{"name": "run_query", "arguments": {"sql": "SELECT 2"}}</tool_call>')
    calls = aa.parse_text_tool_calls(content)
    assert [json.loads(c["function"]["arguments"]) for c in calls] == [{"sql": "SELECT 2"}]


def test_text_tool_call_without_arguments_skipped():
    assert aa.parse_text_tool_calls('<tool_call>{"name": "run_query"}</tool_call>') == []


def test_non_numeric_qwen_env_falls_back_with_warning(monkeypatch, caplog):
    _clear(monkeypatch)
    monkeypatch.setenv("TPCR_QWEN_MAX_TOKENS", "lots")
    monkeypatch.setenv("TPCR_QWEN_TEMPERATURE", "warm")
    with caplog.at_level(logging.WARNING, logger=aa.logger.name):
        assert aa._qwen_params() == (700, 0.2, False)
    assert "TPCR_QWEN_MAX_TOKENS" in caplog.text and "TPCR_QWEN_TEMPERATURE" in caplog.text
    p = _prompt()  # /ask still works
    assert (p["max_tokens"], p["temperature"]) == (700, 0.2)


def test_feedback_log_default_is_bot_dir(monkeypatch):
    monkeypatch.delenv("TPCR_ASK_FEEDBACK_LOG", raising=False)
    tree = ast.parse(BOT_PY.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "FEEDBACK_LOG")
    ns = {"Path": Path, "os": os, "__file__": str(BOT_PY)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(BOT_PY), "exec"), ns)
    assert ns["FEEDBACK_LOG"] == BOT_PY.parent / "ask_feedback.jsonl"
    monkeypatch.setenv("TPCR_ASK_FEEDBACK_LOG", "/tmp/x/f.jsonl")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(BOT_PY), "exec"), ns)
    assert ns["FEEDBACK_LOG"] == Path("/tmp/x/f.jsonl")


def test_answered_by_log_line(monkeypatch, caplog):
    _clear(monkeypatch)
    with caplog.at_level(logging.INFO, logger=aa.logger.name):
        _prompt()
    assert any(r.getMessage().startswith(f"/ask answered by {aa.QWEN_MODEL} in ") and r.getMessage().endswith(" ms")
               for r in caplog.records)


def test_usage_and_question_log_defaults_in_bot_dir():
    bot_dir = BOT_PY.parent
    assert aa.USAGE_FILE == bot_dir / "ask_usage.json"
    assert aa.QUESTION_LOG == bot_dir / "ask_questions.jsonl"
    assert "wilma" not in (bot_dir / "ask_agent.py").read_text()
