#!/usr/bin/env python3
"""
Build Actuals-Only Training Data (ACTUALS-FIRST methodology)

Creates training rows for forecast models that predict actual wait time from
temporal features ONLY — no posted_time. POSTED is used only for conversion
(synthetic actuals); forecasting deals in actuals.

Data sources:
- Synthetic actuals (90M+ rows) — weight 1.0
- Real ACTUAL observations (2.5M rows) — weight 3.5×

Output: matched_pairs/actuals_training_v2/{PARK}.parquet (per park, read by s07)
        and matched_pairs/actuals_training_v2.parquet (combined copy).
Every output file is written to a temp path and renamed into place only after
all parks built, so a crash leaves the previous complete files untouched.

Encodings come from state/encoding_mappings.json. A label that is not in the
mapping raises UnmappedLabelError instead of silently dropping the row; run
pipeline.core.encodings.refresh_mappings_file (s06b does) first.

Run daily by pipeline step s06b_training_data. Config (min_training_year) comes
from the repo's pipeline/config.py; there is no pipeline_v3 dependency.
Schema: entity_code, park_date, observed_at, observed_at_ts, actual_time,
        mins_since_6am, mins_since_open, hour_of_day,
        date_group_id, season, season_year,
        date_group_id_encoded, season_encoded, season_year_encoded,
        is_synthetic
"""

import argparse
import logging
import os
import resource
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import duckdb
import pandas as pd

# Ensure src is on path
if str(Path(__file__).parent.parent / "src") not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from utils.park_code import park_code_sql, entity_code_to_park_code

if str(Path(__file__).parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.config import PipelineConfig
from pipeline.core.encodings import encode, load_mappings

DEFAULT_OUTPUT_BASE = Path("/home/wilma/hazeydata/pipeline")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(__name__)


@dataclass(frozen=True)
class TrainingInputs:
    synth_dir: Path
    parquet_dir: Path
    dim_dir: Path
    encodings_path: Path
    oc_path: Path

    @classmethod
    def under(cls, output_base: Path) -> "TrainingInputs":
        return cls(
            synth_dir=output_base / "synthetic_actuals",
            parquet_dir=output_base / "fact_tables" / "parquet",
            dim_dir=output_base / "dimension_tables",
            encodings_path=output_base / "state" / "encoding_mappings.json",
            oc_path=output_base / "operating_calendar" / "operating_calendar.parquet",
        )


class TrainingBuildError(RuntimeError):
    pass


def _peak_rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _atomic_parquet(df: pd.DataFrame, dest: Path) -> None:
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, dest)


def build_actuals_training_data(
    logger,
    inputs: TrainingInputs,
    output_dir: Path,
    min_training_year: Callable[[str], int],
    write_combined: bool = True,
) -> dict:
    """Build actuals-only training data (synthetic + real, no posted_time).

    Returns a receipt: rows/real rows/max park_date/seconds/peak RSS per park.
    Raises TrainingBuildError (inputs missing, no rows) or UnmappedLabelError.
    """
    synth_dir = inputs.synth_dir
    parquet_dir = inputs.parquet_dir
    dim_dir = inputs.dim_dir
    oc_path = inputs.oc_path

    logger.info("=" * 60)
    logger.info("BUILDING ACTUALS-ONLY TRAINING DATA (ACTUALS-FIRST)")
    logger.info("=" * 60)

    start_time = time.time()

    # Check required paths
    if not synth_dir.exists() or not any(synth_dir.glob("*.parquet")):
        raise TrainingBuildError(f"Synthetic actuals not found: {synth_dir}")

    dategroupid_path = dim_dir / "dimdategroupid.csv"
    season_path = dim_dir / "dimseason.csv"
    parkhours_path = dim_dir / "dimparkhours.csv"
    entity_path = dim_dir / "dimentity.csv"

    for p in [dategroupid_path, season_path, entity_path]:
        if not p.exists():
            raise TrainingBuildError(f"Required dimension table not found: {p}")

    if not inputs.encodings_path.exists():
        raise TrainingBuildError(f"Encoding mappings not found: {inputs.encodings_path}")

    encodings = load_mappings(inputs.encodings_path)
    dg_mapping = encodings["date_group_id"]
    season_mapping = encodings["season"]
    sy_mapping = encodings["season_year"]

    use_operating_calendar = oc_path.exists()
    if use_operating_calendar:
        logger.info(f"Using operating calendar: {oc_path}")
    else:
        logger.info("Operating calendar not found; assuming all operating")

    con = duckdb.connect()

    synth_pattern = str(synth_dir / "*.parquet").replace("\\", "/")
    parquet_str = str(parquet_dir).replace("\\", "/")
    dg_path = str(dategroupid_path).replace("\\", "/")
    season_path_str = str(season_path).replace("\\", "/")
    ph_path = str(parkhours_path).replace("\\", "/")
    entity_path_str = str(entity_path).replace("\\", "/")

    if use_operating_calendar:
        oc_str = str(oc_path).replace("\\", "/")
        operating_filter = f"""
        operating AS (
            SELECT entity_code, CAST(park_date AS DATE) as park_date
            FROM read_parquet('{oc_str}')
            WHERE is_operating = TRUE
        ),
        """
        actual_filter = "AND EXISTS (SELECT 1 FROM operating o WHERE o.entity_code = a.entity_code AND o.park_date = CAST(a.park_date AS DATE))"
        synth_filter = "AND EXISTS (SELECT 1 FROM operating o WHERE o.entity_code = s.entity_code AND o.park_date = s.park_date)"
    else:
        operating_filter = ""
        actual_filter = ""
        synth_filter = ""

    # Park hours join using canonical park_code
    if parkhours_path.exists():
        parkhours_cte = f"""
        parkhours AS (
            SELECT park, CAST(date AS DATE) as park_date,
                   EXTRACT(HOUR FROM CAST(opening_time AS TIMESTAMP)) as open_hour,
                   EXTRACT(MINUTE FROM CAST(opening_time AS TIMESTAMP)) as open_minute
            FROM read_csv('{ph_path}', AUTO_DETECT=TRUE)
            WHERE opening_time IS NOT NULL
        ),
        """
        parkhours_join_synth = f"""
        LEFT JOIN parkhours ph ON ({park_code_sql("s.entity_code")}) = UPPER(ph.park) AND s.park_date = ph.park_date
        """
        parkhours_join_actual = f"""
        LEFT JOIN parkhours ph ON ({park_code_sql("a.entity_code")}) = UPPER(ph.park) AND a.park_date = ph.park_date
        """
        mins_since_open_synth = """
        CASE WHEN ph.open_hour IS NOT NULL THEN
            (EXTRACT(HOUR FROM CAST(s.observed_at AS TIMESTAMP)) - ph.open_hour) * 60 +
            (EXTRACT(MINUTE FROM CAST(s.observed_at AS TIMESTAMP)) - ph.open_minute)
        ELSE NULL END
        """
        mins_since_open_actual = """
        CASE WHEN ph.open_hour IS NOT NULL THEN
            (EXTRACT(HOUR FROM a.observed_at_ts) - ph.open_hour) * 60 +
            (EXTRACT(MINUTE FROM a.observed_at_ts) - ph.open_minute)
        ELSE NULL END
        """
    else:
        parkhours_cte = ""
        parkhours_join_synth = ""
        parkhours_join_actual = ""
        mins_since_open_synth = "NULL"
        mins_since_open_actual = "NULL"

    # Valid entities (standby only)
    valid_entities = f"""
    valid_entities AS (
        SELECT code as entity_code FROM read_csv_auto('{entity_path_str}')
        WHERE fastpass_booth = FALSE
    ),
    """

    # OOM-safe: chunk by park (per data-access-pattern: >30M rows)
    entities = sorted(
        r[0] for r in con.execute(
            f"SELECT DISTINCT entity_code FROM read_parquet('{synth_pattern}') WHERE entity_code IS NOT NULL"
        ).fetchall()
    )
    park_to_entities = {}
    for e in entities:
        park = entity_code_to_park_code(e)
        park_to_entities.setdefault(park, []).append(e)
    parks = sorted(park_to_entities.keys())
    logger.info(f"Chunking by park: {len(parks)} parks, {len(entities)} entities")

    output_dir.mkdir(parents=True, exist_ok=True)
    chunk_dir = output_dir / f".actuals_training_v2.staging-{os.getpid()}"
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir)
    chunk_dir.mkdir()

    total_rows = 0
    total_synthetic = 0
    total_real = 0
    by_park: dict[str, dict] = {}
    try:
        for park_idx, park_code in enumerate(parks, 1):
            park_start = time.time()
            chunk_entities = park_to_entities[park_code]
            entity_list = "', '".join(chunk_entities)
            entity_filter = f"AND s.entity_code IN ('{entity_list}')"
            entity_filter_actual = f"AND a.entity_code IN ('{entity_list}')"

            # Training data year cutoff (Issue #48: remove contaminated pre-2016 data)
            # Applied to BOTH real actuals AND synthetic data — the conversion model
            # that generated pre-2016 synthetics was trained on contaminated data,
            # and pre-2016 park operations (e.g., AK half-day) are fundamentally different.
            min_year = min_training_year(park_code)
            year_filter_actual = f"AND EXTRACT(YEAR FROM a.park_date::DATE) >= {min_year}" if min_year > 0 else ""
            year_filter_synth = f"AND EXTRACT(YEAR FROM s.park_date::DATE) >= {min_year}" if min_year > 0 else ""
            if min_year > 0:
                logger.info(f"  [{park_idx}/{len(parks)}] {park_code}: applying min_training_year={min_year} (real + synthetic)")

            query = f"""
            WITH {operating_filter}
            {valid_entities}
            {parkhours_cte}
            synthetic AS (
                SELECT
                    s.entity_code,
                    s.park_date,
                    s.observed_at,
                    CAST(s.observed_at AS TIMESTAMP) as observed_at_ts,
                    s.synthetic_actual as actual_time,
                    dg.date_group_id,
                    se.season,
                    se.season_year,
                    EXTRACT(HOUR FROM CAST(s.observed_at AS TIMESTAMP)) as hour_of_day,
                    (EXTRACT(HOUR FROM CAST(s.observed_at AS TIMESTAMP)) - 6) * 60 + EXTRACT(MINUTE FROM CAST(s.observed_at AS TIMESTAMP)) as mins_since_6am,
                    {mins_since_open_synth} as mins_since_open
                FROM read_parquet('{synth_pattern}') s
                INNER JOIN valid_entities ve ON s.entity_code = ve.entity_code
                LEFT JOIN read_csv('{dg_path}', AUTO_DETECT=TRUE) dg ON s.park_date = dg.park_date
                LEFT JOIN read_csv('{season_path_str}', AUTO_DETECT=TRUE) se ON s.park_date = se.park_date
                {parkhours_join_synth}
                WHERE dg.date_group_id IS NOT NULL AND se.season IS NOT NULL
                  AND s.synthetic_actual > 0
                  {entity_filter}
                  {synth_filter}
                  {year_filter_synth}
            ),
            real_actuals AS (
                SELECT
                    a.entity_code,
                    a.park_date,
                    a.observed_at,
                    a.observed_at_ts,
                    a.wait_time_minutes as actual_time,
                    dg.date_group_id,
                    se.season,
                    se.season_year,
                    EXTRACT(HOUR FROM a.observed_at_ts) as hour_of_day,
                    (EXTRACT(HOUR FROM a.observed_at_ts) - 6) * 60 + EXTRACT(MINUTE FROM a.observed_at_ts) as mins_since_6am,
                    {mins_since_open_actual} as mins_since_open
                FROM read_parquet('{parquet_str}/*.parquet') a
                INNER JOIN valid_entities ve ON a.entity_code = ve.entity_code
                LEFT JOIN read_csv('{dg_path}', AUTO_DETECT=TRUE) dg ON a.park_date = dg.park_date
                LEFT JOIN read_csv('{season_path_str}', AUTO_DETECT=TRUE) se ON a.park_date = se.park_date
                {parkhours_join_actual}
                WHERE a.wait_time_type = 'ACTUAL'
                  AND a.wait_time_minutes IS NOT NULL
                  AND a.wait_time_minutes > 0
                  {entity_filter_actual}
                  {year_filter_actual}
                  {actual_filter}
            ),
            combined AS (
                SELECT entity_code, park_date, observed_at, observed_at_ts, actual_time,
                       date_group_id, season, season_year, hour_of_day, mins_since_6am, mins_since_open,
                       TRUE as is_synthetic FROM synthetic
                UNION ALL
                SELECT entity_code, park_date, observed_at, observed_at_ts, actual_time,
                       date_group_id, season, season_year, hour_of_day, mins_since_6am, mins_since_open,
                       FALSE as is_synthetic FROM real_actuals
            )
            SELECT * FROM combined ORDER BY entity_code, observed_at
            """

            df = con.execute(query).fetchdf()
            if len(df) == 0:
                logger.info(f"  [{park_idx}/{len(parks)}] {park_code}: 0 rows (skip)")
                continue

            n_before = len(df)
            df = df.dropna(subset=["date_group_id", "season", "season_year"])
            no_dimension = n_before - len(df)
            if no_dimension:
                logger.warning(f"  [{park_idx}/{len(parks)}] {park_code}: {no_dimension:,} rows have no dimdategroupid/dimseason row (dropped)")
            df["date_group_id_encoded"] = encode(df["date_group_id"], dg_mapping, "date_group_id")
            df["season_encoded"] = encode(df["season"], season_mapping, "season")
            df["season_year_encoded"] = encode(df["season_year"], sy_mapping, "season_year")

            _atomic_parquet(df, chunk_dir / f"{park_code}.parquet")
            n_synth = int(df["is_synthetic"].sum())
            n_real = len(df) - n_synth
            real = df.loc[~df["is_synthetic"].astype(bool), "park_date"]
            by_park[park_code] = {
                "rows": len(df),
                "real_rows": n_real,
                "max_park_date": str(pd.to_datetime(df["park_date"]).max().date()),
                "max_real_park_date": str(pd.to_datetime(real).max().date()) if len(real) else None,
                "dropped_no_dimension": no_dimension,
                "seconds": round(time.time() - park_start, 1),
                "peak_rss_mb": _peak_rss_mb(),
            }
            logger.info(f"  [{park_idx}/{len(parks)}] {park_code}: {len(df):,} rows (synth: {n_synth:,}, real: {n_real:,})")
            del df
    except BaseException:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        raise
    finally:
        con.close()

    for stats in by_park.values():
        total_rows += stats["rows"]
        total_real += stats["real_rows"]
        total_synthetic += stats["rows"] - stats["real_rows"]

    if total_rows == 0:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        raise TrainingBuildError("No actuals training rows generated")

    output_dir_parks = output_dir / "actuals_training_v2"
    output_dir_parks.mkdir(exist_ok=True)
    for park_code in sorted(by_park):
        os.replace(chunk_dir / f"{park_code}.parquet", output_dir_parks / f"{park_code}.parquet")
    shutil.rmtree(chunk_dir, ignore_errors=True)

    combined_path = output_dir / "actuals_training_v2.parquet"
    if write_combined:
        combined_tmp = output_dir / f".actuals_training_v2.parquet.tmp-{os.getpid()}"
        pattern = str(output_dir_parks / "*.parquet").replace("\\", "/")
        con2 = duckdb.connect()
        try:
            con2.execute(f"""
                COPY (
                    SELECT * FROM read_parquet('{pattern}')
                    ORDER BY entity_code, observed_at
                ) TO '{str(combined_tmp)}' (FORMAT PARQUET)
            """)
        except BaseException:
            combined_tmp.unlink(missing_ok=True)
            raise
        finally:
            con2.close()
        os.replace(combined_tmp, combined_path)

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("ACTUALS TRAINING DATA BUILD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Output: {output_dir_parks}/ (per-park){' + ' + str(combined_path) if write_combined else ''}")
    logger.info(f"Rows: {total_rows:,} (synthetic: {total_synthetic:,}, real: {total_real:,})")
    logger.info(f"Build time: {elapsed:.1f}s, peak RSS {_peak_rss_mb()} MB")

    return {
        "rows": total_rows,
        "real_rows": total_real,
        "synthetic_rows": total_synthetic,
        "seconds": round(elapsed, 1),
        "peak_rss_mb": _peak_rss_mb(),
        "by_park": by_park,
        "output_dir": str(output_dir_parks),
    }


def main():
    parser = argparse.ArgumentParser(description="Build actuals-only training data")
    parser.add_argument("--output-base", type=Path, default=DEFAULT_OUTPUT_BASE)
    args = parser.parse_args()

    logger = setup_logging()
    output_base = args.output_base.resolve()
    cfg = PipelineConfig(output_base=output_base)
    try:
        build_actuals_training_data(
            logger,
            TrainingInputs.under(output_base),
            output_base / "matched_pairs",
            cfg.get_min_training_year,
        )
    except Exception as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
