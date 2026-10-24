#!/usr/bin/env python3
"""
One-off backfill of the Oct 1-9 2026 POSTED waits into tpcr_live.duckdb live_waits (TPCR-BACKFILL-OCT1-9).

Source: output_base/fact_tables/clean/2026-10/{park}_{YYYY-MM-DD}.csv for the fixed days 2026-10-01..09
(read only; no CSV is ever written). Only wait_time_type == 'POSTED' rows are candidates; PRIORITY, ACTUAL and
anything else are counted per day (excluded_PRIORITY / excluded_ACTUAL / excluded_other) and never inserted.
Each day needs at least one clean CSV; otherwise "MISSING DAYS: d1,d2" and exit 4 before any DB access.

Per day, Expected = clean POSTED minus five independent drop classes, each counted with its own predicate on the
rows still remaining after the earlier classes (in this order, mutually exclusive): null_key (entity_code /
observed_at / wait_time_type null or blank); bad_observed_at (key present, observed_at unparseable);
wait_nonpositive (wait parses as a number and is <= 0); duplicate_keys (exact key); park_date_mismatch (6 AM-rule
park_date is not that file's day). Nothing is a remainder: a row whose wait is non-numeric (e.g. 'abc') fits no class
(prep drops it, it is not counted), so reconcile fails. Only Expected rows are inserted, so nothing outside Oct 1-9
is written. The dry run prints each drop class per day and a reconcile line; clean_posted == expected + sum(drops)
must hold EXACTLY for every day, else the run exits 7 before any DB access or write.

Insert is the scraper's exact-key anti-join (insert_missing_live_waits), so a rerun inserts 0.

Dry run is the default (DB opened read-only, nothing written). --apply writes. Usage:
    python scripts/backfill_live_waits_oct1_9.py --output-base "$OB"
    python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --apply
    python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --rollback --load-key '<logged value>'
    python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --rollback --load-key '<logged value>' --apply

Load key: --apply tags all inserted rows with ONE inserted_at (naive UTC TIMESTAMP with microseconds, or
--load-key 'YYYY-MM-DD HH:MM:SS.ffffff'), logged as "LOAD KEY inserted_at=<value>" at start and end. A key already
present is refused. If live_waits has no inserted_at column the run is REFUSED (exit 2).

VERIFY (after --apply): for every day live POSTED count(park_date=day) must equal expected exactly
("VERIFY PASS|FAIL <day>: live_posted=.. expected=.."). Any FAIL: exit 5 and the rollback command is printed.

Rollback: --rollback --load-key <value> deletes exactly the rows with inserted_at = <value> (NULL never matches).
Without --apply it is a dry run (read-only, "ROLLBACK DRY RUN: would delete <n> rows ..." plus per-day counts).

Checksums: sha256 of every target clean file at start, before the first write and at the end; a change prints
"CHECKSUM CHANGED: <file>" and exits 6. Success prints "CLEAN CHECKSUMS UNCHANGED (<n> files)".

Protected windows (America/New_York, half-open [start, end)): ETL 05:30-09:00, replay 04:25-04:45. The run is
refused (exit 8) at start in every mode, and again before each day's insert during --apply (earlier days stay
key-tagged and rollback-able). S11 (~06:15, ~110 s) falls inside the ETL window, so no marker is used; if the DB
is locked (S11 or the scraper holds the write lock) the run exits 9 at once: no retry, no sleep.

Exit codes: 0 ok, 2 bad input / path refused / no inserted_at / key clash / rollback without key, 4 missing days,
5 VERIFY failed or rollback count check failed, 6 clean checksum changed, 7 reconcile mismatch,
8 inside protected window, 9 DB locked.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (str(_REPO_ROOT / "src"), str(_REPO_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from init_live_duckdb import setup_logging
from live_waits_common import (
    LIVE_WAITS_COLUMNS,
    LIVE_WAITS_KEY,
    insert_missing_live_waits,
    park_tz_for_park_code,
    prepare_live_waits_rows,
)
from utils import get_output_base

SCRIPT = "scripts/backfill_live_waits_oct1_9.py"
DAYS = [f"2026-10-{d:02d}" for d in range(1, 10)]
CSV_COLUMNS = list(LIVE_WAITS_KEY) + ["wait_time_minutes"]
_NAME_RE = re.compile(r"^([A-Za-z0-9]+)_(\d{4}-\d{2}-\d{2})\.csv$")

EXIT_OK, EXIT_PATH, EXIT_MISSING, EXIT_VERIFY, EXIT_CHECKSUM, EXIT_RECONCILE, EXIT_WINDOW, EXIT_LOCKED = (
    0, 2, 4, 5, 6, 7, 8, 9)

ET = ZoneInfo("America/New_York")
# Protected windows, half-open [start, end), America/New_York.
# ETL (spec R1, OQ1): starts 06:00 per docs/PIPELINE_STATE.md:16, so 05:30 adds a 30-min margin; there is no end in
# the repo, so 09:00 covers the 90-min crowd-report wait. S11 (~06:15, ~110s; docs/PIPELINE_STATE.md:48) is inside
# this window, so no marker is needed; a lock exits 9 with no retry.
ETL_WINDOW_ET = (time(5, 30), time(9, 0))
# Replay (spec R1, OQ1): 04:30-04:40 per src/get_wait_times_from_queue_times.py:159-161, plus 5-min margins.
REPLAY_WINDOW_ET = (time(4, 25), time(4, 45))
WINDOWS = (("ETL", ETL_WINDOW_ET), ("replay", REPLAY_WINDOW_ET))

KEY_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_KEY_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}")


class RefusedPath(Exception):
    pass


# --------------------------------------------------------------------------- paths


def _check_under_clean(path: Path, clean_root: Path) -> Path:
    """Return resolved path; raise RefusedPath if it (or any dir above it, up to clean_root) escapes clean."""
    resolved = path.resolve()
    # run_load already refused a symlinked fact_tables or clean; here every component below clean_root is checked
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


def parse_name(path: Path) -> tuple[str, str]:
    m = _NAME_RE.match(path.name)
    if not m:
        raise RefusedPath(f"unexpected file name (want {{park}}_{{YYYY-MM-DD}}.csv): {path.name}")
    return m.group(1).lower(), m.group(2)


def day_files(clean_root: Path) -> dict[str, list[Path]]:
    """Validated clean CSVs per day of DAYS (empty list = missing day)."""
    month_dir = clean_root / DAYS[0][:7]
    if month_dir.exists():
        _check_under_clean(month_dir, clean_root)
    out = {}
    for day in DAYS:
        found = sorted(month_dir.glob(f"*_{day}.csv")) if month_dir.is_dir() else []
        out[day] = [_check_under_clean(f, clean_root) for f in found]
        for f in out[day]:
            park_tz_for_park_code(parse_name(f)[0])
    return out


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


# --------------------------------------------------------------------------- load key, windows, locks


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


def current_time(now=None) -> datetime:
    """The injected tz-aware clock value, else the real ET time. Tests monkeypatch this for the mid-run check."""
    return now if now is not None else datetime.now(ET)


def window_refusal(now: datetime) -> str | None:
    """'REFUSED: inside <name> window ...' if now (any tz-aware datetime) is in a protected window, else None."""
    t = now.astimezone(ET).time()
    for name, (start, end) in WINDOWS:
        if start <= t < end:
            return (f"REFUSED: inside {name} window {start:%H:%M}-{end:%H:%M} ET "
                    f"(now {t:%H:%M:%S} ET); not running")
    return None


def is_lock_error(e: Exception) -> bool:
    m = str(e)
    return ("Could not set lock" in m or "Conflicting lock" in m
            or (isinstance(e, duckdb.IOException) and "lock" in m.lower()))


def print_rollback(args, ob: Path, key: str) -> None:
    base = f'python {SCRIPT} --output-base "{ob}"' + (f' --db "{args.db}"' if args.db else "")
    print(f"To undo this load (dry run first, then delete):\n  {base} --rollback --load-key '{key}'\n"
          f"  {base} --rollback --load-key '{key}' --apply")


# --------------------------------------------------------------------------- reading


def read_day(files: list[Path], day: str) -> tuple[dict, pd.DataFrame]:
    """(counts, eligible rows) for one day: POSTED only, scraper prep, distinct keys, park_date == day."""
    st = dict(clean_posted=0, excluded_PRIORITY=0, excluded_ACTUAL=0, excluded_other=0,
              null_key=0, bad_observed_at=0, wait_nonpositive=0, duplicate_keys=0, park_date_mismatch=0)
    frames = []
    for f in files:
        raw = pd.read_csv(f, usecols=lambda c: c in CSV_COLUMNS, dtype=str)
        for col in CSV_COLUMNS:
            if col not in raw.columns:
                raw[col] = None
        raw = raw[CSV_COLUMNS]
        typ = raw["wait_time_type"]
        posted = raw[typ == "POSTED"]
        st["clean_posted"] += len(posted)
        st["excluded_PRIORITY"] += int((typ == "PRIORITY").sum())
        st["excluded_ACTUAL"] += int((typ == "ACTUAL").sum())
        st["excluded_other"] += len(raw) - len(posted) - int(typ.isin(["PRIORITY", "ACTUAL"]).sum())
        if posted.empty:
            continue
        prepped = prepare_live_waits_rows(posted, park_tz_for_park_code(parse_name(f)[0]))
        # Each class has its own predicate on the rows left by the earlier classes; none is a remainder.
        null_key = posted[list(LIVE_WAITS_KEY)].apply(
            lambda c: c.isna() | (c.astype(str).str.strip() == "")).any(axis=1)
        st["null_key"] += int(null_key.sum())
        keyed = posted[~null_key]
        bad_t = pd.to_datetime(keyed["observed_at"], utc=True, errors="coerce").isna()
        st["bad_observed_at"] += int(bad_t.sum())
        wait = pd.to_numeric(keyed.loc[~bad_t, "wait_time_minutes"], errors="coerce")
        st["wait_nonpositive"] += int((wait <= 0).sum())
        frames.append(prepped)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(LIVE_WAITS_COLUMNS))
    if not df.empty:
        t = pd.to_datetime(df["observed_at"], utc=True, errors="coerce")
        dup = df.assign(_t=t).duplicated(["entity_code", "_t", "wait_time_type"])
        st["duplicate_keys"] = int(dup.sum())
        df = df[~dup]
        keep = df["park_date"] == day
        st["park_date_mismatch"] = int((~keep).sum())
        df = df[keep]
    return st, df


def count_present(con, df: pd.DataFrame) -> int:
    """Rows of df whose exact key is already in live_waits."""
    if df.empty:
        return 0
    con.register("_bf_new", df[list(LIVE_WAITS_KEY)])
    try:
        return con.execute("""
            SELECT count(*) FROM (SELECT entity_code, observed_at::TIMESTAMPTZ AS observed_at, wait_time_type
                                  FROM _bf_new) n
            WHERE EXISTS (SELECT 1 FROM live_waits w WHERE w.entity_code = n.entity_code
                          AND w.observed_at = n.observed_at AND w.wait_time_type = n.wait_time_type)
        """).fetchall()[0][0]
    finally:
        con.unregister("_bf_new")


# --------------------------------------------------------------------------- reconcile

DROP_CLASSES = ("null_key", "bad_observed_at", "wait_nonpositive", "duplicate_keys", "park_date_mismatch")


def reconcile(plans: dict) -> bool:
    """Print each day's reconcile line; True only if clean_posted == expected + sum(drops) exactly for every day."""
    ok = True
    for day in DAYS:
        st, df = plans[day]
        drops = sum(st[c] for c in DROP_CLASSES)
        day_ok = st["clean_posted"] == len(df) + drops
        ok &= day_ok
        print(f"  {day} reconcile: clean_posted={st['clean_posted']} {'==' if day_ok else '!='} "
              f"expected={len(df)} + drops={drops} ("
              + " + ".join(f"{c}={st[c]}" for c in DROP_CLASSES) + f") {'OK' if day_ok else 'MISMATCH'}")
    return ok


# --------------------------------------------------------------------------- load


def load_days(con, plans: dict, apply: bool, key: str | None, args, ob: Path, now) -> int:
    """Per-day counts; with apply also insert (key-tagged) and VERIFY. Returns an exit code."""
    verb = "inserted" if apply else "would_insert"
    tot = dict(expected=0, already_in_live=0, n=0)
    for day in DAYS:
        st, df = plans[day]
        if apply and (msg := window_refusal(current_time(now))):
            print(f"{msg}\nSTOPPED before {day}; earlier days stay loaded under the load key.")
            print_rollback(args, ob, key)
            return EXIT_WINDOW
        present = count_present(con, df)
        n = len(df) - present
        if apply:
            con.execute("BEGIN TRANSACTION")
            try:
                n = insert_missing_live_waits(con, df, inserted_at=key)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        print(f"  {day}: " + " ".join(f"{k}={v:,}" for k, v in st.items())
              + f" expected={len(df):,} already_in_live={present:,} {verb}={n:,}")
        tot["expected"] += len(df); tot["already_in_live"] += present; tot["n"] += n
    print(f"TOTAL: expected={tot['expected']:,} already_in_live={tot['already_in_live']:,} {verb}={tot['n']:,}")
    if not apply:
        return EXIT_OK

    failed = 0
    for day in DAYS:
        live_n = con.execute("SELECT count(*) FROM live_waits WHERE park_date = CAST(? AS DATE) "
                             "AND wait_time_type = 'POSTED'", [day]).fetchall()[0][0]
        ok = live_n == len(plans[day][1])
        failed += not ok
        print(f"  VERIFY {'PASS' if ok else 'FAIL'} {day}: live_posted={live_n:,} expected={len(plans[day][1]):,}")
    if failed:
        print("VERIFY FAILED: data stays loaded.")
        print_rollback(args, ob, key)
        return EXIT_VERIFY
    return EXIT_OK


# --------------------------------------------------------------------------- rollback


def run_rollback(args, ob: Path, db: Path) -> int:
    try:
        if args.load_key is None:
            raise RefusedPath("--rollback requires --load-key")
        key = parse_load_key(args.load_key)
        if not db.is_file():
            raise RefusedPath(f"database not found: {db}")
    except RefusedPath as e:
        print(f"REFUSED: {e}")
        return EXIT_PATH

    where = "inserted_at = CAST(? AS TIMESTAMP)"
    con = duckdb.connect(str(db), read_only=not args.apply)
    try:
        if not has_inserted_at(con):
            print("REFUSED: live_waits has no inserted_at column; nothing to roll back by key")
            return EXIT_PATH
        per_day = con.execute(
            f"SELECT CAST(park_date AS VARCHAR), count(*) FROM live_waits WHERE {where} GROUP BY 1 ORDER BY 1",
            [key]).fetchall()
        n = sum(c for _, c in per_day)
        if not args.apply:
            print(f"ROLLBACK DRY RUN: would delete {n} rows with inserted_at={key}")
            for d, c in per_day:
                print(f"  {d}: would delete={c:,}")
            print("Nothing deleted. Add --apply to delete.")
            return EXIT_OK
        print(f"ROLLBACK: deleting {n} rows with inserted_at={key}")
        con.execute("BEGIN TRANSACTION")
        try:
            deleted = con.execute(f"DELETE FROM live_waits WHERE {where}", [key]).fetchall()[0][0]
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        remaining = con.execute(f"SELECT count(*) FROM live_waits WHERE {where}", [key]).fetchall()[0][0]
        print(f"ROLLBACK: deleted {deleted} rows with inserted_at={key}")
        if remaining != 0 or deleted != n:
            print(f"ROLLBACK VERIFY FAIL: expected to delete {n}, deleted {deleted}, {remaining} still match")
            return EXIT_VERIFY
        print(f"ROLLBACK VERIFY PASS: 0 rows remain with inserted_at={key}")
        return EXIT_OK
    finally:
        con.close()


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backfill live_waits POSTED rows for 2026-10-01..09 from fact_tables/clean "
                                            "(dry run unless --apply)")
    p.add_argument("--output-base", type=Path, default=None)
    p.add_argument("--db", type=Path, default=None, help="default: <output-base>/tpcr_live.duckdb")
    p.add_argument("--apply", action="store_true", help="write (load, or delete with --rollback); default is a dry run")
    p.add_argument("--load-key", default=None,
                   help="inserted_at value 'YYYY-MM-DD HH:MM:SS.ffffff' (default: now, UTC); required by --rollback")
    p.add_argument("--rollback", action="store_true", help="delete the rows with inserted_at = --load-key")
    return p


def run_load(args, ob: Path, db: Path, now, state: dict) -> int:
    clean_root = ob / "fact_tables" / "clean"
    try:
        # The spec is silent on symlinks; refusing is the safer choice.
        for d in (ob / "fact_tables", clean_root):
            if d.is_symlink():
                raise RefusedPath(f"{d} is a symlink; refusing")
        by_day = day_files(clean_root)
        missing = [d for d in DAYS if not by_day[d]]
        print(f"MISSING DAYS: {','.join(missing) if missing else 'none'}")
        if missing:
            print("Missing days; refusing. No DB access.")
            return EXIT_MISSING
        if not db.is_file():
            raise RefusedPath(f"database not found: {db}")
        key = parse_load_key(args.load_key) if args.load_key is not None else new_load_key()
        files = [f for d in DAYS for f in by_day[d]]
        hashes = hash_files(files)
    except (RefusedPath, ValueError) as e:
        print(f"REFUSED: {e}")
        return EXIT_PATH

    def checksum_changed() -> bool:
        changed = changed_files(hashes)
        for f in changed:
            print(f"CHECKSUM CHANGED: {f}")
        return bool(changed)

    print(f"output base: {ob}\ndb: {db}\ntarget files: {len(files)}\nmode: {'LOAD' if args.apply else 'DRY RUN'}")
    plans = {d: read_day(by_day[d], d) for d in DAYS}
    if checksum_changed():
        print("Clean files changed during the run; aborting before any write.")
        return EXIT_CHECKSUM

    if not reconcile(plans):
        print("RECONCILE FAILED: clean_posted != expected + drops for at least one day; nothing written.")
        return EXIT_RECONCILE

    con = duckdb.connect(str(db), read_only=not args.apply)
    try:
        if not has_inserted_at(con):
            print("REFUSED: live_waits has no inserted_at column; nothing written")
            return EXIT_PATH
        if args.apply:
            clash = con.execute("SELECT count(*) FROM live_waits WHERE inserted_at = CAST(? AS TIMESTAMP)",
                                [key]).fetchall()[0][0]
            if clash:
                print(f"REFUSED: {clash} live_waits rows already have inserted_at={key}; use a fresh load key")
                return EXIT_PATH
            state["key"] = key
            print(f"LOAD KEY inserted_at={key}")
        rc = load_days(con, plans, args.apply, key, args, ob, now)
        if args.apply:
            print(f"LOAD KEY inserted_at={key}")
    finally:
        con.close()
    if rc not in (EXIT_OK, EXIT_VERIFY):
        return rc
    if checksum_changed():
        return EXIT_CHECKSUM
    print(f"CLEAN CHECKSUMS UNCHANGED ({len(hashes)} files)")
    return rc


def main(argv=None, now=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    ob = (args.output_base or get_output_base()).resolve()
    db = args.db or ob / "tpcr_live.duckdb"
    if msg := window_refusal(current_time(now)):
        print(msg)
        return EXIT_WINDOW
    state = {"key": None}
    try:
        return run_rollback(args, ob, db) if args.rollback else run_load(args, ob, db, now, state)
    except duckdb.Error as e:
        if not is_lock_error(e):
            raise
        print(f"LOCKED: {str(e).splitlines()[0]} -- S11 or another writer holds the DB lock; exiting, no retry")
        if state["key"]:
            print_rollback(args, ob, state["key"])
        return EXIT_LOCKED


if __name__ == "__main__":
    sys.exit(main())
