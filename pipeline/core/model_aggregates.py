"""Build aggregates/model_aggregates.parquet (TPCR-S4-AGG-001).

One implementation, used by the daily S4 step and by
scripts/build_model_aggregates.py. The method is the original builder's
(858ddd5d): posted waits > 0, fastpass booths excluded, grouped by
entity_code x date_group_id x 15-minute clock slot, with a 730-day
half-life weighted mean over all history. Two changes from that builder,
both from the spec:

* The input is de-duplicated on the natural key
  (entity_code, observed_at, wait_time_type, park_date) before aggregating, so a
  re-appended row (the Oct 10-11 duplicates) counts once.
* A cell with fewer than ``min_count`` observations is not written.

The file is written to a temp path, validated against the schema of the
existing file, and only then renamed over the old one. Any failure raises
AggregateBuildError and leaves the previous file untouched.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

MIN_COUNT = 5
HALF_LIFE_DAYS = 730.0
SLOTS_PER_DAY = 96
HOURS_PER_DAY = 24

# Column-for-column schema of the file built in Feb 2026 (spec section 1).
EXPECTED_SCHEMA: list[tuple[str, str]] = [
    ("entity_code", "VARCHAR"),
    ("date_group_id", "VARCHAR"),
    ("time_slot", "INTEGER"),
    ("hour_of_day", "INTEGER"),
    ("wait_median", "DOUBLE"),
    ("wait_mean", "DOUBLE"),
    ("wait_p25", "DOUBLE"),
    ("wait_p75", "DOUBLE"),
    ("wait_std", "DOUBLE"),
    ("wait_min", "BIGINT"),
    ("wait_max", "BIGINT"),
    ("wait_mean_weighted", "DOUBLE"),
    ("sample_count", "BIGINT"),
    ("date_count", "BIGINT"),
    ("min_park_date", "DATE"),
    ("max_park_date", "DATE"),
]


class AggregateBuildError(RuntimeError):
    """The build or its validation failed. The previous file is untouched."""


@dataclass
class BuildResult:
    rows: int
    entities: int
    input_rows: int
    duplicate_rows_dropped: int
    input_max_date: date
    output_max_park_date: date
    duration_sec: float
    output_path: Path
    sidecar_path: Path


def _q(p: Path | str) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def sidecar_path_for(output_path: Path) -> Path:
    return output_path.with_name(output_path.stem + ".meta.json")


def build_model_aggregates(
    output_base: Path,
    *,
    as_of: date | None = None,
    min_count: int = MIN_COUNT,
    require_full_day: bool = True,
    memory_limit: str = "16GB",
    threads: int = 4,
    buckets: int = 16,
    log=None,
) -> BuildResult:
    """Rebuild model_aggregates.parquet from fact_tables/parquet.

    ``as_of`` is the run date (default: today). Inputs are park dates
    before ``as_of`` (through yesterday) and the half-life weights are
    measured from ``as_of``. The build fails unless the output covers
    yesterday.

    Entities are processed in ``buckets`` hash partitions (each one a
    separate dedupe + aggregate pass) so peak memory is about 1/buckets of
    a single-pass build; the parts are then merged into one sorted file.
    """
    say = (log.info if log is not None else (lambda m: None))
    as_of = as_of or date.today()
    yesterday = as_of - timedelta(days=1)
    t0 = time.time()

    parquet_dir = output_base / "fact_tables" / "parquet"
    dim_dir = output_base / "dimension_tables"
    dg_file = dim_dir / "dimdategroupid.csv"
    ent_file = dim_dir / "dimentity.csv"
    agg_dir = output_base / "aggregates"
    out = agg_dir / "model_aggregates.parquet"
    tmp = agg_dir / ".model_aggregates.parquet.tmp"
    sidecar = sidecar_path_for(out)
    sidecar_tmp = agg_dir / ".model_aggregates.meta.json.tmp"

    for p in (dg_file, ent_file):
        if not p.exists():
            raise AggregateBuildError(f"input missing: {p}")
    files = sorted(parquet_dir.glob("*.parquet")) if parquet_dir.exists() else []
    if not files:
        raise AggregateBuildError(f"no fact parquet files in {parquet_dir}")
    agg_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    try:
        con.execute(f"SET memory_limit='{memory_limit}'")
        con.execute(f"SET threads={int(threads)}")
        con.execute(f"""
            CREATE TEMP TABLE dimdategroupid AS
            SELECT CAST(park_date AS DATE) AS park_date, date_group_id
            FROM read_csv_auto('{_q(dg_file)}')
        """)
        con.execute(f"""
            CREATE TEMP TABLE dimentity AS
            SELECT code AS entity_code, fastpass_booth
            FROM read_csv_auto('{_q(ent_file)}')
        """)
        # Posted rows through yesterday, deduped on the natural key.
        con.execute(f"""
            CREATE TEMP VIEW posted_raw AS
            SELECT f.entity_code, f.observed_at, f.wait_time_type,
                   f.wait_time_minutes, CAST(f.park_date AS DATE) AS park_date
            FROM read_parquet('{_q(parquet_dir)}/*.parquet', union_by_name=true) f
            WHERE f.wait_time_type = 'POSTED'
              AND f.wait_time_minutes IS NOT NULL
              AND f.wait_time_minutes > 0
              AND CAST(f.park_date AS DATE) < DATE '{as_of.isoformat()}'
        """)
        con.execute("""
            CREATE TEMP VIEW posted AS
            SELECT entity_code, observed_at, park_date,
                   MAX(wait_time_minutes) AS wait_time_minutes
            FROM posted_raw
            GROUP BY entity_code, observed_at, wait_time_type, park_date
        """)
        input_rows, input_max = con.execute(
            "SELECT COUNT(*), MAX(park_date) FROM posted_raw").fetchone()
        dedup_rows = con.execute("SELECT COUNT(*) FROM posted").fetchone()[0]
        dropped = int(input_rows) - int(dedup_rows)
        say(f"Input: {input_rows:,} posted rows from {len(files)} files, "
            f"{dropped:,} duplicate rows dropped, max park_date {input_max}")
        if not input_rows:
            raise AggregateBuildError("no posted rows before " + as_of.isoformat())

        parts_dir = agg_dir / ".model_aggregates.parts.tmp"
        _rmtree(parts_dir)
        parts_dir.mkdir()
        # Park-local wall clock: the first 19 chars of the ISO string, offset
        # ignored. CAST(<iso with offset> AS TIMESTAMP) is DuckDB-version
        # dependent (UTC-converted in some, offset dropped in 2.x), so we do
        # not rely on it. S8's grid and the dashboard use park-local slots.
        ts = "strptime(substr(CAST(f.observed_at AS VARCHAR), 1, 19), '%Y-%m-%dT%H:%M:%S')"
        w = f"POWER(0.5, DATEDIFF('day', f.park_date, DATE '{as_of.isoformat()}') / {HALF_LIFE_DAYS})"
        for b in range(int(buckets)):
          con.execute(f"""
            COPY (
                SELECT
                    f.entity_code,
                    COALESCE(d.date_group_id, 'UNKNOWN') AS date_group_id,
                    CAST(EXTRACT(HOUR FROM {ts}) * 4
                         + FLOOR(EXTRACT(MINUTE FROM {ts}) / 15) AS INTEGER) AS time_slot,
                    CAST(EXTRACT(HOUR FROM {ts}) AS INTEGER) AS hour_of_day,
                    CAST(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY f.wait_time_minutes) AS DOUBLE) AS wait_median,
                    CAST(AVG(f.wait_time_minutes) AS DOUBLE) AS wait_mean,
                    CAST(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY f.wait_time_minutes) AS DOUBLE) AS wait_p25,
                    CAST(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY f.wait_time_minutes) AS DOUBLE) AS wait_p75,
                    CAST(STDDEV(f.wait_time_minutes) AS DOUBLE) AS wait_std,
                    CAST(MIN(f.wait_time_minutes) AS BIGINT) AS wait_min,
                    CAST(MAX(f.wait_time_minutes) AS BIGINT) AS wait_max,
                    CAST(SUM(f.wait_time_minutes * {w}) / NULLIF(SUM({w}), 0) AS DOUBLE) AS wait_mean_weighted,
                    CAST(COUNT(*) AS BIGINT) AS sample_count,
                    CAST(COUNT(DISTINCT f.park_date) AS BIGINT) AS date_count,
                    MIN(f.park_date) AS min_park_date,
                    MAX(f.park_date) AS max_park_date
                FROM posted f
                LEFT JOIN dimdategroupid d ON f.park_date = d.park_date
                INNER JOIN dimentity e ON f.entity_code = e.entity_code
                WHERE e.fastpass_booth = FALSE
                  AND hash(f.entity_code) % {int(buckets)} = {b}
                GROUP BY 1, 2, 3, 4
                HAVING COUNT(*) >= {int(min_count)}
            ) TO '{_q(parts_dir / f"part_{b:03d}.parquet")}' (FORMAT PARQUET)
          """)
        con.execute(f"""
            COPY (
                SELECT * FROM read_parquet('{_q(parts_dir)}/*.parquet')
                ORDER BY entity_code, date_group_id, time_slot
            ) TO '{_q(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        _rmtree(parts_dir)

        stats = _validate(con, tmp, yesterday, require_full_day)
    except AggregateBuildError:
        _unlink(tmp)
        _rmtree(agg_dir / ".model_aggregates.parts.tmp")
        raise
    except Exception as e:  # duckdb, IO, anything: same contract
        _unlink(tmp)
        _rmtree(agg_dir / ".model_aggregates.parts.tmp")
        raise AggregateBuildError(f"{type(e).__name__}: {e}") from e
    finally:
        con.close()

    duration = round(time.time() - t0, 2)
    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "row_count": stats["rows"],
        "entity_count": stats["entities"],
        "input_rows": int(input_rows),
        "duplicate_rows_dropped": dropped,
        "input_max_date": input_max.isoformat(),
        "output_max_park_date": stats["max_park_date"].isoformat(),
        "min_count": int(min_count),
        "half_life_days": HALF_LIFE_DAYS,
        "duration_sec": duration,
    }
    try:
        sidecar_tmp.write_text(json.dumps(meta, indent=2) + "\n")
        os.replace(tmp, out)  # atomic on the same filesystem
        os.replace(sidecar_tmp, sidecar)
    except Exception as e:
        _unlink(tmp)
        _unlink(sidecar_tmp)
        raise AggregateBuildError(f"atomic replace failed: {e}") from e

    return BuildResult(
        rows=stats["rows"], entities=stats["entities"], input_rows=int(input_rows),
        duplicate_rows_dropped=dropped, input_max_date=input_max,
        output_max_park_date=stats["max_park_date"], duration_sec=duration,
        output_path=out, sidecar_path=sidecar,
    )


def _validate(con, path: Path, yesterday: date, require_full_day: bool) -> dict:
    schema = [(r[0], r[1]) for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{_q(path)}')").fetchall()]
    if schema != EXPECTED_SCHEMA:
        raise AggregateBuildError(f"schema mismatch: got {schema}")
    rows, ents, smin, smax, nslot, hmin, hmax, nhour, maxd, minn, zeros = con.execute(f"""
        SELECT COUNT(*), COUNT(DISTINCT entity_code),
               MIN(time_slot), MAX(time_slot), COUNT(DISTINCT time_slot),
               MIN(hour_of_day), MAX(hour_of_day), COUNT(DISTINCT hour_of_day),
               MAX(max_park_date), MIN(sample_count),
               COUNT(*) FILTER (WHERE wait_median IS NULL OR wait_median <= 0)
        FROM read_parquet('{_q(path)}')
    """).fetchone()
    if not rows:
        raise AggregateBuildError("built 0 rows")
    if smin < 0 or smax >= SLOTS_PER_DAY or hmin < 0 or hmax >= HOURS_PER_DAY:
        raise AggregateBuildError(f"slot/hour out of range: slots {smin}-{smax}, hours {hmin}-{hmax}")
    if require_full_day and (nslot != SLOTS_PER_DAY or nhour != HOURS_PER_DAY):
        raise AggregateBuildError(f"expected {SLOTS_PER_DAY} slots and {HOURS_PER_DAY} hours, got {nslot}/{nhour}")
    if zeros:
        raise AggregateBuildError(f"{zeros} rows with null/zero wait_median")
    if maxd is None or maxd < yesterday:
        raise AggregateBuildError(f"stale input: max park_date {maxd} < yesterday {yesterday}")
    return {"rows": int(rows), "entities": int(ents), "max_park_date": maxd, "min_sample_count": int(minn)}


def check_aggregates_freshness(
    path: Path, as_of: date | None = None
) -> tuple[bool, date | None, str]:
    """Is the aggregates file current? Same rule as _validate: max park_date >= yesterday.

    Prefers the sidecar's output_max_park_date (and checks its row_count against
    the file); without a readable sidecar, falls back to the parquet itself.
    Returns (fresh, max_park_date, reason).
    """
    yesterday = (as_of or date.today()) - timedelta(days=1)
    sidecar = sidecar_path_for(path)
    meta = None
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text())
            max_date = date.fromisoformat(meta["output_max_park_date"])
        except Exception:
            meta = None
    con = duckdb.connect()
    try:
        src = f"read_parquet('{_q(path)}')"
        if meta is None:
            max_date = con.execute(f"SELECT MAX(max_park_date) FROM {src}").fetchone()[0]
            note = "no sidecar" if not sidecar.exists() else "unreadable sidecar"
        else:
            note = "sidecar"
            expected = meta.get("row_count")
            if expected is not None:
                rows = con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]
                if int(expected) != int(rows):
                    return False, max_date, f"sidecar row_count {expected} != file rows {rows}"
    finally:
        con.close()
    if max_date is None or max_date < yesterday:
        return False, max_date, f"max park_date {max_date} < yesterday {yesterday} ({note})"
    return True, max_date, f"max park_date {max_date} ({note})"


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def _rmtree(p: Path) -> None:
    import shutil
    shutil.rmtree(p, ignore_errors=True)
