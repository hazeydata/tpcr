"""Tests for scripts/backfill_live_waits_oct1_9.py (TPCR-BACKFILL-OCT1-9). tmp_path only; no real data."""

from __future__ import annotations

import hashlib
import importlib.util
import re
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


# ---------------------------------------------------------------- TPCR-RECOVER-OCT14-22 acceptance criteria

PARITY12 = ("--parity-date", "2026-10-12")
R = ("--start", "2026-10-14", "--end", "2026-10-22", *PARITY12)
KEY = "2030-01-02 03:04:05.000006"


def _renv(env):
    """Clean files for 10-14..10-22 (4 valid rows each); parity day is 10-12 (already in the fixture)."""
    for d in range(14, 23):
        day = f"2026-10-{d:02d}"
        write_csv(env["clean"] / f"mk_{day}.csv", day_rows(day)[:4])
    return env


def _inserted_total(out: str) -> int:
    """Sum of the per-day `inserted=` values (day lines only, not TOTAL)."""
    return sum(int(x) for x in re.findall(r"^  \d{4}-\d{2}-\d{2}: .*already_present=\d+ inserted=(\d+)", out, re.M))


def _drop_inserted_at(db: Path) -> None:
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE lw2 AS SELECT entity_code, observed_at, wait_time_type, wait_time_minutes, park_date "
                "FROM live_waits")
    con.execute("DROP TABLE live_waits")
    con.execute("ALTER TABLE lw2 RENAME TO live_waits")
    con.close()


def _insert_scraper(db: Path, entity: str, hhmm: str, day: str, inserted_at: str | None) -> None:
    con = duckdb.connect(str(db))
    con.execute(
        "INSERT INTO live_waits (entity_code, observed_at, wait_time_type, wait_time_minutes, park_date, inserted_at) "
        "VALUES (?, ?::TIMESTAMPTZ, 'POSTED', 9, ?::DATE, ?::TIMESTAMP)",
        [entity, ts(day, hhmm), day, inserted_at])
    con.close()


def test_ac1_missing_day_listed_exit4(env, capsys, monkeypatch):
    _renv(env)
    (env["clean"] / "mk_2026-10-17.csv").unlink()
    (env["clean"] / "mk_2026-10-19.csv").unlink()
    before = sha(env["db"])

    class NoDb:
        def connect(self, *a, **k):
            raise AssertionError("DB accessed")

    monkeypatch.setattr(bf, "duckdb", NoDb())
    assert run(env, *R, "--dry-run") == 4
    assert "MISSING DAYS: 2026-10-17,2026-10-19" in capsys.readouterr().out
    monkeypatch.undo()
    assert sha(env["db"]) == before


def test_ac1_allow_missing(env, capsys):
    _renv(env)
    (env["clean"] / "mk_2026-10-17.csv").unlink()
    (env["clean"] / "mk_2026-10-19.csv").unlink()
    assert run(env, *R, "--allow-missing") == 0
    out = capsys.readouterr().out
    assert "MISSING DAYS: 2026-10-17,2026-10-19" in out and "target files: 7" in out
    c = counts(env["db"])
    assert c["2026-10-18"] == 4 and "2026-10-17" not in c and "2026-10-19" not in c
    write_csv(env["clean"] / "mk_2026-10-17.csv", day_rows("2026-10-17")[:4])
    write_csv(env["clean"] / "mk_2026-10-19.csv", day_rows("2026-10-19")[:4])
    assert run(env, *R, "--dry-run") == 0
    assert "MISSING DAYS: none" in capsys.readouterr().out


def test_ac1_all_days_missing_exit2_even_with_allow_missing(env, capsys):
    assert run(env, *R, "--allow-missing") == 2
    out = capsys.readouterr().out
    assert "MISSING DAYS: 2026-10-14,2026-10-15" in out and "REFUSED" in out


def test_ac2_parity_fail_prints_would_insert_exit3_no_write(env, capsys):
    _renv(env)
    delete_live(env["db"], "entity_code = 'P4'")
    before = sha(env["db"])
    assert run(env, *R, "--dry-run") == 3
    out = capsys.readouterr().out
    assert "PARITY FAIL" in out and "DRY RUN (parity failed; counts only)" in out
    assert out.count("would insert=4") == 9
    assert sha(env["db"]) == before
    # same for a non-dry run: counts only, nothing written
    assert run(env, *R) == 3
    out = capsys.readouterr().out
    assert "DRY RUN (parity failed; counts only)" in out and out.count("would insert=4") == 9
    assert sha(env["db"]) == before


def test_ac3_range_including_parity_refused_before_read(env, capsys, monkeypatch):
    _renv(env)

    def boom(*a, **k):
        raise AssertionError("read before refusal")

    monkeypatch.setattr(bf, "read_prepped", boom)
    monkeypatch.setattr(bf, "run_parity", boom)
    monkeypatch.setattr(bf, "hash_files", boom)
    before = sha(env["db"])
    assert run(env, "--start", "2026-10-14", "--end", "2026-10-22", "--parity-date", "2026-10-16") == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sha(env["db"]) == before


def test_ac4_verify_pass_after_load(env, capsys):
    _renv(env)
    assert run(env, *R, "--load-key", KEY) == 0
    out = capsys.readouterr().out
    assert out.count("VERIFY PASS 2026-10-") == 9 and "VERIFY FAIL" not in out


def test_ac4_verify_fail_exit5(env, capsys):
    _renv(env)
    for i in range(10):  # scraper rows with keys absent from clean: live >> clean for 10-15
        _insert_scraper(env["db"], f"Z{i}", "20:00", "2026-10-15", None)
    assert run(env, *R, "--load-key", KEY) == 5
    out = capsys.readouterr().out
    assert "VERIFY FAIL 2026-10-15" in out and "VERIFY PASS 2026-10-14" in out
    assert f"--rollback --load-key '{KEY}'" in out
    assert counts(env["db"])["2026-10-15"] == 14  # data stays loaded


def test_ac5_second_dry_run_zero(env, capsys):
    _renv(env)
    assert run(env, *R) == 0
    capsys.readouterr()
    assert run(env, *R, "--dry-run") == 0
    out = capsys.readouterr().out
    assert len(re.findall(r"^  2026-10-\d\d: .*would insert=0 ", out, re.M)) == 9 and "would insert=4" not in out
    assert "TOTAL:" in out and "would insert=0 " in out.split("TOTAL:")[1]


def test_ac6_checksums_unchanged_live_and_dry(env, capsys):
    _renv(env)
    files = sorted(env["clean"].glob("*.csv"))
    before = {f: sha(f) for f in files}
    assert run(env, *R, "--dry-run") == 0
    assert "CLEAN CHECKSUMS UNCHANGED (10 files)" in capsys.readouterr().out  # 9 targets + 1 parity file
    assert run(env, *R) == 0
    assert "CLEAN CHECKSUMS UNCHANGED (10 files)" in capsys.readouterr().out
    assert {f: sha(f) for f in files} == before


def test_ac6_checksum_change_aborts(env, capsys, monkeypatch):
    _renv(env)
    orig = bf.run_parity
    target = env["clean"] / "mk_2026-10-16.csv"

    def parity_then_modify(*a, **k):
        r = orig(*a, **k)
        with open(target, "a") as fh:
            fh.write("A,2026-10-16T20:00:00+00:00,POSTED,5\n")
        return r

    monkeypatch.setattr(bf, "run_parity", parity_then_modify)
    before = counts(env["db"])
    assert run(env, *R) == 6
    out = capsys.readouterr().out
    assert f"CHECKSUM CHANGED: {target.resolve()}" in out and "CLEAN CHECKSUMS UNCHANGED" not in out
    assert counts(env["db"]) == before


def test_ac6_checksum_change_at_end_exit6(env, capsys, monkeypatch):
    _renv(env)
    orig = bf.load_days
    target = env["clean"] / "mk_2026-10-16.csv"

    def load_then_modify(*a, **k):
        r = orig(*a, **k)
        with open(target, "a") as fh:
            fh.write("\n")
        return r

    monkeypatch.setattr(bf, "load_days", load_then_modify)
    assert run(env, *R, "--dry-run") == 6
    out = capsys.readouterr().out
    assert "CHECKSUM CHANGED:" in out and "CLEAN CHECKSUMS UNCHANGED" not in out


def test_ac7_single_nonnull_load_key_logged(env, capsys):
    _renv(env)
    assert run(env, *R) == 0
    out = capsys.readouterr().out
    keys = re.findall(r"LOAD KEY inserted_at=(\S+ \S+)", out)
    assert len(keys) == 2 and keys[0] == keys[1]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}", keys[0])
    con = duckdb.connect(str(env["db"]), read_only=True)
    try:
        n = con.execute("SELECT count(*) FROM live_waits WHERE inserted_at = CAST(? AS TIMESTAMP)",
                        [keys[0]]).fetchone()[0]
        distinct, nulls = con.execute(
            "SELECT count(DISTINCT inserted_at), count(*) FILTER (WHERE inserted_at IS NULL) FROM live_waits "
            "WHERE park_date BETWEEN '2026-10-14' AND '2026-10-22'").fetchone()
    finally:
        con.close()
    assert n == _inserted_total(out) == 36
    assert distinct == 1 and nulls == 0


def test_ac7_existing_key_refused(env, capsys):
    _renv(env)
    assert run(env, *R, "--load-key", KEY) == 0
    capsys.readouterr()
    before = counts(env["db"])
    assert run(env, *R, "--load-key", KEY) == 2
    assert "REFUSED" in capsys.readouterr().out
    assert counts(env["db"]) == before
    assert run(env, *R, "--load-key", "2030-01-02") == 2  # bad format


@pytest.mark.parametrize("extra", [(), ("--dry-run",)])
def test_ac7_hold_when_no_inserted_at(env, capsys, extra):
    _renv(env)
    _drop_inserted_at(env["db"])
    before = counts(env["db"])
    assert run(env, *R, *extra) == 7
    out = capsys.readouterr().out
    assert "HOLD: live_waits has no inserted_at column (OQ1); no live load" in out
    assert out.count("would insert=4") == 9 and out.index("would insert=4") < out.index("HOLD:")
    assert counts(env["db"]) == before


def test_ac7_parity_fail_takes_precedence_over_hold(env, capsys):
    _renv(env)
    _drop_inserted_at(env["db"])
    delete_live(env["db"], "entity_code = 'P4'")
    assert run(env, *R, "--dry-run") == 3
    assert "HOLD:" not in capsys.readouterr().out


def test_ac8_rollback_dry_run_count_equals_inserted(env, capsys):
    _renv(env)
    assert run(env, *R, "--load-key", KEY) == 0
    inserted = _inserted_total(capsys.readouterr().out)
    before = sha(env["db"])
    assert bf.main(["--output-base", str(env["base"]), "--rollback", "--load-key", KEY, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"ROLLBACK DRY RUN: would delete {inserted} rows with inserted_at={KEY}" in out
    assert "2026-10-14: would delete=4" in out and out.count("would delete=4") == 9
    assert sha(env["db"]) == before
    # restricted to a park-date range
    assert bf.main(["--output-base", str(env["base"]), "--rollback", "--load-key", KEY, "--dry-run",
                    "--start", "2026-10-14", "--end", "2026-10-15"]) == 0
    assert "would delete 8 rows" in capsys.readouterr().out


def test_ac8_rollback_deletes_only_key_rows(env, capsys):
    _renv(env)
    _insert_scraper(env["db"], "S1", "20:00", "2026-10-15", None)
    _insert_scraper(env["db"], "S2", "20:00", "2026-10-15", "2020-01-01 00:00:00")
    assert run(env, *R, "--load-key", KEY, "--row-tol", "0.6") == 0  # 2 scraper rows widen 10-15 by 50%
    capsys.readouterr()
    assert bf.main(["--output-base", str(env["base"]), "--rollback", "--load-key", KEY]) == 0
    out = capsys.readouterr().out
    assert f"ROLLBACK: deleted 36 rows with inserted_at={KEY}" in out and "ROLLBACK VERIFY PASS" in out
    assert counts(env["db"]) == {"2026-10-01": 1, "2026-10-12": len(parity_rows()), "2026-10-15": 2}
    con = duckdb.connect(str(env["db"]), read_only=True)
    assert con.execute("SELECT count(*) FROM live_waits WHERE entity_code IN ('S1','S2')").fetchone()[0] == 2
    con.close()


def test_ac8_rollback_bad_args_exit2(env, capsys):
    base = ["--output-base", str(env["base"]), "--rollback"]
    assert bf.main(base) == 2  # no --load-key
    assert bf.main([*base, "--load-key", "nope"]) == 2
    _drop_inserted_at(env["db"])
    assert bf.main([*base, "--load-key", KEY]) == 2  # no inserted_at column
    assert "REFUSED" in capsys.readouterr().out


def test_ac9_per_day_log_fields(env, capsys):
    assert run(env, "--dry-run") == 0
    out = capsys.readouterr().out
    assert ("2026-10-01: files=2 raw=8 prepped=6 distinct_keys=6 already_present=1 would insert=5 "
            "derived_park_date_mismatch=0 candidates=8 dropped_null_key=1 skipped_existing=1") in out
    assert "dropped_null_key=0 skipped_existing=0" in out.split("2026-10-03: ")[1]
    assert run(env, "--load-key", KEY) == 0
    out = capsys.readouterr().out
    assert ("already_present=1 inserted=5 derived_park_date_mismatch=0 candidates=8 dropped_null_key=1 "
            "skipped_existing=1") in out
    assert "candidates=17 dropped_null_key=1 skipped_existing=1" in out.split("TOTAL:")[1]


def test_ac12_rollout_doc_matches_code_flags_exit_codes_and_log_strings():
    """AC12: the rollout doc names every flag, exit code and operator-facing log string the code uses."""
    doc = (_SCRIPT.parent.parent / "docs" / "rollout" / "TPCR-BACKFILL-OCT1-9.md").read_text()
    src = _SCRIPT.read_text()
    for flag in ("--start", "--end", "--parity-date", "--dry-run", "--allow-missing", "--load-key", "--rollback"):
        assert flag in src and flag in doc, flag
    for code in (bf.EXIT_OK, bf.EXIT_PATH, bf.EXIT_PARITY, bf.EXIT_MISSING, bf.EXIT_VERIFY, bf.EXIT_CHECKSUM, bf.EXIT_HOLD):
        assert f"| {code} |" in doc, code
    for s in ("MISSING DAYS:", "CHECKSUM CHANGED:", "CLEAN CHECKSUMS UNCHANGED", "LOAD KEY inserted_at=",
              "HOLD: live_waits has no inserted_at column (OQ1); no live load", "ROLLBACK DRY RUN: would delete",
              "ROLLBACK VERIFY PASS", "DRY RUN (parity failed; counts only)", "VERIFY FAIL",
              "--parity-date 2026-10-12"):
        assert s in doc, s
        assert s.split(" 2026")[0] in src or s.startswith("--parity-date"), s
