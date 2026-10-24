"""TPCR-S4-AGG-001: daily model_aggregates rebuild (S4) and S8 fallback logging."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from pipeline.config import load_config
from pipeline.core.logging import PipelineLogger
from pipeline.core.model_aggregates import (
    EXPECTED_SCHEMA, AggregateBuildError, build_model_aggregates, sidecar_path_for,
)
from pipeline.core.validation import ValidationError
from pipeline.steps import s04_aggregates, s08_forecast
from pipeline.tests.agg_fixture import write_dims, write_synthetic_facts

AS_OF = date(2026, 10, 23)
YESTERDAY = AS_OF - timedelta(days=1)


def _write_facts(base: Path, df: pd.DataFrame, name: str) -> None:
    d = base / "fact_tables" / "parquet"
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / name, index=False)


def _small_facts(entities=("MK01", "EP02", "FP99"), days=10, seed=1) -> pd.DataFrame:
    """Every 15 min, 24h, ``days`` days ending yesterday; random posted waits."""
    rng = np.random.default_rng(seed)
    rows = []
    for e in entities:
        for k in range(days):
            d = YESTERDAY - timedelta(days=k)
            for s in range(96):
                t = datetime(d.year, d.month, d.day, s // 4, (s % 4) * 15)
                rows.append((e, t.strftime("%Y-%m-%dT%H:%M:%S") + "-04:00", "POSTED",
                             int(rng.integers(1, 120)), d))
    return pd.DataFrame(rows, columns=["entity_code", "observed_at", "wait_time_type",
                                       "wait_time_minutes", "park_date"])


def _base(tmp_path: Path, facts: pd.DataFrame, groups: int = 2) -> Path:
    base = tmp_path / "pipeline"
    write_dims(base, ["MK01", "EP02", "FP99"], date(2009, 1, 1), AS_OF, booths=("FP99",), n_groups=groups)
    _write_facts(base, facts, "wait_times_2026-10.parquet")
    return base


def _read(base: Path) -> pd.DataFrame:
    return pd.read_parquet(base / "aggregates" / "model_aggregates.parquet")


def _reference(base: Path, facts: pd.DataFrame, min_count: int) -> pd.DataFrame:
    """The builder's method written independently in pandas."""
    dg = pd.read_csv(base / "dimension_tables" / "dimdategroupid.csv", parse_dates=["park_date"])
    f = facts[(facts.wait_time_type == "POSTED") & (facts.wait_time_minutes > 0)].copy()
    f = f[f.entity_code != "FP99"]
    f["park_date"] = pd.to_datetime(f["park_date"])
    f = f.merge(dg, on="park_date", how="left")
    # park-local wall clock from the ISO string (offset ignored)
    ts = pd.to_datetime(f["observed_at"].str.slice(0, 19))
    f["time_slot"] = ts.dt.hour * 4 + ts.dt.minute // 15
    f["hour_of_day"] = ts.dt.hour
    f["w"] = 0.5 ** ((pd.Timestamp(AS_OF) - f["park_date"]).dt.days / 730.0)
    f["ww"] = f["w"] * f["wait_time_minutes"]
    g = f.groupby(["entity_code", "date_group_id", "time_slot", "hour_of_day"])
    out = pd.DataFrame({
        "wait_median": g.wait_time_minutes.median(),
        "wait_mean": g.wait_time_minutes.mean(),
        "wait_p25": g.wait_time_minutes.quantile(0.25),
        "wait_p75": g.wait_time_minutes.quantile(0.75),
        "wait_std": g.wait_time_minutes.std(),
        "wait_min": g.wait_time_minutes.min(),
        "wait_max": g.wait_time_minutes.max(),
        "wait_mean_weighted": g.ww.sum() / g.w.sum(),
        "sample_count": g.size(),
        "date_count": g.park_date.nunique(),
    }).reset_index()
    return out[out.sample_count >= min_count]


def test_values_match_builder_method_on_fixed_fixture(tmp_path):  # AC8
    facts = _small_facts()
    base = _base(tmp_path, facts)
    build_model_aggregates(base, as_of=AS_OF)
    got = _read(base).sort_values(["entity_code", "date_group_id", "time_slot"]).reset_index(drop=True)
    ref = _reference(base, facts, 5).sort_values(["entity_code", "date_group_id", "time_slot"]).reset_index(drop=True)
    assert len(got) == len(ref) > 0
    assert set(got.entity_code) == {"MK01", "EP02"}  # fastpass booth excluded
    for c in ["entity_code", "date_group_id", "time_slot", "hour_of_day", "wait_min", "wait_max",
              "sample_count", "date_count"]:
        assert (got[c].astype(str).values == ref[c].astype(str).values).all(), c
    for c in ["wait_median", "wait_mean", "wait_p25", "wait_p75", "wait_std", "wait_mean_weighted"]:
        np.testing.assert_allclose(got[c].values, ref[c].values, rtol=1e-9, err_msg=c)


def test_schema_identical_to_current_file(tmp_path):  # AC12
    base = _base(tmp_path, _small_facts())
    build_model_aggregates(base, as_of=AS_OF)
    path = base / "aggregates" / "model_aggregates.parquet"
    schema = pq.read_schema(path)
    assert [(f.name, str(f.type)) for f in schema] == [
        ("entity_code", "string"), ("date_group_id", "string"), ("time_slot", "int32"),
        ("hour_of_day", "int32"), ("wait_median", "double"), ("wait_mean", "double"),
        ("wait_p25", "double"), ("wait_p75", "double"), ("wait_std", "double"),
        ("wait_min", "int64"), ("wait_max", "int64"), ("wait_mean_weighted", "double"),
        ("sample_count", "int64"), ("date_count", "int64"),
        ("min_park_date", "date32[day]"), ("max_park_date", "date32[day]"),
    ]
    df = _read(base)
    assert sorted(df.time_slot.unique()) == list(range(96))
    assert sorted(df.hour_of_day.unique()) == list(range(24))
    assert df.max_park_date.max() >= YESTERDAY


def test_under_min_count_is_absent_never_zero(tmp_path):  # AC2
    facts = _small_facts(days=4)  # 4 obs per (entity, dg, slot) with one date group
    base = _base(tmp_path, facts, groups=1)
    with pytest.raises(AggregateBuildError, match="built 0 rows"):
        build_model_aggregates(base, as_of=AS_OF)
    # Now give MK01 a 5th day: its cells appear, EP02 (4 obs) stays absent.
    d5 = _small_facts(entities=("MK01",), days=5)
    _write_facts(base, d5[d5.park_date == YESTERDAY - timedelta(days=4)], "wait_times_2026-09.parquet")
    build_model_aggregates(base, as_of=AS_OF)
    df = _read(base)
    assert set(df.entity_code) == {"MK01"}
    assert df.sample_count.min() == 5
    assert (df.wait_median > 0).all() and (df.wait_mean_weighted > 0).all()


def test_duplicate_rows_count_once(tmp_path):  # AC5
    facts = _small_facts()
    base = _base(tmp_path, facts)
    build_model_aggregates(base, as_of=AS_OF)
    clean = _read(base)
    # Oct 10-11 style: the same rows appended again in another file
    dup = facts[facts.park_date >= YESTERDAY - timedelta(days=1)]
    _write_facts(base, dup, "wait_times_2026-10_restaged.parquet")
    res = build_model_aggregates(base, as_of=AS_OF)
    assert res.duplicate_rows_dropped == len(dup)
    pd.testing.assert_frame_equal(_read(base), clean)


def test_failure_keeps_previous_file_byte_identical(tmp_path, caplog):  # AC3, AC4
    base = _base(tmp_path, _small_facts())
    build_model_aggregates(base, as_of=AS_OF)
    path = base / "aggregates" / "model_aggregates.parquet"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    # Corrupt input: a fact file that is not parquet
    (base / "fact_tables" / "parquet" / "wait_times_2026-11.parquet").write_bytes(b"not parquet")
    cfg = load_config(output_base=base)
    with caplog.at_level(logging.ERROR), pytest.raises(ValidationError, match="S4 aggregate build failed"):
        s04_aggregates.run(cfg, PipelineLogger("s04_test"))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert "previous file kept" in caplog.text and "wait_times_2026-11" in caplog.text
    assert sorted(p.name for p in path.parent.iterdir()) == ["model_aggregates.meta.json", "model_aggregates.parquet"]


def test_stale_input_fails_and_missing_inputs_fail(tmp_path):  # AC1, AC4
    facts = _small_facts()
    base = _base(tmp_path, facts[facts.park_date < YESTERDAY])
    with pytest.raises(AggregateBuildError, match="stale input"):
        build_model_aggregates(base, as_of=AS_OF)
    assert not (base / "aggregates" / "model_aggregates.parquet").exists()
    empty = tmp_path / "empty"
    with pytest.raises(ValidationError):
        s04_aggregates.run(load_config(output_base=empty), PipelineLogger("s04_test"))


def test_s4_run_rebuilds_and_writes_sidecar(tmp_path, monkeypatch):  # AC1, AC7
    base = _base(tmp_path, _small_facts())
    monkeypatch.setattr("pipeline.core.model_aggregates.date",
                        type("D", (date,), {"today": staticmethod(lambda: AS_OF)}))
    res = s04_aggregates.run(load_config(output_base=base), PipelineLogger("s04_test"))
    assert res["action"] == "rebuilt" and res["rows"] > 0
    path = base / "aggregates" / "model_aggregates.parquet"
    meta = json.loads(sidecar_path_for(path).read_text())
    assert meta["row_count"] == res["rows"] == len(_read(base))
    assert meta["input_max_date"] == YESTERDAY.isoformat()
    assert datetime.fromisoformat(meta["built_at"])
    assert meta["min_count"] == 5


def test_expected_schema_constant_matches_parquet_types():
    assert [c for c, _ in EXPECTED_SCHEMA][:4] == ["entity_code", "date_group_id", "time_slot", "hour_of_day"]


def test_build_time_on_representative_fixture(tmp_path):  # AC11 (box evidence only)
    """~10.5M posted rows (300 entities x 365 days x 96 slots) must build well
    inside budget. At this rate 10 minutes covers ~10.5M * 600 / elapsed rows."""
    n = write_synthetic_facts(tmp_path / "p", 300, AS_OF - timedelta(days=365), YESTERDAY,
                              step_min=15, n_groups=24)
    res = build_model_aggregates(tmp_path / "p", as_of=AS_OF)
    rate = n / res.duration_sec
    print(f"timing: input_rows={n} out_rows={res.rows} build_sec={res.duration_sec} rows_per_sec={rate:,.0f}")
    assert res.rows > 0
    # Fixture is ~1/20 of the estimated real input (see PR body); keep it
    # under 1/10 of the 600 s budget so a regression to >10 min at real size
    # shows up here. The real-size number must be measured on the Mini (R3).
    assert res.duration_sec <= 60


# ---------------------------------------------------------------- S8 (AC6)

def _grid(dg="DG000"):
    rows = []
    for s in range(36, 40):
        rows.append({"park_date": AS_OF, "time_slot": f"{s // 4:02d}:{(s % 4) * 15:02d}",
                     "time_slot_15min": s, "hour_of_day": s // 4,
                     "mins_since_6am": max(0, (s // 4 - 6) * 60 + (s % 4) * 15),
                     "date_group_id": dg, "date_group_id_encoded": 0,
                     "season_encoded": 0, "season_year_encoded": 0})
    return pd.DataFrame(rows)


def test_s8_records_every_fallback_with_entity_and_reason(tmp_path, caplog):
    agg = {"MK01": pd.DataFrame({"date_group_id": ["DG000", "DG000"], "time_slot_15min": [36, 37],
                                 "wait_median": [30.0, 40.0]})}
    fb: list = []
    for e in ("MK01", "EP02"):
        df, feat = s08_forecast._forecast_entity_fast(
            e, "MK", _grid(), tmp_path / "models", agg, {}, {"__global__": 0.5}, None, fb)
        assert feat == "fallback"
    assert ("MK01", "posted_default", "no aggregate for (date_group_id, slot)", 2) in fb
    assert ("EP02", "posted_default", "entity not in model_aggregates", 4) in fb
    assert ("EP02", "ratio", "no model file; global ratio 0.5", 4) in fb
    with caplog.at_level(logging.INFO):
        out = s08_forecast._log_fallbacks(fb, PipelineLogger("s08_test"))
    assert out["posted_fallback_entities"] == ["EP02", "MK01"]
    assert out["ratio_fallback_entities"] == ["EP02", "MK01"]
    assert "S8 fallback posted_default: EP02: entity not in model_aggregates (4 rows)" in caplog.text
    assert "forecast_posted_fallback_entities=2" in caplog.text


def test_s8_has_no_silent_posted_default():
    src = Path(s08_forecast.__file__).read_text()
    assert "fillna(5.0)" not in src and '"posted_time"] = 5.0' not in src


def test_time_slot_is_park_local_wall_clock(tmp_path):
    """10:30-04:00 is slot 42 (10*4+2), not the UTC slot 58."""
    facts = _small_facts(entities=("MK01",), days=6)
    base = _base(tmp_path, facts, groups=1)
    build_model_aggregates(base, as_of=AS_OF)
    df = _read(base)
    want = facts[facts.observed_at.str.slice(11, 16) == "10:30"].wait_time_minutes.median()
    row = df[df.time_slot == 42].iloc[0]
    assert row.hour_of_day == 10 and row.wait_median == want
