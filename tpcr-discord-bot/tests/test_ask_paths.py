"""ask_agent host paths come from env vars (read at call time) and never crash /ask."""

import asyncio
import json
import logging
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402

ENV_VARS = (aa.DUCKDB_PATH_ENV, aa.USAGE_FILE_ENV, aa.QUESTION_LOG_ENV)


def _clear(monkeypatch):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)


def test_defaults_unchanged_without_env(monkeypatch, tmp_path):
    _clear(monkeypatch)
    assert aa._duckdb_path() == f"{tmp_path}/tpcr_live.duckdb"
    assert aa._usage_file() == aa.USAGE_FILE
    assert aa._question_log() == aa.QUESTION_LOG
    assert str(aa.USAGE_FILE).endswith("tpcr-discord-bot/ask_usage.json")


def test_env_overrides_read_at_call_time(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(aa.DUCKDB_PATH_ENV, str(tmp_path / "x.duckdb"))
    monkeypatch.setenv(aa.USAGE_FILE_ENV, str(tmp_path / "u.json"))
    monkeypatch.setenv(aa.QUESTION_LOG_ENV, str(tmp_path / "q.jsonl"))
    assert aa._duckdb_path() == str(tmp_path / "x.duckdb")
    assert aa._usage_file() == tmp_path / "u.json"
    assert aa._question_log() == tmp_path / "q.jsonl"


def test_blank_env_falls_back_to_default(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(aa.DUCKDB_PATH_ENV, "  ")
    assert aa._duckdb_path() == f"{tmp_path}/tpcr_live.duckdb"


def test_track_usage_and_log_create_parent_dirs(monkeypatch, tmp_path):
    _clear(monkeypatch)
    usage = tmp_path / "a" / "b" / "usage.json"
    qlog = tmp_path / "c" / "d" / "q.jsonl"
    monkeypatch.setenv(aa.USAGE_FILE_ENV, str(usage))
    monkeypatch.setenv(aa.QUESTION_LOG_ENV, str(qlog))
    assert aa.track_usage("u1") == (1, -1)
    assert aa.track_usage("u1") == (2, -1)
    assert sum(json.loads(usage.read_text()).popitem()[1].values()) == 2
    aa.log_question("u1", "n", "q?", "a.", 5)
    assert json.loads(qlog.read_text().splitlines()[0])["question"] == "q?"


def test_unwritable_paths_log_error_and_do_not_raise(monkeypatch, tmp_path, caplog):
    _clear(monkeypatch)
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    monkeypatch.setenv(aa.USAGE_FILE_ENV, str(blocker / "usage.json"))
    monkeypatch.setenv(aa.QUESTION_LOG_ENV, str(blocker / "q.jsonl"))
    with caplog.at_level(logging.ERROR, logger="tpcr-bot"):
        assert aa.track_usage("u1") == (0, -1)
        aa.log_question("u1", "n", "q", "a", 1)
    text = caplog.text
    assert "ask usage tracking failed" in text and aa.USAGE_FILE_ENV in text
    assert "ask question log failed" in text and aa.QUESTION_LOG_ENV in text


def test_unset_env_on_foreign_host_does_not_crash_ask(monkeypatch, tmp_path, caplog):
    """The default /home/wilma paths don't exist here: /ask must still answer."""
    _clear(monkeypatch)
    monkeypatch.setattr(aa, "USAGE_FILE", tmp_path / "nope" / "x" / "usage.json")
    (tmp_path / "nope").write_text("blocks mkdir")
    reply = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    with patch.object(aa, "_post_chat", AsyncMock(return_value=reply)), \
         patch.object(aa, "_trigger_self_heal"), \
         patch.object(aa, "log_question"):
        assert asyncio.run(aa.ask_agent("q", "u1")) == "hi"
    assert "ask usage tracking failed" in caplog.text


def _tool_round(sql, query_result):
    call = {"id": "c1", "type": "function",
            "function": {"name": "run_query", "arguments": json.dumps({"sql": sql})}}
    first = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
    final = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}
    return first, final


def _run_tool_round(sql, query_result, caplog):
    first, final = _tool_round(sql, query_result)
    with patch.object(aa, "_post_chat", AsyncMock(side_effect=[first, final])), \
         patch.object(aa, "run_duckdb_query", return_value=query_result), \
         patch.object(aa, "track_usage", return_value=(1, -1)), \
         patch.object(aa, "log_question"), patch.object(aa, "_trigger_self_heal"), \
         caplog.at_level(logging.INFO, logger="tpcr-bot"):
        assert asyncio.run(aa.ask_agent("q", "u1")) == "done"
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]


def test_tool_round_logs_ok(caplog):
    msgs = _run_tool_round("SELECT 1", "x\n-\n1", caplog)
    assert "ask tool_call name=run_query id=c1" in msgs
    assert "ask tool_result name=run_query status=ok" in msgs


def test_tool_round_logs_error_on_glitch(caplog):
    msgs = _run_tool_round("SELECT 1", aa.QUERY_GLITCH_MSG, caplog)
    assert "ask tool_result name=run_query status=error" in msgs


def test_tool_round_logs_error_on_rejected_sql(caplog):
    msgs = _run_tool_round("DROP TABLE x", "unused", caplog)
    assert "ask tool_result name=run_query status=error" in msgs


def test_run_duckdb_query_uses_env_path(monkeypatch, tmp_path):
    seen = []

    class Boom(Exception):
        pass

    def fake_connect(path, read_only):
        seen.append(path)
        raise Boom("nope")

    monkeypatch.setenv(aa.DUCKDB_PATH_ENV, str(tmp_path / "mini.duckdb"))
    monkeypatch.setattr(aa.duckdb, "connect", fake_connect, raising=False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert aa.run_duckdb_query("SELECT 1", max_retries=2) == aa.QUERY_GLITCH_MSG
    assert seen == [str(tmp_path / "mini.duckdb")] * 2


def test_question_log_default_used_when_env_unset(monkeypatch, tmp_path):
    """With TPCR_ASK_QUESTION_LOG unset, log_question writes to the QUESTION_LOG default."""
    _clear(monkeypatch)
    monkeypatch.delenv("TPCR_ASK_QUESTION_LOG", raising=False)
    assert str(aa._question_log()).endswith("tpcr-discord-bot/ask_questions.jsonl")
    default = tmp_path / "default" / "ask_questions.jsonl"
    monkeypatch.setattr(aa, "QUESTION_LOG", default)
    assert aa._question_log() == default
    aa.log_question("u1", "n", "default?", "a.", 3)
    assert json.loads(default.read_text().splitlines()[0])["question"] == "default?"


def test_duckdb_path_env_var_name_is_exact(monkeypatch, tmp_path):
    """The DuckDB path comes from the env var named exactly TPCR_DUCKDB_PATH."""
    _clear(monkeypatch)
    assert aa.DUCKDB_PATH_ENV == "TPCR_DUCKDB_PATH"
    target = str(tmp_path / "exact.duckdb")
    monkeypatch.setenv("TPCR_DUCKDB_PATH", target)
    assert aa._duckdb_path() == target
