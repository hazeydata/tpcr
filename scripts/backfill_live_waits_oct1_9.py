#!/usr/bin/env python3
"""
One-off backfill of tpcr_live.duckdb live_waits from fact_tables/clean (TPCR-BACKFILL-OCT1-9).
Default target: park dates 2026-10-01..09. Use --start/--end for another inclusive park-date range
(e.g. 2026-10-14..22, the S11 lock loss).

Source: output_base/fact_tables/clean/{YYYY-MM}/{park}_{YYYY-MM-DD}.csv (read only; no CSV is ever written).

Before loading anything, a source-parity check compares the clean CSVs for --parity-date with the
live_waits rows already in the DB for that date (row count, entity set, wait_time_type mix, and
TouringPlans POSTED rows mixed into the clean files). If parity fails nothing is loaded.

Idempotent: rows whose (entity_code, observed_at, wait_time_type) already exist are skipped.

Usage:
    python scripts/backfill_live_waits_oct1_9.py --dry-run
    python scripts/backfill_live_waits_oct1_9.py
    python scripts/backfill_live_waits_oct1_9.py --start 2026-10-14 --end 2026-10-22 --parity-date 2026-10-12 --dry-run
    python scripts/backfill_live_waits_oct1_9.py --rollback --load-key '<logged value>' --dry-run

--start/--end (both required together, start <= end, may span months) are mutually exclusive with --glob,
and the range must not include --parity-date (parity is checked on a day that is not being loaded).

Range runs list days with no clean CSV ("MISSING DAYS: d1,d2" or "MISSING DAYS: none") and skip them; with
missing days the run exits 4 before any DB access unless --allow-missing is given.

Checksums: every target clean file and the parity-date files are sha256-hashed at the start, re-hashed after
parity (before any write) and at the end; any change prints "CHECKSUM CHANGED: <file>" and exits 6.

Load key: every live load tags all inserted rows with ONE inserted_at value (naive UTC TIMESTAMP with
microseconds, or --load-key 'YYYY-MM-DD HH:MM:SS.ffffff'), logged as "LOAD KEY inserted_at=<value>" at start
and end. If live_waits has no inserted_at column the run is HOLD (exit 7, nothing written; a dry run still
prints the per-day counts first). After a live load each day is checked ("VERIFY PASS/FAIL <day>"): the
live_waits count for that park_date vs the distinct keys of all loaded clean rows with that derived
park_date, within --row-tol.

Parity failure (exit 3) still prints the per-day counts, read-only ("DRY RUN (parity failed; counts only)").
Parity failure takes precedence over HOLD.

Rollback: --rollback --load-key <value> [--dry-run] [--start/--end] deletes exactly the rows with
inserted_at = <value> (rows with NULL inserted_at never match), optionally restricted to a park-date range.
No parity check, no file reads. Exit 2 if live_waits has no inserted_at.

Exit codes: 0 ok, 2 bad input/path refused (also: load key already present, rollback without --load-key),
3 source parity failed (nothing loaded), 4 missing days without --allow-missing, 5 post-load VERIFY failed
(data stays loaded; roll back by key) or rollback count check failed, 6 clean file checksum changed,
7 HOLD: live_waits has no inserted_at (no live load).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pandas as pd

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (str(_REPO_ROOT / "src"), str(_REPO_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from init_live_duckdb import setup_logging
from live_waits_common import (
    LIVE_WAITS_KEY,
    insert_missing_live_waits,
    park_tz_for_park_code,
    prepare_live_waits_rows,
)
from utils import get_output_base

DEFAULT_GLOB = "fact_tables/clean/2026-10/*_2026-10-0[1-9].csv"
DEFAULT_PARITY_DATE = "2026-10-12"
CSV_COLUMNS = list(LIVE_WAITS_KEY) + ["wait_time_minutes"]
_NAME_RE = re.compile(r"^([A-Za-z0-9]+)_(\d{4}-\d{2}-\d{2})\.csv$")

EXIT_OK, EXIT_PATH, EXIT_PARITY = 0, 2, 3
EXIT_MISSING, EXIT_VERIFY, EXIT_CHECKSUM, EXIT_HOLD = 4, 5, 6, 7

KEY_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_KEY_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}")


class RefusedPath(Exception):
    pass


# --------------------------------------------------------------------------- paths


def _check_under_clean(path: Path, clean_root: Path) -> Path:
    """Return resolved path; raise RefusedPath if it (or any dir above it, up to clean_root) escapes clean."""
    resolved = path.resolve()
    # clean_root itself may be (under) a symlink, e.g. to an external disk; only symlinks below it are refused
    if not resolved.is_relative_to(clean_root.resolve()):
        raise RefusedPath(f"{path} resolves to {resolved}, outside {clean_root.resolve()}")
    # No symlinked directory between clean_root and the file (e.g. clean/2026-10 -> elsewhere)
    if not path.is_relative_to(clean_root):
        raise RefusedPath(f"{path} is not under {clean_root}")
    cur = clean_root
    for part in path.relative_to(clean_root).parts:
        cur = cur / part
        if cur.is_symlink():
            raise RefusedPath(f"{cur} is a symlink; refusing")
    return resolved


def resolve_targets(output_base: Path, pattern: str, clean_root: Path) -> list[Path]:
    """Glob relative to output_base and validate every match lives inside fact_tables/clean."""
    if Path(pattern).is_absolute():
        raise RefusedPath(f"--glob must be relative to the output base, got {pattern}")
    parts = Path(pattern).parts
    dir_parts = []
    for part in parts:
        if any(c in part for c in "*?["):
            break
        dir_parts.append(part)
    if len(dir_parts) == len(parts):  # no wildcard: last part is the file itself
        dir_parts = dir_parts[:-1]
    # The glob's literal directory must be a real directory inside fact_tables/clean (not a symlink elsewhere)
    glob_dir = output_base.joinpath(*dir_parts)
    if glob_dir.exists():
        _check_under_clean(glob_dir, clean_root)
    matches = sorted(Path(m) for m in glob.glob(str(output_base / pattern)))
    return [_check_under_clean(m, clean_root) for m in matches]


def _parse_date(value: str, flag: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise RefusedPath(f"bad {flag} {value!r} (want YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RefusedPath(f"bad {flag} {value!r} (not a calendar date)")


def resolve_range_targets(output_base: Path, start: str, end: str, parity_date: str, clean_root: Path) -> list[Path]:
    """Files for every park date in [start, end]; each match validated by resolve_targets/_check_under_clean."""
    d0, d1 = _parse_date(start, "--start"), _parse_date(end, "--end")
    if d0 > d1:
        raise RefusedPath(f"--start {start} is after --end {end}")
    if d0 <= _parse_date(parity_date, "--parity-date") <= d1:
        raise RefusedPath(f"range {start}..{end} includes --parity-date {parity_date}; "
                          "parity must be checked on a day that is not being loaded")
    files: list[Path] = []
    d = d0
    while d <= d1:
        iso = d.isoformat()
        files += resolve_targets(output_base, f"fact_tables/clean/{iso[:7]}/*_{iso}.csv", clean_root)
        d += timedelta(days=1)
    return files


def parse_name(path: Path) -> tuple[str, str]:
    m = _NAME_RE.match(path.name)
    if not m:
        raise RefusedPath(f"unexpected file name (want {{park}}_{{YYYY-MM-DD}}.csv): {path.name}")
    return m.group(1).lower(), m.group(2)


def missing_days(start: str, end: str, files: list[Path]) -> list[str]:
    """Park dates in [start, end] with no clean CSV among files."""
    have = {parse_name(f)[1] for f in files}
    d, d1, out = _parse_date(start, "--start"), _parse_date(end, "--end"), []
    while d <= d1:
        if d.isoformat() not in have:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def parity_files(clean_root: Path, parity_date: str) -> list[Path]:
    month_dir = clean_root / parity_date[:7]
    files = sorted(month_dir.glob(f"*_{parity_date}.csv")) if month_dir.is_dir() else []
    return [_check_under_clean(f, clean_root) for f in files]


# --------------------------------------------------------------------------- checksums


def hash_files(files) -> dict[Path, str]:
    out = {}
    for f in files:
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        out[f] = h.hexdigest()
    return out


def changed_files(before: dict[Path, str]) -> list[Path]:
    """Files whose sha256 (or existence) differs from the earlier hash."""
    out = []
    for f, digest in before.items():
        try:
            now = hash_files([f])[f]
        except OSError:
            now = None
        if now != digest:
            out.append(f)
    return out


# --------------------------------------------------------------------------- load key


def parse_load_key(value: str) -> str:
    if not _KEY_RE.fullmatch(value):
        raise RefusedPath(f"bad --load-key {value!r} (want 'YYYY-MM-DD HH:MM:SS.ffffff')")
    try:
        datetime.strptime(value, KEY_FORMAT)
    except ValueError:
        raise RefusedPath(f"bad --load-key {value!r} (not a valid timestamp)")
    return value


def new_load_key() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime(KEY_FORMAT)


def has_inserted_at(con) -> bool:
    return con.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'live_waits' AND column_name = 'inserted_at'").fetchall()[0][0] > 0


# --------------------------------------------------------------------------- reading


def read_prepped(path: Path) -> tuple[int, pd.DataFrame]:
    """Return (raw row count, prepped live_waits rows) for one clean CSV."""
    park, _ = parse_name(path)
    raw = pd.read_csv(path, usecols=lambda c: c in CSV_COLUMNS, dtype=str)
    for col in CSV_COLUMNS:
        if col not in raw.columns:
            raw[col] = None
    return len(raw), prepare_live_waits_rows(raw[CSV_COLUMNS], park_tz_for_park_code(park))


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=list(LIVE_WAITS_KEY) + ["wait_time_minutes", "park_date"])
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- parity


def run_parity(db: Path, clean_root: Path, parity_date: str, args) -> bool:
    month_dir = clean_root / parity_date[:7]
    files = parity_files(clean_root, parity_date)
    print("=" * 64)
    print(f"SOURCE PARITY CHECK: clean CSVs vs live_waits for {parity_date}")
    print(f"  clean files: {len(files)} in {month_dir}")
    if not files:
        print("PARITY FAIL: no clean files for parity date")
        return False

    clean = _concat([read_prepped(f)[1] for f in files])
    con = duckdb.connect(str(db), read_only=True)
    try:
        live = con.execute(
            "SELECT entity_code, observed_at, wait_time_type, wait_time_minutes FROM live_waits WHERE park_date = ?",
            [parity_date],
        ).fetchdf()
    finally:
        con.close()

    m = duckdb.connect()
    try:
        m.execute("SET TimeZone='UTC'")
        m.register("clean_raw", clean[list(LIVE_WAITS_KEY)])
        m.execute("""
            CREATE TEMP TABLE c AS
            SELECT DISTINCT entity_code, observed_at::TIMESTAMPTZ AS observed_at, wait_time_type FROM clean_raw
        """)
        m.register("live_raw", live[list(LIVE_WAITS_KEY)])
        m.execute("""
            CREATE TEMP TABLE l AS
            SELECT DISTINCT entity_code, observed_at::TIMESTAMPTZ AS observed_at, wait_time_type FROM live_raw
        """)
        clean_n = m.execute("SELECT count(*) FROM c").fetchall()[0][0]
        live_n = m.execute("SELECT count(*) FROM l").fetchall()[0][0]
        only_clean = [r[0] for r in m.execute(
            "SELECT DISTINCT entity_code FROM c EXCEPT SELECT DISTINCT entity_code FROM l ORDER BY 1").fetchall()]
        only_live = [r[0] for r in m.execute(
            "SELECT DISTINCT entity_code FROM l EXCEPT SELECT DISTINCT entity_code FROM c ORDER BY 1").fetchall()]
        live_entities = m.execute("SELECT count(DISTINCT entity_code) FROM l").fetchall()[0][0]
        c_types = dict(m.execute("SELECT wait_time_type, count(*) FROM c GROUP BY 1").fetchall())
        l_types = dict(m.execute("SELECT wait_time_type, count(*) FROM l GROUP BY 1").fetchall())
        c_posted = c_types.get("POSTED", 0)
        unmatched = m.execute("""
            SELECT count(*) FROM c WHERE wait_time_type = 'POSTED' AND NOT EXISTS (
                SELECT 1 FROM l WHERE l.entity_code = c.entity_code AND l.observed_at = c.observed_at
                  AND l.wait_time_type = c.wait_time_type)
        """).fetchall()[0][0]
        examples = m.execute("""
            SELECT entity_code, observed_at FROM c WHERE wait_time_type = 'POSTED' AND NOT EXISTS (
                SELECT 1 FROM l WHERE l.entity_code = c.entity_code AND l.observed_at = c.observed_at
                  AND l.wait_time_type = c.wait_time_type)
            ORDER BY observed_at, entity_code LIMIT 5
        """).fetchall()
        live_not_in_clean = m.execute("""
            SELECT count(*) FROM l WHERE NOT EXISTS (
                SELECT 1 FROM c WHERE l.entity_code = c.entity_code AND l.observed_at = c.observed_at
                  AND l.wait_time_type = c.wait_time_type)
        """).fetchall()[0][0]
    finally:
        m.close()

    ok = True

    row_diff = abs(clean_n - live_n) / max(live_n, 1)
    a_ok = row_diff <= args.row_tol
    ok &= a_ok
    print(f"  a. rows: clean={clean_n:,} live={live_n:,} diff={row_diff:.4%} (tol {args.row_tol:.2%}) "
          f"-> {'ok' if a_ok else 'FAIL'}")

    f_clean = len(only_clean) / max(live_entities, 1)
    f_live = len(only_live) / max(live_entities, 1)
    b_ok = f_clean <= args.entity_tol and f_live <= args.entity_tol
    ok &= b_ok
    print(f"  b. entities (live={live_entities}): only in clean={len(only_clean)} ({f_clean:.2%}) "
          f"only in live={len(only_live)} ({f_live:.2%}) (tol {args.entity_tol:.2%}) -> {'ok' if b_ok else 'FAIL'}")
    if only_clean:
        print(f"       only in clean: {only_clean[:10]}")
    if only_live:
        print(f"       only in live:  {only_live[:10]}")

    c_ok = True
    print("  c. wait_time_type mix:")
    for t in sorted(set(c_types) | set(l_types)):
        cn, ln = c_types.get(t, 0), l_types.get(t, 0)
        if cn and not ln:
            t_ok = False
        elif not cn:
            t_ok = True  # live-only type; covered by the row count check
        else:
            t_ok = abs(cn - ln) / max(ln, 1) <= args.row_tol
        c_ok &= t_ok
        print(f"       {t}: clean={cn:,} live={ln:,} -> {'ok' if t_ok else 'FAIL'}")
    ok &= c_ok

    tp_frac = unmatched / max(c_posted, 1)
    d_ok = tp_frac <= args.tp_posted_tol
    ok &= d_ok
    print(f"  d. clean POSTED not in live: {unmatched:,}/{c_posted:,} ({tp_frac:.2%}, tol {args.tp_posted_tol:.2%}) "
          f"-> {'ok' if d_ok else 'FAIL (TouringPlans rows mixed in?)'}; live keys not in clean: {live_not_in_clean:,}")
    for ec, ts in examples:
        print(f"       unmatched clean POSTED: {ec} {ts}")

    print(f"PARITY {'PASS' if ok else 'FAIL'}")
    print("=" * 64)
    return ok


# --------------------------------------------------------------------------- load


def count_new(con, df: pd.DataFrame) -> tuple[int, int]:
    """(distinct keys in df, of which already present in live_waits)."""
    if df.empty:
        return 0, 0
    con.register("_bf_new", df[list(LIVE_WAITS_KEY)])
    try:
        return tuple(con.execute("""
            SELECT count(*), count(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM live_waits w WHERE w.entity_code = n.entity_code
                  AND w.observed_at = n.observed_at AND w.wait_time_type = n.wait_time_type))
            FROM (SELECT DISTINCT entity_code, observed_at::TIMESTAMPTZ AS observed_at, wait_time_type
                  FROM _bf_new) n
        """).fetchall()[0])
    finally:
        con.unregister("_bf_new")


def distinct_keys_by_park_date(con, df: pd.DataFrame) -> dict[str, int]:
    """Distinct (entity_code, observed_at, wait_time_type) per derived park_date."""
    if df.empty:
        return {}
    con.register("_bf_all", df[list(LIVE_WAITS_KEY) + ["park_date"]])
    try:
        return {str(d): n for d, n in con.execute("""
            SELECT CAST(park_date AS VARCHAR), count(*) FROM (
                SELECT DISTINCT entity_code, observed_at::TIMESTAMPTZ AS observed_at, wait_time_type, park_date
                FROM _bf_all) GROUP BY 1
        """).fetchall()}
    finally:
        con.unregister("_bf_all")


def load_days(files: list[Path], con, header: str, inserted_at: str | None = None, row_tol: float = 0.02) -> bool:
    """Per-day counts; with inserted_at set, also insert (tagged) and VERIFY. True unless a VERIFY failed."""
    dry_run = inserted_at is None
    by_day: dict[str, list[Path]] = {}
    for f in files:
        by_day.setdefault(parse_name(f)[1], []).append(f)

    before = con.execute("SELECT count(*) FROM live_waits").fetchall()[0][0]
    tot = dict(files=0, raw=0, prepped=0, present=0, would=0, inserted=0, mismatch=0, dropped=0)
    verb = "would insert" if dry_run else "inserted"
    print(f"{header}: live_waits rows before: {before:,}")
    loaded: list[pd.DataFrame] = []
    for day in sorted(by_day):
        raw_n, frames, mismatch, dropped = 0, [], 0, 0
        for f in by_day[day]:
            n, prepped = read_prepped(f)
            raw_n += n
            dropped += int(prepped.attrs.get("dropped_null_key", 0))
            mismatch += int((prepped["park_date"] != day).sum())
            frames.append(prepped)
        df = _concat(frames)
        distinct, present = count_new(con, df)
        would = distinct - present
        inserted = 0
        if not dry_run:
            con.execute("BEGIN TRANSACTION")
            try:
                inserted = insert_missing_live_waits(con, df, inserted_at=inserted_at)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            if inserted != would:
                print(f"  WARNING {day}: inserted {inserted} != expected {would}")
            loaded.append(df[list(LIVE_WAITS_KEY) + ["park_date"]])
        print(f"  {day}: files={len(by_day[day])} raw={raw_n:,} prepped={len(df):,} distinct_keys={distinct:,} "
              f"already_present={present:,} {verb}={would if dry_run else inserted:,} "
              f"derived_park_date_mismatch={mismatch:,} candidates={raw_n:,} dropped_null_key={dropped:,} "
              f"skipped_existing={present:,}")
        tot["files"] += len(by_day[day]); tot["raw"] += raw_n; tot["prepped"] += len(df)
        tot["present"] += present; tot["would"] += would; tot["inserted"] += inserted; tot["mismatch"] += mismatch
        tot["dropped"] += dropped
    after = con.execute("SELECT count(*) FROM live_waits").fetchall()[0][0]
    print(f"TOTAL: files={tot['files']} raw={tot['raw']:,} prepped={tot['prepped']:,} "
          f"already_present={tot['present']:,} {verb}={(tot['would'] if dry_run else tot['inserted']):,} "
          f"derived_park_date_mismatch={tot['mismatch']:,} candidates={tot['raw']:,} "
          f"dropped_null_key={tot['dropped']:,} skipped_existing={tot['present']:,}")
    print(f"live_waits rows after: {after:,}" + (" (dry run; unchanged)" if dry_run else ""))
    if dry_run:
        return True

    clean_by_day = distinct_keys_by_park_date(con, _concat(loaded))
    failed = 0
    for day in sorted(by_day):
        clean_n = clean_by_day.get(day, 0)
        live_n = con.execute("SELECT count(*) FROM live_waits WHERE park_date = CAST(? AS DATE)",
                             [day]).fetchall()[0][0]
        diff = abs(live_n - clean_n) / max(clean_n, 1)
        ok = diff <= row_tol
        failed += not ok
        print(f"  VERIFY {'PASS' if ok else 'FAIL'} {day}: live_waits={live_n:,} clean_distinct_keys={clean_n:,} "
              f"diff={diff:.4%} (tol {row_tol:.2%})")
    return failed == 0


# --------------------------------------------------------------------------- rollback


def run_rollback(args, db: Path) -> int:
    try:
        if args.load_key is None:
            raise RefusedPath("--rollback requires --load-key")
        key = parse_load_key(args.load_key)
        if (args.start is None) != (args.end is None):
            raise RefusedPath("--start and --end must be given together")
        where, params = "inserted_at = CAST(? AS TIMESTAMP)", [key]
        if args.start is not None:
            d0, d1 = _parse_date(args.start, "--start"), _parse_date(args.end, "--end")
            if d0 > d1:
                raise RefusedPath(f"--start {args.start} is after --end {args.end}")
            where += " AND park_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)"
            params += [args.start, args.end]
        if not db.is_file():
            raise RefusedPath(f"database not found: {db}")
    except (RefusedPath, ValueError) as e:
        print(f"REFUSED: {e}")
        return EXIT_PATH

    con = duckdb.connect(str(db), read_only=args.dry_run)
    try:
        if not has_inserted_at(con):
            print("REFUSED: live_waits has no inserted_at column; nothing to roll back by key")
            return EXIT_PATH
        per_day = con.execute(
            f"SELECT CAST(park_date AS VARCHAR), count(*) FROM live_waits WHERE {where} GROUP BY 1 ORDER BY 1",
            params).fetchall()
        n = sum(c for _, c in per_day)
        if args.dry_run:
            print(f"ROLLBACK DRY RUN: would delete {n} rows with inserted_at={key}")
            for d, c in per_day:
                print(f"  {d}: would delete={c:,}")
            return EXIT_OK
        print(f"ROLLBACK: deleting {n} rows with inserted_at={key}")
        con.execute("BEGIN TRANSACTION")
        try:
            deleted = con.execute(f"DELETE FROM live_waits WHERE {where}", params).fetchall()[0][0]
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        remaining = con.execute(f"SELECT count(*) FROM live_waits WHERE {where}", params).fetchall()[0][0]
        print(f"ROLLBACK: deleted {deleted} rows with inserted_at={key}")
        if remaining != 0 or deleted != n:
            print(f"ROLLBACK VERIFY FAIL: expected to delete {n}, deleted {deleted}, {remaining} still match")
            return EXIT_VERIFY
        print(f"ROLLBACK VERIFY PASS: 0 rows remain with inserted_at={key}")
        return EXIT_OK
    finally:
        con.close()


# --------------------------------------------------------------------------- run phase


def run_load_phase(args, db: Path, files: list[Path], parity_ok: bool, key_arg: str | None) -> int:
    ro = duckdb.connect(str(db), read_only=True)
    try:
        has_key = has_inserted_at(ro)
        if args.dry_run or not parity_ok or not has_key:
            header = ("DRY RUN (parity failed; counts only)" if not parity_ok
                      else "DRY RUN" if args.dry_run else "DRY RUN (inserted_at missing; counts only)")
            load_days(files, ro, header)
            if not parity_ok:
                print("Source parity failed; refusing to load. Nothing was written.")
                return EXIT_PARITY
            if not has_key:
                print("HOLD: live_waits has no inserted_at column (OQ1); no live load")
                return EXIT_HOLD
            return EXIT_OK
    finally:
        ro.close()

    key = key_arg or new_load_key()
    con = duckdb.connect(str(db))
    try:
        clash = con.execute("SELECT count(*) FROM live_waits WHERE inserted_at = CAST(? AS TIMESTAMP)",
                            [key]).fetchall()[0][0]
        if clash:
            print(f"REFUSED: {clash} live_waits rows already have inserted_at={key}; use a fresh load key")
            return EXIT_PATH
        print(f"LOAD KEY inserted_at={key}")
        ok = load_days(files, con, "LOAD", inserted_at=key, row_tol=args.row_tol)
        print(f"LOAD KEY inserted_at={key}")
    finally:
        con.close()
    if not ok:
        print("VERIFY FAILED: data stays loaded. To undo this load: "
              f"--rollback --load-key '{key}' --dry-run, then without --dry-run")
        return EXIT_VERIFY
    return EXIT_OK


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backfill live_waits (default 2026-10-01..09) from fact_tables/clean CSVs")
    p.add_argument("--output-base", type=Path, default=None)
    p.add_argument("--db", type=Path, default=None, help="default: <output-base>/tpcr_live.duckdb")
    p.add_argument("--glob", default=None, help=f"relative to output base (default: {DEFAULT_GLOB})")
    p.add_argument("--start", default=None, help="YYYY-MM-DD, inclusive park date; requires --end; excludes --glob")
    p.add_argument("--end", default=None, help="YYYY-MM-DD, inclusive park date; requires --start; excludes --glob")
    p.add_argument("--parity-date", default=DEFAULT_PARITY_DATE)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-missing", action="store_true",
                   help="continue with the days that have a clean CSV (range runs; otherwise missing days exit 4)")
    p.add_argument("--load-key", default=None,
                   help="inserted_at value 'YYYY-MM-DD HH:MM:SS.ffffff' (default: now, UTC); required by --rollback")
    p.add_argument("--rollback", action="store_true",
                   help="delete the rows with inserted_at = --load-key (optionally within --start/--end)")
    p.add_argument("--row-tol", type=float, default=0.02)
    p.add_argument("--entity-tol", type=float, default=0.02)
    p.add_argument("--tp-posted-tol", type=float, default=0.01)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    output_base = (args.output_base or get_output_base()).resolve()
    db = (args.db or output_base / "tpcr_live.duckdb")
    clean_root = output_base / "fact_tables" / "clean"

    if args.rollback:
        return run_rollback(args, db)

    missing: list[str] = []
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.parity_date):
            raise RefusedPath(f"bad --parity-date {args.parity_date!r}")
        if (args.start is None) != (args.end is None):
            raise RefusedPath("--start and --end must be given together")
        if args.start is not None:
            if args.glob is not None:
                raise RefusedPath("--start/--end are mutually exclusive with --glob")
            desc = f"{args.start}..{args.end}"
            files = resolve_range_targets(output_base, args.start, args.end, args.parity_date, clean_root)
            missing = missing_days(args.start, args.end, files)
            print(f"MISSING DAYS: {','.join(missing) if missing else 'none'}")
        else:
            desc = args.glob or DEFAULT_GLOB
            files = resolve_targets(output_base, desc, clean_root)
        if not files:
            raise RefusedPath(f"no files match {desc} under {output_base}")
        for f in files:
            parse_name(f)
            park_tz_for_park_code(parse_name(f)[0])
        if missing and not args.allow_missing:
            print("Missing days and --allow-missing not set; refusing. No DB access.")
            return EXIT_MISSING
        if not db.is_file():
            raise RefusedPath(f"database not found: {db}")
        key_arg = parse_load_key(args.load_key) if args.load_key is not None else None
        hashes = hash_files(sorted(set(files) | set(parity_files(clean_root, args.parity_date))))
    except (RefusedPath, ValueError) as e:
        print(f"REFUSED: {e}")
        return EXIT_PATH

    print(f"output base: {output_base}\ndb: {db}\ntarget files: {len(files)}")

    def checksum_changed() -> bool:
        changed = changed_files(hashes)
        for f in changed:
            print(f"CHECKSUM CHANGED: {f}")
        return bool(changed)

    try:
        parity_ok = run_parity(db, clean_root, args.parity_date, args)
    except RefusedPath as e:
        print(f"REFUSED: {e}")
        return EXIT_PATH
    if checksum_changed():
        print("Clean files changed during the run; aborting before any write.")
        return EXIT_CHECKSUM

    rc = run_load_phase(args, db, files, parity_ok, key_arg)
    if checksum_changed():
        return EXIT_CHECKSUM
    print(f"CLEAN CHECKSUMS UNCHANGED ({len(hashes)} files)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
