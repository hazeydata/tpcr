"""Tests for scripts/backfill_live_waits_oct1_9.py (TPCR-BACKFILL-OCT1-9). tmp_path only; no real data."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

import duckdb
import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_live_waits_oct1_9.py"
_spec = importlib.util.spec_from_file_location("backfill_live_waits_oct1_9", _SCRIPT)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

SCHEMA_PK = """
    CREATE TABLE live_waits (
        entity_code     VARCHAR NOT NULL,
        observed_at     TIMESTAMP WITH TIME ZONE NOT NULL,
        wait_time_type  VARCHAR DEFAULT 'POSTED',
        wait_time_minutes INTEGER NOT NULL,
        park_date       DATE NOT NULL,
        inserted_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (entity_code, observed_at, wait_time_type)
    )
"""
SCHEMA_NO_PK = SCHEMA_PK.replace(",\n        PRIMARY KEY (entity_code, observed_at, wait_time_type)", "")
assert "PRIMARY KEY" not in SCHEMA_NO_PK

COLS = ["entity_code", "observed_at", "wait_time_type", "wait_time_minutes"]
KEY = "2030-01-02 03:04:05.000006"
ET = bf.ET
SAFE = datetime(2026, 10, 24, 14, 0, tzinfo=ET)
DAYS = [f"2026-10-{d:02d}" for d in range(1, 10)]
# per day: 4 expected POSTED keys (day 01 has a second park file with 2 more -> 6); 38 in total
EXPECTED_TOTAL = 6 + 8 * 4


def at(h: int, m: int) -> datetime:
    return datetime(2026, 10, 24, h, m, tzinfo=ET)


def ts(day: str, hhmm: str) -> str:
    return f"{day}T{hhmm}:00+00:00"


def write_csv(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=COLS).to_csv(path, index=False)


def day_rows(day: str) -> list[tuple]:
    """8 POSTED (expected 4) + 1 PRIORITY + 1 ACTUAL + 1 blank-type row."""
    return [
        ("A", ts(day, "14:00"), "POSTED", 10),
        ("A", ts(day, "15:00"), "POSTED", 11),
        ("B", ts(day, "14:00"), "POSTED", 12),
        ("B", ts(day, "15:00"), "POSTED", 13),
        ("C", ts(day, "14:00"), "POSTED", 0),    # nonpositive
        ("", ts(day, "16:00"), "POSTED", 20),    # null key
        ("A", ts(day, "14:00"), "POSTED", 99),   # duplicate key
        ("A", ts(day, "08:00"), "POSTED", 7),    # 04:00 ET: park_date is the day before -> mismatch
        ("A", ts(day, "17:00"), "PRIORITY", 15),
        ("A", ts(day, "18:00"), "ACTUAL", 9),
        ("D", ts(day, "14:00"), "", 5),          # blank type -> excluded_other
    ]


def scraper_row(db: Path, entity: str, hhmm: str, day: str, typ: str = "POSTED", inserted_at: str | None = None):
    con = duckdb.connect(str(db))
    con.execute(
        "INSERT INTO live_waits (entity_code, observed_at, wait_time_type, wait_time_minutes, park_date, inserted_at) "
        "VALUES (?, ?::TIMESTAMPTZ, ?, 9, ?::DATE, ?::TIMESTAMP)", [entity, ts(day, hhmm), typ, day, inserted_at])
    con.close()


@pytest.fixture(params=["pk", "no_pk"])
def env(tmp_path, request):
    base = tmp_path / "out"
    clean = base / "fact_tables" / "clean" / "2026-10"
    for day in DAYS:
        write_csv(clean / f"mk_{day}.csv", day_rows(day))
    write_csv(clean / "ep_2026-10-01.csv", [("E1", ts("2026-10-01", "14:00"), "POSTED", 5),
                                             ("E1", ts("2026-10-01", "15:00"), "POSTED", 6)])
    write_csv(clean / "mk_2026-10-10.csv", day_rows("2026-10-10"))  # outside the fixed days: never touched
    db = base / "tpcr_live.duckdb"
    con = duckdb.connect(str(db))
    con.execute(SCHEMA_PK if request.param == "pk" else SCHEMA_NO_PK)
    con.close()
    return {"base": base, "clean": clean, "db": db}


def run(env, *extra, now=SAFE) -> int:
    return bf.main(["--output-base", str(env["base"]), *extra], now=now)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def counts(db: Path) -> dict:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return {str(d): n for d, n in con.execute(
            "SELECT park_date, count(*) FROM live_waits GROUP BY 1 ORDER BY 1").fetchall()}
    finally:
        con.close()


def total(out: str, field: str) -> int:
    return int(re.search(rf"^TOTAL: .*\b{field}=([\d,]+)", out, re.M).group(1).replace(",", ""))


def drop_inserted_at(db: Path) -> None:
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE lw2 AS SELECT entity_code, observed_at, wait_time_type, wait_time_minutes, park_date "
                "FROM live_waits")
    con.execute("DROP TABLE live_waits")
    con.execute("ALTER TABLE lw2 RENAME TO live_waits")
    con.close()


# ---------------------------------------------------------------- AC1 missing days


def test_ac1_missing_days_listed_exit4_before_db_access(env, capsys, monkeypatch):
    (env["clean"] / "mk_2026-10-04.csv").unlink()
    (env["clean"] / "mk_2026-10-07.csv").unlink()
    before = sha(env["db"])

    class NoDb:
        def connect(self, *a, **k):
            raise AssertionError("DB accessed")

    monkeypatch.setattr(bf, "duckdb", NoDb())
    assert run(env, "--apply") == 4
    assert "MISSING DAYS: 2026-10-04,2026-10-07" in capsys.readouterr().out
    monkeypatch.undo()
    assert sha(env["db"]) == before


def test_ac1_no_missing_days_prints_none(env, capsys):
    assert run(env) == 0
    assert "MISSING DAYS: none" in capsys.readouterr().out


# ---------------------------------------------------------------- AC2/AC3 POSTED filter, reconciliation


def test_ac2_posted_only_and_excluded_counts(env, capsys):
    assert run(env, "--apply") == 0
    out = capsys.readouterr().out
    assert out.count("excluded_PRIORITY=1 excluded_ACTUAL=1 excluded_other=1 ") == 9
    con = duckdb.connect(str(env["db"]), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM live_waits WHERE wait_time_type <> 'POSTED'").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM live_waits").fetchone()[0] == EXPECTED_TOTAL
    finally:
        con.close()


def test_ac3_reconciliation_null_key_and_nothing_outside_days(env, capsys):
    assert run(env) == 0
    out = capsys.readouterr().out
    day1 = re.search(r"^  2026-10-01: .*$", out, re.M).group(0)
    assert ("clean_posted=10 excluded_PRIORITY=1 excluded_ACTUAL=1 excluded_other=1 null_key=1 "
            "bad_observed_at=0 wait_nonpositive=1 duplicate_keys=1 park_date_mismatch=1 expected=6 ") in day1
    assert ("2026-10-01 reconcile: clean_posted=10 == expected=6 + drops=4 (null_key=1 + bad_observed_at=0 + "
            "wait_nonpositive=1 + duplicate_keys=1 + park_date_mismatch=1) OK") in out
    assert "2026-10-02 reconcile: clean_posted=8 == expected=4 + drops=4" in out
    assert out.count(" OK\n") == 9 and "MISMATCH" not in out
    assert f"TOTAL: expected={EXPECTED_TOTAL} " in out
    assert run(env, "--apply") == 0
    c = counts(env["db"])
    assert set(c) == set(DAYS) and "2026-09-30" not in c and "2026-10-10" not in c


def test_ac3_each_drop_class_counted_and_reconciles(env, capsys):
    assert run(env) == 0
    out = capsys.readouterr().out
    for day in DAYS:
        line = re.search(rf"^  {day}: .*$", out, re.M).group(0)
        for c in bf.DROP_CLASSES:
            # one row of each class per day (day_rows), except bad_observed_at (own test)
            assert f"{c}={0 if c == 'bad_observed_at' else 1} " in line
        assert f"{day} reconcile:" in out
    assert out.index("2026-10-01 reconcile:") < out.index("2026-10-01: clean_posted")


def test_bad_observed_at_counted(env, capsys):
    path = env["clean"] / "mk_2026-10-04.csv"
    write_csv(path, day_rows("2026-10-04") + [("A", "not-a-time", "POSTED", 10)])
    assert run(env) == 0
    out = capsys.readouterr().out
    line = re.search(r"^  2026-10-04: .*$", out, re.M).group(0)
    assert "clean_posted=9 " in line and "bad_observed_at=1 " in line and "wait_nonpositive=1 " in line
    assert "2026-10-04 reconcile: clean_posted=9 == expected=4 + drops=5" in out
    assert "MISMATCH" not in out and "RECONCILE FAILED" not in out


def test_reconcile_row_in_no_class_exits_7(env, capsys):
    path = env["clean"] / "mk_2026-10-04.csv"
    write_csv(path, day_rows("2026-10-04") + [("A", ts("2026-10-04", "19:00"), "POSTED", "abc")])
    before = sha(env["db"])
    assert run(env, "--apply") == bf.EXIT_RECONCILE
    out = capsys.readouterr().out
    assert "2026-10-04 reconcile:" in out and "MISMATCH" in out and "RECONCILE FAILED" in out
    assert "LOAD KEY" not in out
    assert counts(env["db"]) == {} and sha(env["db"]) == before


@pytest.mark.parametrize("extra", [(), ("--apply",)])
def test_ac3_forced_reconcile_mismatch_exits_nonzero_and_writes_nothing(env, capsys, monkeypatch, extra):
    orig = bf.read_day

    def skewed(files, day):
        st, df = orig(files, day)
        if day == "2026-10-03":
            st["duplicate_keys"] += 1  # a drop that never happened: clean_posted != expected + drops
        return st, df

    monkeypatch.setattr(bf, "read_day", skewed)
    before = sha(env["db"])
    assert run(env, *extra) == 7
    out = capsys.readouterr().out
    assert "2026-10-03 reconcile:" in out and "MISMATCH" in out and "RECONCILE FAILED" in out
    assert "LOAD KEY" not in out and "TOTAL:" not in out
    assert sha(env["db"]) == before and counts(env["db"]) == {}


# ---------------------------------------------------------------- AC4/AC6 dry run default, rerun


def test_ac6_dry_run_is_default_and_writes_nothing(env, capsys):
    before = sha(env["db"])
    assert run(env) == 0
    out = capsys.readouterr().out
    assert "mode: DRY RUN" in out and "LOAD KEY" not in out
    day2 = re.search(r"^  2026-10-02: .*$", out, re.M).group(0)
    for field in ("clean_posted=8", "excluded_PRIORITY=1", "null_key=1", "already_in_live=0", "would_insert=4"):
        assert field in day2
    assert sha(env["db"]) == before and counts(env["db"]) == {}


def test_ac4_apply_then_rerun_inserts_zero_and_dry_run_shows_zero(env, capsys):
    assert run(env, "--apply") == 0
    first = capsys.readouterr().out
    assert total(first, "inserted") == EXPECTED_TOTAL
    c = counts(env["db"])
    assert run(env, "--apply") == 0
    again = capsys.readouterr().out
    assert total(again, "inserted") == 0 and total(again, "already_in_live") == EXPECTED_TOTAL
    assert run(env) == 0
    dry = capsys.readouterr().out
    assert len(re.findall(r"^  2026-10-0\d: .* would_insert=0$", dry, re.M)) == 9
    assert counts(env["db"]) == c


def test_ac4_pre_existing_key_not_duplicated(env, capsys):
    scraper_row(env["db"], "A", "14:00", "2026-10-02")
    assert run(env, "--apply") == 0
    out = capsys.readouterr().out
    assert "already_in_live=1 inserted=3" in out
    assert counts(env["db"])["2026-10-02"] == 4


# ---------------------------------------------------------------- AC5 load key


def test_ac5_single_nonnull_load_key_logged(env, capsys):
    assert run(env, "--apply") == 0
    out = capsys.readouterr().out
    keys = re.findall(r"LOAD KEY inserted_at=(\S+ \S+)", out)
    assert len(keys) == 2 and keys[0] == keys[1]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}", keys[0])
    con = duckdb.connect(str(env["db"]), read_only=True)
    try:
        n = con.execute("SELECT count(*) FROM live_waits WHERE inserted_at = CAST(? AS TIMESTAMP)",
                        [keys[0]]).fetchone()[0]
        distinct, nulls = con.execute(
            "SELECT count(DISTINCT inserted_at), count(*) FILTER (WHERE inserted_at IS NULL) FROM live_waits").fetchone()
    finally:
        con.close()
    assert n == total(out, "inserted") == EXPECTED_TOTAL and distinct == 1 and nulls == 0


def test_ac5_existing_key_refused(env, capsys):
    assert run(env, "--apply", "--load-key", KEY) == 0
    capsys.readouterr()
    before = counts(env["db"])
    assert run(env, "--apply", "--load-key", KEY) == 2
    assert "REFUSED" in capsys.readouterr().out
    assert counts(env["db"]) == before
    assert run(env, "--apply", "--load-key", "2030-01-02") == 2  # bad format


@pytest.mark.parametrize("extra", [(), ("--apply",)])
def test_ac5_no_inserted_at_refused_exit2(env, capsys, extra):
    drop_inserted_at(env["db"])
    assert run(env, *extra) == 2
    assert "REFUSED: live_waits has no inserted_at" in capsys.readouterr().out
    assert counts(env["db"]) == {}


# ---------------------------------------------------------------- AC7 verify


def test_ac7_verify_pass_exact(env, capsys):
    assert run(env, "--apply") == 0
    out = capsys.readouterr().out
    assert out.count("VERIFY PASS 2026-10-") == 9 and "VERIFY FAIL" not in out
    assert "VERIFY PASS 2026-10-01: live_posted=6 expected=6" in out
    assert "VERIFY PASS 2026-10-02: live_posted=4 expected=4" in out


def test_ac7_verify_fail_exit5_prints_rollback_command(env, capsys):
    for i in range(3):  # scraper rows with keys not in clean: live_posted 7 != 4
        scraper_row(env["db"], f"Z{i}", "20:00", "2026-10-05")
    assert run(env, "--apply", "--load-key", KEY) == 5
    out = capsys.readouterr().out
    assert "VERIFY FAIL 2026-10-05: live_posted=7 expected=4" in out and "VERIFY PASS 2026-10-04" in out
    cmd = f'python scripts/backfill_live_waits_oct1_9.py --output-base "{env["base"].resolve()}" --rollback --load-key \'{KEY}\''
    assert cmd + "\n" in out and cmd + " --apply" in out
    assert counts(env["db"])["2026-10-05"] == 7  # data stays loaded


# ---------------------------------------------------------------- AC8 windows, locks


MODES = [(), ("--apply",), ("--rollback", "--load-key", KEY), ("--rollback", "--load-key", KEY, "--apply")]


@pytest.mark.parametrize("when,name", [
    ((5, 30), "ETL"), ((6, 15), "ETL"), ((8, 59), "ETL"), ((4, 25), "replay"), ((4, 44), "replay")])
@pytest.mark.parametrize("mode", MODES)
def test_ac8_refused_inside_window_at_start(env, capsys, when, name, mode):
    before = sha(env["db"])
    assert run(env, *mode, now=at(*when)) == 8
    assert f"REFUSED: inside {name} window" in capsys.readouterr().out
    assert sha(env["db"]) == before


@pytest.mark.parametrize("when", [(5, 29), (9, 0), (4, 45), (4, 24)])
def test_ac8_accepted_at_boundaries(env, capsys, when):
    assert run(env, now=at(*when)) == 0


def test_ac8_window_uses_et_for_other_timezones(env):
    from datetime import timezone
    assert run(env, now=datetime(2026, 10, 24, 10, 15, tzinfo=timezone.utc)) == 8  # 06:15 EDT
    assert run(env, now=datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)) == 0   # 14:00 EDT


@pytest.mark.parametrize("when,name", [((6, 0), "ETL"), ((4, 35), "replay")])
def test_ac8_refused_mid_run_earlier_days_rollbackable(env, capsys, monkeypatch, when, name):
    calls = []

    def clock(now=None):
        calls.append(1)
        return SAFE if len(calls) <= 2 else at(*when)  # start check, day 01 pass; day 02 is inside the window

    monkeypatch.setattr(bf, "current_time", clock)
    assert run(env, "--apply", "--load-key", KEY) == 8
    out = capsys.readouterr().out
    assert f"REFUSED: inside {name} window" in out and "STOPPED before 2026-10-02" in out
    assert f"--rollback --load-key '{KEY}'" in out and "--apply" in out
    assert counts(env["db"]) == {"2026-10-01": 6}
    con = duckdb.connect(str(env["db"]), read_only=True)
    assert con.execute("SELECT count(*) FROM live_waits WHERE inserted_at = CAST(? AS TIMESTAMP)",
                       [KEY]).fetchone()[0] == 6
    con.close()
    monkeypatch.undo()
    assert run(env, "--rollback", "--load-key", KEY, "--apply") == 0
    assert counts(env["db"]) == {}


HOLDER = """
import duckdb, sys, time
con = duckdb.connect(sys.argv[1])
con.execute("BEGIN")
print("READY", flush=True)
time.sleep(60)
"""


@pytest.fixture
def lock_holder(env):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(env["db"])], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "READY"
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.parametrize("mode", [(), ("--apply",), ("--rollback", "--load-key", KEY),
                                  ("--rollback", "--load-key", KEY, "--apply")])
def test_ac8_locked_db_exits_9_immediately(env, lock_holder, capsys, mode):
    t0 = time.monotonic()
    assert run(env, *mode) == 9
    assert time.monotonic() - t0 < 5
    out = capsys.readouterr().out
    assert "LOCKED:" in out and "no retry" in out


# ---------------------------------------------------------------- AC9 rollback


def _seed_scraper_rows(db: Path) -> None:
    scraper_row(db, "A", "14:00", "2026-10-02")                                   # POSTED, already in live, NULL key
    scraper_row(db, "S1", "20:00", "2026-10-03", "PRIORITY")                      # NULL inserted_at
    scraper_row(db, "S2", "20:00", "2026-10-03", "PRIORITY", "2020-01-01 00:00:00")
    scraper_row(db, "S3", "20:00", "2026-10-12", "POSTED")                        # other day


def test_ac9_rollback_dry_run_count_equals_inserted_then_apply_deletes_only_key_rows(env, capsys):
    _seed_scraper_rows(env["db"])
    assert run(env, "--apply", "--load-key", KEY) == 0
    inserted = total(capsys.readouterr().out, "inserted")
    assert inserted == EXPECTED_TOTAL - 1
    before = sha(env["db"])
    assert run(env, "--rollback", "--load-key", KEY) == 0
    out = capsys.readouterr().out
    assert f"ROLLBACK DRY RUN: would delete {inserted} rows with inserted_at={KEY}" in out
    assert "2026-10-02: would delete=3" in out and "2026-10-01: would delete=6" in out
    assert sha(env["db"]) == before
    assert run(env, "--rollback", "--load-key", KEY, "--apply") == 0
    out = capsys.readouterr().out
    assert f"ROLLBACK: deleted {inserted} rows with inserted_at={KEY}" in out and "ROLLBACK VERIFY PASS" in out
    assert counts(env["db"]) == {"2026-10-02": 1, "2026-10-03": 2, "2026-10-12": 1}


def test_ac9_rollback_bad_args_exit2(env, capsys):
    assert run(env, "--rollback") == 2
    assert run(env, "--rollback", "--load-key", "nope") == 2
    drop_inserted_at(env["db"])
    assert run(env, "--rollback", "--load-key", KEY) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_ac9_removed_range_options_are_rejected(env):
    for flag in (["--start", "2026-10-01"], ["--glob", "x"], ["--dry-run"], ["--row-tol", "0.1"]):
        with pytest.raises(SystemExit):
            run(env, "--rollback", "--load-key", KEY, *flag)


# ---------------------------------------------------------------- AC10 checksums


def test_ac10_checksums_unchanged(env, capsys):
    files = sorted(env["clean"].glob("*.csv"))
    before = {f: sha(f) for f in files}
    assert run(env) == 0
    assert "CLEAN CHECKSUMS UNCHANGED (10 files)" in capsys.readouterr().out
    assert run(env, "--apply") == 0
    assert "CLEAN CHECKSUMS UNCHANGED (10 files)" in capsys.readouterr().out
    assert {f: sha(f) for f in files} == before


def test_ac10_change_before_write_aborts_exit6(env, capsys, monkeypatch):
    orig = bf.read_day
    target = env["clean"] / "mk_2026-10-06.csv"

    def read_then_modify(files, day):
        r = orig(files, day)
        if day == "2026-10-09":
            with open(target, "a") as fh:
                fh.write("A,2026-10-06T20:00:00+00:00,POSTED,5\n")
        return r

    monkeypatch.setattr(bf, "read_day", read_then_modify)
    assert run(env, "--apply") == 6
    out = capsys.readouterr().out
    assert f"CHECKSUM CHANGED: {target.resolve()}" in out and "CLEAN CHECKSUMS UNCHANGED" not in out
    assert counts(env["db"]) == {}


def test_ac10_change_at_end_exit6(env, capsys, monkeypatch):
    orig = bf.load_days
    target = env["clean"] / "mk_2026-10-06.csv"

    def load_then_modify(*a, **k):
        r = orig(*a, **k)
        with open(target, "a") as fh:
            fh.write("\n")
        return r

    monkeypatch.setattr(bf, "load_days", load_then_modify)
    assert run(env) == 6
    out = capsys.readouterr().out
    assert "CHECKSUM CHANGED:" in out and "CLEAN CHECKSUMS UNCHANGED" not in out


# ---------------------------------------------------------------- path safety


def test_symlinked_month_dir_refused(env, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(env["clean"]), str(elsewhere))
    env["clean"].symlink_to(elsewhere, target_is_directory=True)
    before = sha(env["db"])
    assert run(env, "--apply") == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before


def test_symlinked_clean_root_refused(env, tmp_path, capsys):
    clean_root = env["base"] / "fact_tables" / "clean"
    external = tmp_path / "external_disk" / "clean"
    external.parent.mkdir()
    shutil.move(str(clean_root), str(external))
    clean_root.symlink_to(external, target_is_directory=True)
    before = sha(env["db"])
    assert run(env, "--apply") == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before


def test_symlinked_fact_tables_refused(env, tmp_path, capsys):
    fact = env["base"] / "fact_tables"
    other = tmp_path / "other_tree" / "fact_tables"
    other.parent.mkdir()
    shutil.move(str(fact), str(other))
    fact.symlink_to(other, target_is_directory=True)
    before = sha(env["db"])
    assert run(env, "--apply") == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before and counts(env["db"]) == {}


def test_symlink_inside_clean_refused(env, capsys):
    real = env["clean"].parent / "_real_2026-10"  # inside clean, so the resolve() check passes
    env["clean"].rename(real)
    env["clean"].symlink_to(real, target_is_directory=True)
    assert env["clean"].resolve().is_relative_to((env["base"] / "fact_tables" / "clean").resolve())
    before = sha(env["db"])
    assert run(env, "--apply") == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before and counts(env["db"]) == {}


def test_dry_run_opens_db_read_only(env, monkeypatch):
    real_connect = duckdb.connect
    calls = []

    def recording(*a, **k):
        calls.append(k)
        return real_connect(*a, **k)

    monkeypatch.setattr(bf.duckdb, "connect", recording)
    assert run(env) == 0
    assert calls and all(k.get("read_only") is True for k in calls)


def test_symlinked_file_refused(env, tmp_path):
    outside = tmp_path / "outside.csv"
    shutil.move(str(env["clean"] / "mk_2026-10-03.csv"), str(outside))
    (env["clean"] / "mk_2026-10-03.csv").symlink_to(outside)
    before = sha(env["db"])
    assert run(env, "--apply") == 2
    assert sha(env["db"]) == before


def test_offpath_dir_symlink_under_clean_refused(env, tmp_path, capsys):
    outside = tmp_path / "outside_dir"
    write_csv(outside / "mk_2026-09-01.csv", day_rows("2026-09-01"))
    (env["clean"].parent / "2026-09").symlink_to(outside, target_is_directory=True)
    before = sha(env["db"])
    assert run(env, "--apply") == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before and counts(env["db"]) == {}


@pytest.mark.parametrize("mode", [["--apply"], []])
def test_offpath_file_symlink_under_clean_refused(env, tmp_path, capsys, mode):
    outside = tmp_path / "outside_10.csv"
    shutil.move(str(env["clean"] / "mk_2026-10-10.csv"), str(outside))
    (env["clean"] / "mk_2026-10-10.csv").symlink_to(outside)
    before = sha(env["db"])
    assert run(env, *mode) == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before and counts(env["db"]) == {}


CLASSES = ("null_key", "bad_observed_at", "wait_nonpositive", "duplicate_keys", "park_date_mismatch")
D4 = "2026-10-04"


@pytest.mark.parametrize("extra, want", [
    pytest.param(("", "not-a-time", "POSTED", 10),
                 dict(null_key=2, bad_observed_at=0, wait_nonpositive=1, duplicate_keys=1, park_date_mismatch=1),
                 id="null_key+bad_observed_at"),
    pytest.param(("", ts(D4, "19:00"), "POSTED", 0),
                 dict(null_key=2, bad_observed_at=0, wait_nonpositive=1, duplicate_keys=1, park_date_mismatch=1),
                 id="null_key+wait_nonpositive"),
    pytest.param(("A", "not-a-time", "POSTED", 0),
                 dict(null_key=1, bad_observed_at=1, wait_nonpositive=1, duplicate_keys=1, park_date_mismatch=1),
                 id="bad_observed_at+wait_nonpositive"),
    pytest.param(("B", ts(D4, "14:00"), "POSTED", 0),
                 dict(null_key=1, bad_observed_at=0, wait_nonpositive=2, duplicate_keys=1, park_date_mismatch=1),
                 id="wait_nonpositive+duplicate"),
    pytest.param(("A", ts(D4, "08:00"), "POSTED", 8),
                 dict(null_key=1, bad_observed_at=0, wait_nonpositive=1, duplicate_keys=2, park_date_mismatch=1),
                 id="duplicate+wrong_day"),
    pytest.param(("", ts(D4, "08:30"), "POSTED", 5),
                 dict(null_key=2, bad_observed_at=0, wait_nonpositive=1, duplicate_keys=1, park_date_mismatch=1),
                 id="null_key+wrong_day"),
])
def test_r2_class_order_earlier_class_wins(env, capsys, extra, want):
    write_csv(env["clean"] / f"mk_{D4}.csv", day_rows(D4) + [extra])
    assert run(env) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith(f"  {D4}: "))
    for c in CLASSES:
        assert f" {c}={want[c]} " in line, (c, line)
    assert " expected=4 " in line
    assert "MISMATCH" not in out


def test_bad_file_name_and_park_code_refused(env, capsys):
    write_csv(env["clean"] / "a_b_2026-10-03.csv", day_rows("2026-10-03"))
    assert run(env) == 2
    (env["clean"] / "a_b_2026-10-03.csv").unlink()
    write_csv(env["clean"] / "zz_2026-10-03.csv", day_rows("2026-10-03"))
    assert run(env) == 2
    assert "REFUSED" in capsys.readouterr().out


# ---------------------------------------------------------------- AC13 docs in sync


def test_ac13_rollout_doc_matches_code():
    doc = (_SCRIPT.parent.parent / "docs" / "rollout" / "TPCR-BACKFILL-OCT1-9.md").read_text()
    flags = [o for a in bf.build_parser()._actions for o in a.option_strings if o != "-h" and o != "--help"]
    assert sorted(flags) == ["--apply", "--db", "--load-key", "--output-base", "--rollback"]
    for flag in flags:
        assert flag in doc, flag
    for code in (bf.EXIT_OK, bf.EXIT_PATH, bf.EXIT_MISSING, bf.EXIT_VERIFY, bf.EXIT_CHECKSUM,
                 bf.EXIT_RECONCILE, bf.EXIT_WINDOW, bf.EXIT_LOCKED):
        assert f"| {code} |" in doc, code
    assert bf.ETL_WINDOW_ET == (dtime(5, 30), dtime(9, 0)) and bf.REPLAY_WINDOW_ET == (dtime(4, 25), dtime(4, 45))
    assert bf.DROP_CLASSES == ("null_key", "bad_observed_at", "wait_nonpositive", "duplicate_keys",
                               "park_date_mismatch")
    readme = (_SCRIPT.parent / "README.md").read_text()
    for text in (doc, readme[readme.index("### `backfill_live_waits_oct1_9.py`"):]):
        assert "05:30-09:00" in text and "04:25-04:45" in text and "reconcile" in text
        assert "dropped_nonpositive_or_unparseable" not in text
        assert "05:30-07:00" not in text and "04:30-04:40" not in text
    assert "clean_posted == expected + sum(drops)" in doc and "exit 9" in doc and "no marker" in doc
    for s in ("MISSING DAYS:", "CHECKSUM CHANGED:", "CLEAN CHECKSUMS UNCHANGED", "LOAD KEY inserted_at=",
              "ROLLBACK DRY RUN: would delete", "ROLLBACK VERIFY PASS", "VERIFY PASS", "VERIFY FAIL",
              "REFUSED: inside", "LOCKED:", "excluded_PRIORITY=", "excluded_ACTUAL=", "excluded_other=",
              "null_key=", "bad_observed_at=", "wait_nonpositive=", "duplicate_keys=", "park_date_mismatch=",
              "expected=", "already_in_live=", "would_insert=", "05:30", "09:00", "04:25", "04:45", "reconcile"):
        assert s in doc, s
    for gone in ("--parity-date", "--glob", "--start", "--end", "--allow-missing", "--row-tol", "--dry-run",
                 "--entity-tol", "--tp-posted-tol", "HOLD"):
        assert gone not in doc, gone
