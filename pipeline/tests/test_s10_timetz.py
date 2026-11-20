"""s10 must read forecast archives whose time_slot is TIME WITH TIME ZONE.

Older pyarrow writes parquet TIME with isAdjustedToUTC=true, which DuckDB reads
as TIME WITH TIME ZONE; fetching that to pandas raised
'Unsupported type "TIME WITH TIME ZONE"' on the Mini (TPCR-BIAS-001 shadow run).
"""
from pathlib import Path

import duckdb
import pytest

SRC = (Path(__file__).resolve().parents[1] / "steps" / "s10_accuracy.py").read_text()


def test_s10_casts_archived_time_slot_to_time():
    assert SRC.count("CAST(time_slot AS TIME)") == 2


def test_timetz_archive_fetches_after_cast(tmp_path):
    p = tmp_path / "forecast.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 'AK86' e, '10:05:00+00'::TIMETZ time_slot) TO '{p}' (FORMAT PARQUET)")
    df = con.execute(f"SELECT CAST(time_slot AS TIME) AS time_slot FROM read_parquet('{p}')").fetchdf()
    assert str(df.time_slot[0]) == "10:05:00"
    joined = con.execute(
        f"SELECT count(*) FROM read_parquet('{p}') f JOIN (SELECT TIME '10:05:00' t) a "
        f"ON CAST(f.time_slot AS TIME) = a.t").fetchone()[0]
    assert joined == 1
