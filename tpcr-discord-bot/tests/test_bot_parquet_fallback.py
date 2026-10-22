"""Parquet-fallback queries in bot.py (no live DuckDB file, no network)."""

from datetime import date
from unittest import mock

import duckdb
import pytest

from .test_ask_qwen import bot  # noqa: F401  (module-scoped bot import fixture)

D1 = date(2026, 9, 15)
D2 = date(2026, 9, 16)


@pytest.fixture()
def parquet_paths(tmp_path):
    wti = tmp_path / "wti.parquet"
    fc = tmp_path / "forecasts.parquet"
    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT * FROM (VALUES
            ('MK', DATE '2026-09-15', 15.0),
            ('MK', DATE '2026-09-16', 45.0),
            ('EP', DATE '2026-09-15', 99.0)
        ) t(park_code, park_date, wti)) TO '{wti}' (FORMAT PARQUET)
    """)
    con.execute(f"""
        COPY (SELECT * FROM (VALUES
            ('MK01', DATE '2026-09-15', '10:00:00', 20.0),
            ('MK01', DATE '2026-09-15', '14:00:00', 40.0),
            ('MK01', DATE '2026-09-16', '10:00:00', 10.0),
            ('MK01', DATE '2026-09-16', '14:00:00', 50.0),
            ('EP01', DATE '2026-09-15', '10:00:00', 70.0)
        ) t(entity_code, park_date, time_slot, predicted_actual))
        TO '{fc}' (FORMAT PARQUET)
    """)
    con.close()
    return str(wti), str(fc)


@pytest.fixture()
def parquet_mode(bot, parquet_paths):  # noqa: F811
    wti, fc = parquet_paths
    with (
        mock.patch.object(bot, "_USE_DUCKDB", False),
        mock.patch.object(bot, "WTI_PATH", wti),
        mock.patch.object(bot, "FORECASTS_PATH", fc),
    ):
        yield bot


@pytest.fixture()
def busy_default_connection():
    """Leave an unfinished result on DuckDB's shared default connection."""
    duckdb.execute("SELECT * FROM range(100000)")
    yield
    duckdb.execute("SELECT 1").fetchall()


def test_get_wti_range_picks_row_for_each_date(parquet_mode):
    r1 = parquet_mode.get_wti_range("MK", D1)
    r2 = parquet_mode.get_wti_range("MK", D2)
    # raw avg 30 on both days; scaled to that day's mapped WTI
    assert r1 == {"wti_low": 10.0, "wti_avg": 15.0, "wti_high": 20.0}
    assert r2 == {"wti_low": 15.0, "wti_avg": 45.0, "wti_high": 75.0}


def test_get_wti_score_parquet_with_busy_default_connection(parquet_mode, busy_default_connection):
    assert parquet_mode.get_wti_score("MK", D2) == pytest.approx(45.0)


def test_get_forecasts_parquet_with_busy_default_connection(parquet_mode, busy_default_connection):
    df = parquet_mode.get_forecasts("MK", D1)
    assert list(df["entity_code"]) == ["MK01"]
    assert float(df["avg_wait"].iloc[0]) == pytest.approx(30.0)


def test_get_wti_range_with_busy_default_connection(parquet_mode, busy_default_connection):
    assert parquet_mode.get_wti_range("MK", D2)["wti_avg"] == 45.0
