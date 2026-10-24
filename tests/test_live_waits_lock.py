"""
Tests for TPCR-S11-LOCK-001: scraper live_waits write survives the s11 DuckDB write lock.

The lock is simulated with a child process holding the DuckDB file open read-write
(DuckDB file locks are per-process). Everything lives under tmp_path.

Run: python -m pytest tests/test_live_waits_lock.py
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO / "src"), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import get_wait_times_from_queue_times as scraper
from live_waits_common import (
    LIVE_WAITS_COLUMNS,
    insert_missing_live_waits,
    park_tz_for_park_code,
    prepare_live_waits_rows,
)
from replay_live_waits import replay_staging_to_live

ET = ZoneInfo("America/New_York")
LOG = logging.getLogger("test_live_waits_lock")

# Same table as scripts/init_live_duckdb.py (with and without the PRIMARY KEY)
DDL_PK = """
    CREATE TABLE live_waits (
        entity_code VARCHAR NOT NULL, observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        wait_time_type VARCHAR DEFAULT 'POSTED', wait_time_minutes INTEGER NOT NULL,
        park_date DATE NOT NULL, inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (entity_code, observed_at, wait_time_type))
"""
DDL_NO_PK = DDL_PK.replace(",\n        PRIMARY KEY (entity_code, observed_at, wait_time_type)", "")
DDL_FRESH = """CREATE TABLE data_freshness (source VARCHAR PRIMARY KEY, last_updated TIMESTAMP NOT NULL,
               row_count INTEGER, notes VARCHAR)"""

HOLDER = """
import duckdb, sys, time
con = duckdb.connect(sys.argv[1])
con.execute("BEGIN")
print("READY", flush=True)
time.sleep(float(sys.argv[2]))
"""


def make_db(path: Path, pk: bool = True) -> Path:
    con = duckdb.connect(str(path))
    con.execute(DDL_PK if pk else DDL_NO_PK)
    con.execute(DDL_FRESH)
    con.close()
    return path


def count(path: Path) -> int:
    con = duckdb.connect(str(path))
    try:
        return con.execute("SELECT COUNT(*) FROM live_waits").fetchone()[0]
    finally:
        con.close()


def mk_df(n: int, start_hour: int = 10, entity: str = "MK01", day: str = "2026-10-20") -> pd.DataFrame:
    rows = [
        {"entity_code": entity, "observed_at": f"{day}T{start_hour:02d}:{i:02d}:00-04:00",
         "wait_time_type": "POSTED", "wait_time_minutes": 10 + i}
        for i in range(n)
    ]
    return pd.DataFrame(rows)


class Holder:
    """Child process that holds the DuckDB write lock for `seconds`."""

    def __init__(self, db: Path, seconds: float):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(db), str(seconds)],
            stdout=subprocess.PIPE, text=True,
        )
        assert self.proc.stdout.readline().strip() == "READY"

    def release(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture(autouse=True)
def _reset_cooldown():
    scraper._live_write_cooldown_until = 0.0
    yield
    scraper._live_write_cooldown_until = 0.0


def pending_files(base: Path):
    d = base / "state" / "live_waits_pending"
    return sorted(d.glob("*.csv")) if d.exists() else []


def test_retry_succeeds_after_lock_released(tmp_path):
    make_db(tmp_path / "tpcr_live.duckdb")
    with Holder(tmp_path / "tpcr_live.duckdb", 4):
        ok = scraper._write_to_live_duckdb(
            mk_df(5), ET, tmp_path, LOG, retry_budget_s=20, attempt_timeout_s=10)
    assert ok is True
    assert count(tmp_path / "tpcr_live.duckdb") == 5
    assert pending_files(tmp_path) == []


def test_batch_kept_on_exhaustion_then_flushed(tmp_path):
    db = make_db(tmp_path / "tpcr_live.duckdb")
    with Holder(db, 30) as h:
        ok = scraper._write_to_live_duckdb(
            mk_df(3), ET, tmp_path, LOG, retry_budget_s=3, attempt_timeout_s=5)
        assert ok is False
        assert len(pending_files(tmp_path)) == 1
        h.release()
    assert count(db) == 0

    scraper._live_write_cooldown_until = 0.0
    ok = scraper._write_to_live_duckdb(
        mk_df(2, start_hour=11), ET, tmp_path, LOG, retry_budget_s=10, attempt_timeout_s=10)
    assert ok is True
    assert count(db) == 5
    assert pending_files(tmp_path) == []


def test_cooldown_parks_without_retry(tmp_path, monkeypatch):
    make_db(tmp_path / "tpcr_live.duckdb")
    calls = []
    monkeypatch.setattr(scraper.subprocess, "run", lambda *a, **k: calls.append(k) or None)
    scraper._live_write_cooldown_until = time.monotonic() + 60
    assert scraper._write_to_live_duckdb(mk_df(2), ET, tmp_path, LOG) is False
    assert calls == []
    assert len(pending_files(tmp_path)) == 1


def test_attempt_timeouts_bounded_by_attempt_and_budget(tmp_path, monkeypatch):
    make_db(tmp_path / "tpcr_live.duckdb")
    start = time.monotonic()
    budget, attempt_t = 4.0, 2.0
    seen = []

    def fake_run(cmd, **kw):
        seen.append((kw["timeout"], budget - (time.monotonic() - start)))
        raise RuntimeError("IO Error: Could not set lock on file")

    monkeypatch.setattr(scraper.subprocess, "run", fake_run)
    ok = scraper._write_to_live_duckdb(
        mk_df(2), ET, tmp_path, LOG, retry_budget_s=budget, attempt_timeout_s=attempt_t)
    assert ok is False
    assert len(seen) >= 2
    for timeout, remaining in seen:
        assert 1 <= timeout <= attempt_t
        assert timeout <= max(1.0, remaining) + 0.05
    assert len(pending_files(tmp_path)) == 1
    assert time.monotonic() - start < budget + 2


def test_non_lock_error_not_retried(tmp_path, monkeypatch):
    make_db(tmp_path / "tpcr_live.duckdb")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(1)
        raise RuntimeError("Catalog Error: table live_waits does not exist")

    monkeypatch.setattr(scraper.subprocess, "run", fake_run)
    assert scraper._write_to_live_duckdb(mk_df(2), ET, tmp_path, LOG) is False
    assert len(calls) == 1
    assert len(pending_files(tmp_path)) == 1


@pytest.mark.parametrize("pk", [True, False])
def test_replay_fills_gaps_and_is_idempotent(tmp_path, pk):
    db = make_db(tmp_path / "tpcr_live.duckdb", pk=pk)
    staging = tmp_path / "staging" / "queue_times" / "2026-10"
    staging.mkdir(parents=True)

    mk = mk_df(10, entity="MK01", day="2026-10-20")
    mk.loc[len(mk)] = ["MK02", "2026-10-20T12:00:00-04:00", "POSTED", 0]  # wait<=0: not a candidate
    mk.to_csv(staging / "mk_2026-10-20.csv", index=False)
    uh = mk_df(6, entity="USH01", day="2026-10-21").assign(
        observed_at=lambda d: d["observed_at"].str.replace("-04:00", "-07:00"))
    uh.to_csv(staging / "uh_2026-10-21.csv", index=False)

    # Pre-existing rows (some gaps remain)
    con = duckdb.connect(str(db))
    insert_missing_live_waits(con, prepare_live_waits_rows(mk.iloc[:4], park_tz_for_park_code("mk")))
    insert_missing_live_waits(con, prepare_live_waits_rows(uh.iloc[:2], park_tz_for_park_code("uh")))
    con.close()
    assert count(db) == 6

    dates = ["2026-10-20", "2026-10-21"]
    r = replay_staging_to_live(db, tmp_path / "staging" / "queue_times", dates, LOG, budget_s=5)
    assert r["files"] == 2
    assert r["candidate_rows"] == 16
    assert r["inserted"] == 10  # 6 missing mk + 4 missing uh
    assert r["per_date"]["2026-10-20"]["inserted"] == 6
    assert r["per_date"]["2026-10-21"]["inserted"] == 4
    assert count(db) == 16
    n_after_first = count(db)

    r2 = replay_staging_to_live(db, tmp_path / "staging" / "queue_times", dates, LOG, budget_s=5)
    assert r2["inserted"] == 0
    assert count(db) == n_after_first == 16, "replay twice must give the same row count"


def test_prep_drops_bad_rows_and_uses_6am_rule():
    df = pd.DataFrame([
        {"entity_code": "MK01", "observed_at": "2026-10-20T05:30:00-04:00", "wait_time_type": "POSTED", "wait_time_minutes": 10},
        {"entity_code": "MK01", "observed_at": "2026-10-20T06:00:00-04:00", "wait_time_type": "POSTED", "wait_time_minutes": 20},
        {"entity_code": "MK01", "observed_at": "2026-10-20T07:00:00-04:00", "wait_time_type": "POSTED", "wait_time_minutes": 0},
        {"entity_code": "MK01", "observed_at": "2026-10-20T08:00:00-04:00", "wait_time_type": "POSTED", "wait_time_minutes": -5},
        {"entity_code": None, "observed_at": "2026-10-20T09:00:00-04:00", "wait_time_type": "POSTED", "wait_time_minutes": 5},
        {"entity_code": "", "observed_at": "2026-10-20T09:00:00-04:00", "wait_time_type": "POSTED", "wait_time_minutes": 5},
        {"entity_code": "MK01", "observed_at": None, "wait_time_type": "POSTED", "wait_time_minutes": 5},
        {"entity_code": "MK01", "observed_at": "2026-10-20T09:00:00-04:00", "wait_time_type": None, "wait_time_minutes": 5},
    ])
    out = prepare_live_waits_rows(df, park_tz_for_park_code("mk"))
    assert list(out.columns) == list(LIVE_WAITS_COLUMNS)
    assert out["wait_time_minutes"].tolist() == [10, 20]
    assert out["park_date"].tolist() == ["2026-10-19", "2026-10-20"]


def test_null_key_rows_counted_and_logged(caplog):
    good = {"observed_at": "2026-10-20T09:00:00-04:00", "wait_time_type": "POSTED", "wait_time_minutes": 5}
    rows = [
        {**good, "entity_code": "MK01"},
        {**good, "entity_code": "MK02", "observed_at": "2026-10-20T10:00:00-04:00"},
        {**good, "entity_code": None},
        {**good, "entity_code": "  "},
        {**good, "entity_code": "MK03", "observed_at": None},
        {**good, "entity_code": "MK04", "wait_time_type": ""},
    ]
    tz = park_tz_for_park_code("mk")
    with caplog.at_level(logging.WARNING):
        out = prepare_live_waits_rows(pd.DataFrame(rows), tz)
    assert len(out) == 2
    assert out.attrs["dropped_null_key"] == 4
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("dropped 4 row(s) with null/empty key" in r.getMessage() for r in warnings)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        clean = prepare_live_waits_rows(pd.DataFrame(rows[:2]), tz)
    assert clean.attrs["dropped_null_key"] == 0
    assert not any("null/empty key" in r.getMessage() for r in caplog.records)


def test_park_tz_for_park_code():
    assert park_tz_for_park_code("mk").key == "America/New_York"
    assert park_tz_for_park_code("uh").key == "America/Los_Angeles"
    assert park_tz_for_park_code("tdl").key == "Asia/Tokyo"
    with pytest.raises(ValueError):
        park_tz_for_park_code("zz")


def test_replay_due():
    def et(h, m, d=21):
        return datetime(2026, 10, d, h, m, tzinfo=ET)

    assert scraper.replay_due(et(4, 29), None) is False
    assert scraper.replay_due(et(4, 30), None) is True
    assert scraper.replay_due(et(9, 0), date(2026, 10, 20)) is True
    assert scraper.replay_due(et(9, 0), date(2026, 10, 21)) is False
    assert scraper.replay_due(et(3, 0, d=22), date(2026, 10, 21)) is False
