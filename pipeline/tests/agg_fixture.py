"""Synthetic fact/dimension tables for model_aggregates tests and the timing bench.

Shape matches the real inputs the builder reads: fact_tables/parquet/*.parquet
with entity_code, observed_at (ISO string), wait_time_type, wait_time_minutes,
park_date; dimension_tables/dimdategroupid.csv and dimentity.csv.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb


def write_dims(base: Path, entities: list[str], start: date, end: date,
               booths: tuple[str, ...] = (), n_groups: int = 1) -> None:
    dim = base / "dimension_tables"
    dim.mkdir(parents=True, exist_ok=True)
    lines = ["park_date,date_group_id"]
    d, i = start, 0
    while d <= end:
        lines.append(f"{d.isoformat()},DG{i % n_groups:03d}")
        d += timedelta(days=1)
        i += 1
    (dim / "dimdategroupid.csv").write_text("\n".join(lines) + "\n")
    rows = ["code,fastpass_booth"] + [f"{e},{'true' if e in booths else 'false'}" for e in entities]
    (dim / "dimentity.csv").write_text("\n".join(rows) + "\n")


def write_synthetic_facts(base: Path, n_entities: int, start: date, end: date,
                          step_min: int = 15, n_groups: int = 1, seed: int = 7) -> int:
    """One posted observation per entity every ``step_min`` minutes over 24h, every day.

    Written as one parquet file per month, like fact_tables/parquet. Returns row count.
    """
    pq = base / "fact_tables" / "parquet"
    pq.mkdir(parents=True, exist_ok=True)
    entities = [f"E{i:04d}" for i in range(n_entities)]
    write_dims(base, entities, start, end, n_groups=n_groups)
    con = duckdb.connect()
    con.execute("SET memory_limit='2GB'")
    per_day = 1440 // step_min
    n = 0
    m0 = date(start.year, start.month, 1)
    while m0 <= end:
        m1 = date(m0.year + (m0.month == 12), m0.month % 12 + 1, 1)
        lo, hi = max(m0, start), min(m1 - timedelta(days=1), end)
        out = pq / f"wait_times_{m0:%Y-%m}.parquet"
        con.execute(f"""
            COPY (
              SELECT 'E' || lpad(CAST(e AS VARCHAR), 4, '0') AS entity_code,
                     strftime(CAST(d AS TIMESTAMP) + to_minutes(CAST(s * {step_min} AS BIGINT)),
                              '%Y-%m-%dT%H:%M:%S') || '-04:00' AS observed_at,
                     'POSTED' AS wait_time_type,
                     CAST(5 + (hash(e, d, s, {seed}) % 90) AS INTEGER) AS wait_time_minutes,
                     CAST(d AS DATE) AS park_date
              FROM range({n_entities}) t(e),
                   range(DATE '{lo}', DATE '{hi + timedelta(days=1)}', INTERVAL 1 DAY) r(d),
                   range({per_day}) q(s)
            ) TO '{out}' (FORMAT PARQUET)
        """)
        n += n_entities * ((hi - lo).days + 1) * per_day
        m0 = m1
    con.close()
    return n
