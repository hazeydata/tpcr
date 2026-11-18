"""TPCR-ACCURACY-001: leak-free recent-level feature."""
from __future__ import annotations

import json
from datetime import date, time as dtime, timedelta

import numpy as np
import pandas as pd
import pytest

from pipeline.config import PipelineConfig
from pipeline.core import recent_level as rl
from pipeline.core.logging import PipelineLogger
from pipeline.steps import s07_training, s08_forecast

START = date(2026, 1, 1)
N_DAYS = 120


def _frame(rows_per_day=2, value=lambda i: 10.0 + i * 1.5, entity="AK85"):
    recs = []
    for i in range(N_DAYS):
        for k in range(rows_per_day):
            recs.append({"entity_code": entity, "park_date": START + timedelta(days=i),
                         "actual_time": value(i), "mins_since_6am": 60 * (k + 3),
                         "mins_since_open": 60 * k, "date_group_id_encoded": i % 3,
                         "season_encoded": 1, "season_year_encoded": 2,
                         "is_synthetic": bool(k % 2)})
    return pd.DataFrame(recs)


def test_training_feature_is_exact_lagged_window_mean():
    df = _frame(rows_per_day=2)
    out = rl.recent_level_for_rows(df["park_date"], df["actual_time"], 28, 7, 50)
    for r, d in enumerate(df["park_date"]):
        i = (d - START).days
        days = [j for j in range(i - 35, i - 7) if 0 <= j < N_DAYS]
        n = 2 * len(days)
        if n < 50:
            assert np.isnan(out[r])
        else:
            assert out[r] == pytest.approx(np.mean([10.0 + j * 1.5 for j in days]))
    assert np.isfinite(out).any() and np.isnan(out).any()


def test_training_feature_ignores_days_on_or_after_d_minus_lag():
    df = _frame(rows_per_day=2)
    base = rl.recent_level_for_rows(df["park_date"], df["actual_time"], 28, 7, 50)
    day = (pd.to_datetime(df["park_date"]) - pd.Timestamp(START)).dt.days.to_numpy()
    for i in (45, 70, 100):
        mutated = df["actual_time"].to_numpy().copy()
        mutated[day >= i - 7] += 1000.0
        new = rl.recent_level_for_rows(df["park_date"], mutated, 28, 7, 50)
        rows_d = day == i
        np.testing.assert_array_equal(new[rows_d], base[rows_d])
        # and earlier rows (whose windows end before i-7) are unaffected as well
        np.testing.assert_array_equal(new[day < i], base[day < i])


def test_nonpositive_and_nan_actuals_do_not_count():
    df = _frame(rows_per_day=2)
    df.loc[df.index % 2 == 0, "actual_time"] = np.nan
    out = rl.recent_level_for_rows(df["park_date"], df["actual_time"], 28, 7, 50)
    # one valid row per day -> 28 per window < 50 -> all NaN
    assert np.isnan(out).all()


def _write_training(tmp_path, df):
    d = tmp_path / "train"
    d.mkdir()
    df.to_parquet(d / "AK.parquet", index=False)
    return d


def test_forecast_loader_uses_exactly_window_before_as_of(tmp_path):
    df = _frame(rows_per_day=2)
    T = START + timedelta(days=100)
    d = _write_training(tmp_path, df)
    got = rl.load_recent_levels(d, T, 28, 50)
    expect = np.mean([10.0 + j * 1.5 for j in range(100 - 28, 100)])
    assert got["AK85"] == pytest.approx(expect)

    mutated = df.copy()
    day = (pd.to_datetime(mutated["park_date"]) - pd.Timestamp(START)).dt.days
    mutated.loc[day >= 100, "actual_time"] = 9999.0
    d2 = tmp_path / "train2"
    d2.mkdir()
    mutated.to_parquet(d2 / "AK.parquet", index=False)
    assert rl.load_recent_levels(d2, T, 28, 50)["AK85"] == pytest.approx(expect)
    # day T-29 is outside the window
    mutated.loc[day == 71, "actual_time"] = 9999.0
    mutated.to_parquet(d2 / "AK.parquet", index=False)
    assert rl.load_recent_levels(d2, T, 28, 50)["AK85"] == pytest.approx(expect)


def test_forecast_loader_min_rows_and_s08_wrapper(tmp_path):
    df = _frame(rows_per_day=1)  # 28 rows in window < 50
    cfg = PipelineConfig(output_base=tmp_path / "base")
    d = _write_training(tmp_path, df)
    assert rl.load_recent_levels(d, START + timedelta(days=100), 28, 50) == {}
    assert rl.load_recent_levels(d, START + timedelta(days=100), 28, 20)  # enough rows at lower floor
    assert s08_forecast._load_recent_levels(cfg, PipelineLogger("t"), START + timedelta(days=100)) == {}
    cfg_off = PipelineConfig(output_base=tmp_path / "base", recent_level_enabled=False)
    assert s08_forecast._load_recent_levels(cfg_off, PipelineLogger("t"), START) == {}


def test_baseline_matrix_keeps_nan_and_fills_old_features():
    df = _frame(rows_per_day=1)  # always < min_rows -> NaN
    df.loc[3, "season_encoded"] = np.nan
    m = s07_training.baseline_matrix(df)
    assert m[rl.FEATURE].isna().all()
    assert m["season_encoded"].iloc[3] == 0
    assert not m[s07_training.BASELINE_FEATURES_V6].isna().any().any()
    off = s07_training.baseline_matrix(df, PipelineConfig(recent_level_enabled=False))
    assert rl.FEATURE not in off.columns


def _cfg(tmp_path, **kw):
    return PipelineConfig(output_base=tmp_path / "base", training_rounds=20,
                          training_early_stopping=5, **kw)


def _grid(entity_days=3):
    rows = []
    for i in range(entity_days):
        for k in range(4):
            rows.append({"park_date": START + timedelta(days=N_DAYS + i), "time_slot": dtime(10 + k, 0),
                         "time_slot_15min": 40 + k, "hour_of_day": 10 + k, "mins_since_6am": 240 + 60 * k,
                         "date_group_id": "DG_A", "date_group_id_encoded": 0,
                         "season_encoded": 1, "season_year_encoded": 2})
    return pd.DataFrame(rows)


def _forecast(cfg, grid, levels=None):
    return s08_forecast._forecast_entity_fast("AK85", "AK", grid, cfg.models_dir, {}, {},
                                              {"__global__": 0.7}, None, recent_levels=levels)


def test_end_to_end_seven_features_and_forecast(tmp_path):
    cfg = _cfg(tmp_path)
    df = _frame(rows_per_day=4)
    res = s07_training._train_baseline_model("AK85", df, cfg, 100, as_of=START + timedelta(days=N_DAYS))
    assert res is not None
    meta = json.loads((cfg.models_dir / "AK85" / "metadata_baseline.json").read_text())
    assert meta["features"] == s07_training.BASELINE_FEATURES
    assert meta["features"][-1] == "recent_level_28d" and len(meta["features"]) == 7
    out, key = _forecast(cfg, _grid(), {"AK85": 120.0})
    assert key == "baseline_7feat" and len(out) == 12
    low, _ = _forecast(cfg, _grid(), {"AK85": 5.0})
    nan, _ = _forecast(cfg, _grid(), None)  # NaN level still predicts
    assert len(nan) == 12
    assert (out["predicted_actual"] != low["predicted_actual"]).any()


def test_recent_level_disabled_trains_old_six(tmp_path):
    cfg = _cfg(tmp_path, recent_level_enabled=False)
    df = _frame(rows_per_day=4)
    assert s07_training._train_baseline_model("AK85", df, cfg, 100, as_of=START + timedelta(days=N_DAYS))
    meta = json.loads((cfg.models_dir / "AK85" / "metadata_baseline.json").read_text())
    assert meta["features"] == s07_training.BASELINE_FEATURES_V6 and len(meta["features"]) == 6
    out, key = _forecast(cfg, _grid(), {"AK85": 120.0})
    assert key == "baseline_6feat" and len(out) == 12


def test_six_feature_model_with_six_feature_metadata_still_forecasts(tmp_path):
    import xgboost as xgb
    cfg = _cfg(tmp_path)
    df = s07_training.baseline_matrix(_frame(rows_per_day=4), PipelineConfig(recent_level_enabled=False))
    X = df[s07_training.BASELINE_FEATURES_V6].values.astype(np.float32)
    bst = xgb.train({"verbosity": 0}, xgb.DMatrix(X, label=df["actual_time"].values.astype(np.float32)), 5)
    mdir = cfg.models_dir / "AK85"
    mdir.mkdir(parents=True)
    bst.save_model(str(mdir / "model_baseline.json"))
    (mdir / "metadata_baseline.json").write_text(json.dumps({"features": s07_training.BASELINE_FEATURES_V6}))
    out, key = _forecast(cfg, _grid(), {"AK85": 50.0})
    assert key == "baseline_6feat" and len(out) == 12
    # No metadata at all: assume the old 6, never 7 columns into a 6-feature model
    (mdir / "metadata_baseline.json").unlink()
    out, key = _forecast(cfg, _grid(), {"AK85": 50.0})
    assert key == "baseline_default" and len(out) == 12
