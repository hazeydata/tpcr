#!/usr/bin/env python3
"""
Convert CSV fact tables to Parquet format for 10-50x faster reads.

This is a one-time conversion that will dramatically speed up all pipeline operations.
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(__name__)


def convert_month_to_parquet(args):
    """Convert all CSVs in a month directory to a single Parquet file."""
    month_dir, output_dir = args
    
    try:
        dfs = []
        for csv_file in sorted(month_dir.glob("*.csv")):
            df = pd.read_csv(csv_file, low_memory=False)
            df["entity_code"] = df["entity_code"].str.upper()
            df["source_file"] = csv_file.stem
            dfs.append(df)
        
        if not dfs:
            return None, 0
        
        combined = pd.concat(dfs, ignore_index=True)
        
        # Parse timestamp and add useful columns
        combined["observed_at_ts"] = pd.to_datetime(combined["observed_at"], utc=True, errors="coerce")
        combined["park_date"] = combined["observed_at_ts"].dt.date.astype(str)
        
        # Derive park_code from entity_code
        def get_park(entity):
            import re
            s = str(entity).upper()
            m = re.search(r"\d", s)
            prefix = s[:m.start()] if m else s
            return prefix.lower()
        
        combined["park_code"] = combined["entity_code"].apply(get_park)

        # Exclude water parks (BB, TL, VB) per config.ignore_parks
        excluded_parks = {"bb", "tl", "vb"}
        before = len(combined)
        combined = combined[~combined["park_code"].isin(excluded_parks)]
        dropped = before - len(combined)
        if dropped > 0:
            logging.getLogger(__name__).info(f"  {month_dir.name}: dropped {dropped} water park rows")

        # Output path
        output_path = output_dir / f"{month_dir.name}.parquet"
        combined.to_parquet(output_path, index=False, compression="snappy")
        
        return month_dir.name, len(combined)
    
    except Exception as e:
        return month_dir.name, f"ERROR: {e}"


def main():
    parser = argparse.ArgumentParser(description="Convert CSVs to Parquet")
    parser.add_argument("--input", default="/home/wilma/hazeydata/pipeline/fact_tables/clean",
                        help="Input directory with month subdirectories")
    parser.add_argument("--output", default="/home/wilma/hazeydata/pipeline/fact_tables/parquet",
                        help="Output directory for Parquet files")
    parser.add_argument("--workers", type=int, default=2, help="Number of parallel workers")
    parser.add_argument("--recent-months", type=int, default=2,
                        help="Only consider the N most recent month dirs (0 = all; full rebuild)")
    parser.add_argument("--force", action="store_true", help="Reconvert even if parquet is up to date")
    
    args = parser.parse_args()
    logger = setup_logging()
    
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Get all month directories
    if not input_dir.is_dir():
        logger.warning(f"Input dir {input_dir} missing; nothing to convert")
        return
    month_dirs = sorted([d for d in input_dir.iterdir() if d.is_dir()])
    if args.recent_months > 0:
        month_dirs = month_dirs[-args.recent_months:]
    if not args.force:
        def stale(d):
            out = output_dir / f"{d.name}.parquet"
            if not out.exists():
                return True
            newest = max((f.stat().st_mtime for f in d.glob("*.csv")), default=0)
            return newest > out.stat().st_mtime
        month_dirs = [d for d in month_dirs if stale(d)]
    logger.info(f"Found {len(month_dirs)} month directories to convert")
    
    # Convert in parallel
    total_rows = 0
    completed = 0
    
    work_items = [(d, output_dir) for d in month_dirs]
    
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(convert_month_to_parquet, item): item[0].name 
                   for item in work_items}
        
        for future in as_completed(futures):
            month = futures[future]
            result = future.result()
            
            if result[1] and not isinstance(result[1], str):
                total_rows += result[1]
                completed += 1
                if completed % 20 == 0:
                    logger.info(f"  Converted {completed}/{len(month_dirs)} months ({total_rows:,} rows)")
            elif isinstance(result[1], str):
                logger.warning(f"  {month}: {result[1]}")
    
    logger.info(f"\n{'='*60}")
    logger.info(f"CONVERSION COMPLETE")
    logger.info(f"{'='*60}")
    logger.info(f"Total months: {completed}")
    logger.info(f"Total rows: {total_rows:,}")
    logger.info(f"Output: {output_dir}")
    
    # Show size comparison
    if not completed:
        return
    csv_size = sum(f.stat().st_size for d in month_dirs for f in d.glob("*.csv")) or 1
    parquet_size = sum(f.stat().st_size for f in output_dir.glob("*.parquet"))
    
    logger.info(f"\nSize comparison:")
    logger.info(f"  CSV: {csv_size / 1e9:.2f} GB")
    logger.info(f"  Parquet: {parquet_size / 1e9:.2f} GB")
    logger.info(f"  Compression: {parquet_size/csv_size*100:.1f}%")


if __name__ == "__main__":
    main()
