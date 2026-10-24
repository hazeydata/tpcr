"""Tests for scripts/backfill_live_waits_oct1_9.py (TPCR-BACKFILL-OCT1-9). tmp_path only; no real data."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
from datetime import datetime, timedelta, timezone
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
BASE = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)  # 10:00 ET: park date 2026-10-12


def ts(day: str, hhmm: str) -> str:
    return f"{day}T{hhmm}:00+00:00"


def write_csv(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=COLS).to_csv(path, index=False)


def parity_rows() -> list[tuple]:
    """100 POSTED (5 entities x 20) + 10 PRIORITY (2 entities x 5) on 2026-10-12."""
    rows = []
    for e in range(5):
        for i in range(20):
            rows.append((f"P{e}", (BASE + timedelta(minutes=i)).isoformat(), "POSTED", 10 + i))
    for e in range(2):
        for i in range(5):
            rows.append((f"P{e}", (BASE + timedelta(hours=2, minutes=i)).isoformat(), "PRIORITY", 15))
    return rows


def day_rows(day: str) -> list[tuple]:
    return [
        ("A", ts(day, "14:00"), "POSTED", 10),
        ("A", ts(day, "15:00"), "POSTED", 11),
        ("B", ts(day, "14:00"), "POSTED", 12),
        ("B", ts(day, "15:00"), "POSTED", 13),
        ("C", ts(day, "14:00"), "POSTED", 0),  # dropped: wait <= 0
        ("", ts(day, "16:00"), "POSTED", 20),  # dropped: null key
    ]


def insert_live(db: Path, rows: list[tuple], park_date: str) -> None:
    con = duckdb.connect(str(db))
    for ec, obs, typ, w in rows:
        con.execute(
            "INSERT INTO live_waits (entity_code, observed_at, wait_time_type, wait_time_minutes, park_date) "
            "VALUES (?, ?::TIMESTAMPTZ, ?, ?, ?::DATE)",
            [ec, obs, typ, w, park_date],
        )
    con.close()


@pytest.fixture(params=["pk", "no_pk"])
def env(tmp_path, request):
    base = tmp_path / "out"
    clean = base / "fact_tables" / "clean" / "2026-10"
    write_csv(clean / "mk_2026-10-01.csv", day_rows("2026-10-01"))
    write_csv(clean / "ep_2026-10-01.csv", [("E1", ts("2026-10-01", "14:00"), "POSTED", 5),
                                             ("E1", ts("2026-10-01", "15:00"), "POSTED", 6)])
    # 08:00Z = 04:00 ET -> before 6 AM, belongs to park date 10-01 despite the 10-02 filename
    write_csv(clean / "mk_2026-10-02.csv", day_rows("2026-10-02")[:4] + [("A", ts("2026-10-02", "08:00"), "POSTED", 7)])
    write_csv(clean / "mk_2026-10-03.csv", day_rows("2026-10-03")[:4])
    write_csv(clean / "mk_2026-10-12.csv", parity_rows())
    db = base / "tpcr_live.duckdb"
    con = duckdb.connect(str(db))
    con.execute(SCHEMA_PK if request.param == "pk" else SCHEMA_NO_PK)
    con.close()
    insert_live(db, parity_rows(), "2026-10-12")
    insert_live(db, [("A", ts("2026-10-01", "14:00"), "POSTED", 10)], "2026-10-01")  # already present
    return {"base": base, "clean": clean, "db": db}


def run(env, *extra) -> int:
    return bf.main(["--output-base", str(env["base"]), *extra])


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def counts(db: Path) -> dict:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return {str(d): n for d, n in con.execute(
            "SELECT park_date, count(*) FROM live_waits GROUP BY 1 ORDER BY 1").fetchall()}
    finally:
        con.close()


def delete_live(db: Path, where: str) -> None:
    con = duckdb.connect(str(db))
    con.execute(f"DELETE FROM live_waits WHERE {where}")
    con.close()


# ---------------------------------------------------------------- parity


def test_parity_pass_dry_run_changes_nothing_and_reports_counts(env, capsys):
    before = sha(env["db"])
    assert run(env, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "PARITY PASS" in out
    assert "2026-10-01: files=2 raw=8 prepped=6 distinct_keys=6 already_present=1 would insert=5" in out
    assert "2026-10-02: files=1 raw=5 prepped=5 distinct_keys=5 already_present=0 would insert=5" in out
    assert "derived_park_date_mismatch=1" in out  # the pre-6AM row in the 10-02 file
    assert "2026-10-03: files=1 raw=4 prepped=4 distinct_keys=4 already_present=0 would insert=4" in out
    assert sha(env["db"]) == before


def test_parity_fail_row_count(env, capsys):
    delete_live(env["db"], "entity_code = 'P0' AND wait_time_type = 'POSTED' AND wait_time_minutes < 16")
    before = sha(env["db"])
    assert run(env) == 3
    out = capsys.readouterr().out
    assert "PARITY FAIL" in out and "a. rows" in out and "FAIL" in out.split("a. rows")[1].splitlines()[0]
    assert sha(env["db"]) == before


def test_parity_fail_entity_set_even_in_dry_run(env, capsys):
    delete_live(env["db"], "entity_code = 'P4'")
    before = sha(env["db"])
    assert run(env, "--dry-run") == 3
    out = capsys.readouterr().out
    assert "only in clean=1" in out and "PARITY FAIL" in out
    assert sha(env["db"]) == before


def test_parity_fail_type_mix_actual_in_clean_only(env, capsys):
    extra = [(f"P{e}", (BASE + timedelta(hours=4, minutes=e)).isoformat(), "ACTUAL", 9) for e in range(5)]
    write_csv(env["clean"] / "mk_2026-10-12.csv", parity_rows() + extra)
    before = sha(env["db"])
    assert run(env) == 3
    out = capsys.readouterr().out
    assert "ACTUAL: clean=5 live=0 -> FAIL" in out
    assert sha(env["db"]) == before


def test_parity_fail_touringplans_posted_mixed_in(env, capsys):
    extra = [(f"P{e}", (BASE + timedelta(hours=6, minutes=e)).isoformat(), "POSTED", 30) for e in range(5)]
    write_csv(env["clean"] / "mk_2026-10-12.csv", parity_rows() + extra)
    before = sha(env["db"])
    assert run(env) == 3
    out = capsys.readouterr().out
    assert "clean POSTED not in live: 5/105" in out
    assert "unmatched clean POSTED: P0" in out
    assert sha(env["db"]) == before


# ---------------------------------------------------------------- load


def test_live_run_inserts_with_prep_and_second_run_inserts_zero(env, capsys):
    assert run(env) == 0
    out = capsys.readouterr().out
    assert "inserted=5" in out and "inserted=4" in out
    c = counts(env["db"])
    # 10-01: mk 4 valid (A@14:00 pre-existing) + ep 2 + early-morning row from the 10-02 file
    assert c["2026-10-01"] == 7
    assert c["2026-10-02"] == 4
    assert c["2026-10-03"] == 4
    assert c["2026-10-12"] == len(parity_rows())
    con = duckdb.connect(str(env["db"]), read_only=True)
    assert con.execute("SELECT count(*) FROM live_waits WHERE wait_time_minutes <= 0 OR entity_code = ''").fetchone()[0] == 0
    # pre-existing key not duplicated
    assert con.execute("SELECT count(*) FROM live_waits WHERE entity_code='A' AND park_date='2026-10-01' "
                       "AND wait_time_type='POSTED' AND observed_at = '2026-10-01T14:00:00+00:00'::TIMESTAMPTZ"
                       ).fetchone()[0] == 1
    con.close()

    assert run(env) == 0
    out = capsys.readouterr().out
    assert "inserted=0" in out and "inserted=5" not in out
    assert counts(env["db"]) == c
    # a dry run now would insert nothing
    assert run(env, "--dry-run") == 0
    assert "would insert=0" in capsys.readouterr().out
    assert counts(env["db"]) == c


def test_missing_parity_file_is_parity_failure(env, capsys):
    (env["clean"] / "mk_2026-10-12.csv").unlink()
    assert run(env) == 3
    assert "PARITY FAIL" in capsys.readouterr().out


# ---------------------------------------------------------------- path safety


def test_symlinked_month_dir_refused(env, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(env["clean"]), str(elsewhere))
    env["clean"].symlink_to(elsewhere, target_is_directory=True)
    before = sha(env["db"])
    assert run(env) == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before


def test_symlinked_clean_root_allowed(env, tmp_path, capsys):
    clean_root = env["base"] / "fact_tables" / "clean"
    external = tmp_path / "external_disk" / "clean"
    external.parent.mkdir()
    shutil.move(str(clean_root), str(external))
    clean_root.symlink_to(external, target_is_directory=True)
    assert run(env, "--dry-run") == 0
    assert "PARITY PASS" in capsys.readouterr().out


def test_symlinked_file_refused(env, tmp_path, capsys):
    outside = tmp_path / "outside.csv"
    shutil.move(str(env["clean"] / "mk_2026-10-03.csv"), str(outside))
    (env["clean"] / "mk_2026-10-03.csv").symlink_to(outside)
    before = sha(env["db"])
    assert run(env, "--dry-run") == 2
    assert sha(env["db"]) == before


def test_raw_glob_refused(env, capsys):
    raw = env["base"] / "fact_tables" / "raw" / "2026-10"
    write_csv(raw / "mk_2026-10-01.csv", day_rows("2026-10-01"))
    before = sha(env["db"])
    assert run(env, "--glob", "fact_tables/raw/2026-10/*_2026-10-0[1-9].csv") == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before


def test_dotdot_glob_refused(env):
    assert run(env, "--glob", "fact_tables/clean/../raw/*.csv") == 2


# ---------------------------------------------------------------- --start/--end range (Oct 14-22)

RANGE = ("--start", "2026-10-14", "--end", "2026-10-22", "--parity-date", "2026-10-23")


def _range_env(env):
    """Clean files for 10-13..10-23 (4 valid rows each); 10-23 is a parity day that matches live exactly."""
    for d in range(13, 24):
        day = f"2026-10-{d:02d}"
        write_csv(env["clean"] / f"mk_{day}.csv", day_rows(day)[:4])
    p23 = [(e, o.replace("2026-10-12", "2026-10-23"), t, w) for e, o, t, w in parity_rows()]
    write_csv(env["clean"] / "mk_2026-10-23.csv", p23)
    insert_live(env["db"], p23, "2026-10-23")
    return env


def test_range_selects_exactly_14_to_22(env):
    _range_env(env)
    root = env["base"] / "fact_tables" / "clean"
    files = bf.resolve_range_targets(env["base"], "2026-10-14", "2026-10-22", "2026-10-23", root)
    assert sorted(f.name for f in files) == [f"mk_2026-10-{d}.csv" for d in range(14, 23)]


def test_range_dry_run_then_load_only_range_days(env, capsys):
    _range_env(env)
    before = sha(env["db"])
    assert run(env, *RANGE, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "PARITY PASS" in out and "target files: 9" in out
    assert "2026-10-13: files" not in out and "2026-10-23: files" not in out
    assert sha(env["db"]) == before
    assert run(env, *RANGE) == 0
    c = counts(env["db"])
    assert c["2026-10-14"] == 4 and c["2026-10-22"] == 4
    assert "2026-10-13" not in c
    assert c["2026-10-23"] == len(parity_rows())


def test_month_spanning_range(env):
    root = env["base"] / "fact_tables" / "clean"
    write_csv(root / "2026-09" / "mk_2026-09-30.csv", day_rows("2026-09-30")[:4])
    files = bf.resolve_range_targets(env["base"], "2026-09-29", "2026-10-01", "2026-10-12", root)
    assert sorted(f.name for f in files) == ["ep_2026-10-01.csv", "mk_2026-09-30.csv", "mk_2026-10-01.csv"]


@pytest.mark.parametrize("extra", [
    ("--start", "2026-10-22", "--end", "2026-10-14"),
    ("--start", "2026-10-14"),
    ("--end", "2026-10-14"),
    ("--start", "2026/10/14", "--end", "2026-10-22"),
    ("--start", "2026-02-01", "--end", "2026-02-30"),
    ("--glob", "fact_tables/clean/2026-10/*_2026-10-03.csv", "--start", "2026-10-14", "--end", "2026-10-22"),
    ("--start", "2026-10-10", "--end", "2026-10-14", "--parity-date", "2026-10-12"),
])
def test_range_bad_args_exit_2(env, extra, capsys):
    before = sha(env["db"])
    assert run(env, *extra) == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before


def test_range_symlinked_file_refused(env, tmp_path):
    _range_env(env)
    outside = tmp_path / "outside.csv"
    shutil.move(str(env["clean"] / "mk_2026-10-17.csv"), str(outside))
    (env["clean"] / "mk_2026-10-17.csv").symlink_to(outside)
    before = sha(env["db"])
    assert run(env, *RANGE, "--dry-run") == 2
    assert sha(env["db"]) == before


def test_range_run_idempotent(env, capsys):
    _range_env(env)
    assert run(env, *RANGE) == 0
    c = counts(env["db"])
    capsys.readouterr()
    assert run(env, *RANGE) == 0
    out = capsys.readouterr().out
    assert "inserted=0" in out and "inserted=4" not in out
    assert counts(env["db"]) == c


def test_range_partially_present_day_inserts_only_missing(env, capsys):
    _range_env(env)
    insert_live(env["db"], [("A", ts("2026-10-15", "14:00"), "POSTED", 10),
                            ("B", ts("2026-10-15", "15:00"), "POSTED", 13)], "2026-10-15")
    assert run(env, *RANGE, "--dry-run") == 0
    assert ("2026-10-15: files=1 raw=4 prepped=4 distinct_keys=4 already_present=2 would insert=2"
            in capsys.readouterr().out)
    assert run(env, *RANGE) == 0
    assert "already_present=2 inserted=2" in capsys.readouterr().out
    assert counts(env["db"])["2026-10-15"] == 4
