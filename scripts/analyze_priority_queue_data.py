#!/usr/bin/env python3
"""
Analyze priority queue data in the pipeline.

Answers: What do we have? How is it structured?

Run from repo root. Uses output_base from config (or DEV_MODE pipeline_dev).
  python scripts/analyze_priority_queue_data.py
  python scripts/analyze_priority_queue_data.py --output-base /path/to/pipeline
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
from src.utils.paths import get_output_base


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze priority queue data")
    ap.add_argument("--output-base", type=Path, help="Override output_base (e.g. /mnt/data/pipeline)")
    args = ap.parse_args()
    output_base = args.output_base or get_output_base()
    parquet_dir = output_base / "fact_tables" / "parquet"
    dim_dir = output_base / "dimension_tables"
    entity_index_path = output_base / "state" / "entity_index.sqlite"
    dimentity_path = dim_dir / "dimentity.csv"

    print("=" * 70)
    print("PRIORITY QUEUE DATA ANALYSIS")
    print("=" * 70)
    print(f"Output base: {output_base}")
    print(f"Parquet dir: {parquet_dir}")
    print()

    if not parquet_dir.exists():
        print("ERROR: Parquet directory not found. Run ETL first to produce fact_tables/parquet.")
        sys.exit(1)

    con = duckdb.connect()

    # --- 1. Fact table schema ---
    print("1. FACT TABLE SCHEMA (from Parquet)")
    print("-" * 50)
    try:
        schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{parquet_dir}/*.parquet') LIMIT 0").fetchall()
        for row in schema:
            print(f"  {row[0]:25} {row[1]}")
    except Exception as e:
        print(f"  Error: {e}")
    print()

    # --- 2. wait_time_type distribution ---
    print("2. WAIT_TIME_TYPE DISTRIBUTION")
    print("-" * 50)
    try:
        dist = con.execute(f"""
            SELECT wait_time_type, COUNT(*) as cnt
            FROM read_parquet('{parquet_dir}/*.parquet')
            GROUP BY wait_time_type
            ORDER BY cnt DESC
        """).fetchdf()
        for _, row in dist.iterrows():
            print(f"  {row['wait_time_type']:12} {row['cnt']:>12,} rows")
    except Exception as e:
        print(f"  Error: {e}")
    print()

    # --- 3. PRIORITY data summary ---
    print("3. PRIORITY DATA SUMMARY")
    print("-" * 50)
    try:
        prio = con.execute(f"""
            SELECT
                COUNT(*) as total_rows,
                COUNT(DISTINCT entity_code) as distinct_entities,
                MIN(observed_at) as min_observed,
                MAX(observed_at) as max_observed,
                MIN(wait_time_minutes) as min_minutes,
                MAX(wait_time_minutes) as max_minutes,
                SUM(CASE WHEN wait_time_minutes = 8888 THEN 1 ELSE 0 END) as sold_out_8888_count
            FROM read_parquet('{parquet_dir}/*.parquet')
            WHERE wait_time_type = 'PRIORITY'
        """).fetchdf()
        row = prio.iloc[0]
        print(f"  Total PRIORITY rows:     {row['total_rows']:>12,}")
        print(f"  Distinct entities:       {row['distinct_entities']:>12,}")
        print(f"  Date range:              {row['min_observed']} to {row['max_observed']}")
        print(f"  wait_time_minutes range: {row['min_minutes']} to {row['max_minutes']}")
        print(f"  Sold-out (8888) count:   {row['sold_out_8888_count']:>12,}")
    except Exception as e:
        print(f"  Error: {e}")
    print()

    # --- 4. Top PRIORITY entities by row count ---
    print("4. TOP 15 PRIORITY ENTITIES (by row count)")
    print("-" * 50)
    try:
        top = con.execute(f"""
            SELECT entity_code, COUNT(*) as cnt
            FROM read_parquet('{parquet_dir}/*.parquet')
            WHERE wait_time_type = 'PRIORITY'
            GROUP BY entity_code
            ORDER BY cnt DESC
            LIMIT 15
        """).fetchdf()
        for _, r in top.iterrows():
            print(f"  {r['entity_code']:12} {r['cnt']:>10,} rows")
    except Exception as e:
        print(f"  Error: {e}")
    print()

    # --- 5. dimentity: fastpass_booth entities ---
    print("5. DIMENTITY: PRIORITY QUEUE ENTITIES (fastpass_booth=TRUE)")
    print("-" * 50)
    if dimentity_path.exists():
        try:
            dim_str = str(dimentity_path).replace("\\", "/")
            fp_count = con.execute(f"""
                SELECT COUNT(*) as cnt
                FROM read_csv_auto('{dim_str}')
                WHERE LOWER(TRIM(CAST(fastpass_booth AS VARCHAR))) IN ('true', '1', 'yes', 't')
            """).fetchdf()
            print(f"  fastpass_booth=TRUE entities in dimentity: {fp_count.iloc[0]['cnt']}")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        print("  dimentity.csv not found")
    print()

    # --- 6. Entity index: priority_count ---
    print("6. ENTITY INDEX: priority_count")
    print("-" * 50)
    if entity_index_path.exists():
        try:
            idx = con.execute(f"""
                SELECT
                    COUNT(*) as total_entities,
                    SUM(priority_count) as total_priority_in_index,
                    COUNT(CASE WHEN priority_count > 0 THEN 1 END) as entities_with_priority,
                    COUNT(CASE WHEN priority_count >= 500 THEN 1 END) as entities_meeting_500_threshold
                FROM sqlite_scan('{entity_index_path}', 'entity_index')
            """).fetchdf()
            row = idx.iloc[0]
            print(f"  Total entities in index:           {row['total_entities']}")
            print(f"  Entities with priority_count > 0:   {row['entities_with_priority']}")
            print(f"  Entities with priority_count >= 500: {row['entities_meeting_500_threshold']}")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        print("  entity_index.sqlite not found")
    print()

    # --- 7. Sample PRIORITY rows ---
    print("7. SAMPLE PRIORITY ROWS (first 5)")
    print("-" * 50)
    try:
        sample = con.execute(f"""
            SELECT entity_code, observed_at, wait_time_type, wait_time_minutes
            FROM read_parquet('{parquet_dir}/*.parquet')
            WHERE wait_time_type = 'PRIORITY'
            ORDER BY observed_at
            LIMIT 5
        """).fetchdf()
        print(sample.to_string(index=False))
    except Exception as e:
        print(f"  Error: {e}")

    con.close()
    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
