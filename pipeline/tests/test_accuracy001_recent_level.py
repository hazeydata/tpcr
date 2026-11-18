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
    got = rl.load_recent_levels(d, T, 28, 50, kind="mean")
    expect = np.mean([10.0 + j * 1.5 for j in range(100 - 28, 100)])
    assert got["AK85"] == pytest.approx(expect)

    mutated = df.copy()
    day = (pd.to_datetime(mutated["park_date"]) - pd.Timestamp(START)).dt.days
    mutated.loc[day >= 100, "actual_time"] = 9999.0
    d2 = tmp_path / "train2"
    d2.mkdir()
    mutated.to_parquet(d2 / "AK.parquet", index=False)
    assert rl.load_recent_levels(d2, T, 28, 50, kind="mean")["AK85"] == pytest.approx(expect)
    # day T-29 is outside the window
    mutated.loc[day == 71, "actual_time"] = 9999.0
    mutated.to_parquet(d2 / "AK.parquet", index=False)
    assert rl.load_recent_levels(d2, T, 28, 50, kind="mean")["AK85"] == pytest.approx(expect)


def test_forecast_loader_min_rows_and_s08_wrapper(tmp_path):
    df = _frame(rows_per_day=1)  # 28 rows in window < 50
    cfg = PipelineConfig(output_base=tmp_path / "base")
    d = _write_training(tmp_path, df)
    assert rl.load_recent_levels(d, START + timedelta(days=100), 28, 50, kind="mean") == {}
    assert rl.load_recent_levels(d, START + timedelta(days=100), 28, 20, kind="mean")  # enough rows at lower floor
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
    cfg = _cfg(tmp_path, recent_level_kind="mean")
    df = _frame(rows_per_day=4)
    res = s07_training._train_baseline_model("AK85", df, cfg, 100, as_of=START + timedelta(days=N_DAYS))
    assert res is not None
    meta = json.loads((cfg.models_dir / "AK85" / "metadata_baseline.json").read_text())
    assert meta["features"] == s07_training.BASELINE_FEATURES
    assert meta["features"][-1] == "recent_level_28d" and len(meta["features"]) == 7
    assert meta["recent_level_kind"] == "mean" and meta["recent_level_window_days"] == 28
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


def test_forecast_values_horizon_cap():
    T = START + timedelta(days=N_DAYS)
    days = [T - timedelta(days=1), T, T + timedelta(days=14), T + timedelta(days=15), T + timedelta(days=700)]
    out = rl.forecast_values(days, 42.0, T, 14)
    assert np.isnan(out[0]) and out[1] == 42.0 and out[2] == 42.0
    assert np.isnan(out[3]) and np.isnan(out[4])
    assert np.isnan(rl.forecast_values(days, float("nan"), T, 14)).all()
    assert PipelineConfig().recent_level_max_horizon_days == 14


def test_s08_feeds_level_only_within_horizon(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    T = START + timedelta(days=N_DAYS)
    assert s07_training._train_baseline_model("AK85", _frame(rows_per_day=4), cfg, 100, as_of=T)
    grid = _grid(entity_days=1)
    g14 = grid.assign(park_date=T + timedelta(days=14))
    g15 = grid.assign(park_date=T + timedelta(days=15))
    grid = pd.concat([g14, g15], ignore_index=True)

    seen = []
    real_predict = s08_forecast.xgb.XGBRegressor.predict

    def capture(self, X, *a, **k):
        seen.append(np.array(X, copy=True))
        return real_predict(self, X, *a, **k)

    monkeypatch.setattr(s08_forecast.xgb.XGBRegressor, "predict", capture)
    out, key = s08_forecast._forecast_entity_fast(
        "AK85", "AK", grid, cfg.models_dir, {}, {}, {"__global__": 0.7}, None,
        recent_levels={"AK85": 77.0}, as_of=T, recent_level_max_horizon_days=14)
    assert key == "baseline_7feat" and len(out) == 8
    col = seen[-1][:, -1]  # recent_level_28d is the last feature
    np.testing.assert_array_equal(col[:4], np.float32(77.0))  # as_of + 14 -> level
    assert np.isnan(col[4:]).all()                            # as_of + 15 -> NaN
    # as_of omitted: defaults to the grid's first date (T+14) -> both days within 14
    s08_forecast._forecast_entity_fast(
        "AK85", "AK", grid, cfg.models_dir, {}, {}, {"__global__": 0.7}, None,
        recent_levels={"AK85": 77.0})
    np.testing.assert_array_equal(seen[-1][:, -1], np.float32(77.0))


def test_s08_loader_failure_is_fail_soft(tmp_path, monkeypatch):
    cfg = PipelineConfig(output_base=tmp_path / "base")
    log = PipelineLogger("t")
    warnings, metrics = [], {}
    monkeypatch.setattr(log, "warning", lambda msg, **k: warnings.append(msg))
    monkeypatch.setattr(log, "metric", lambda name, value, **k: metrics.__setitem__(name, value))

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(rl, "load_recent_levels", boom)
    assert s08_forecast._load_recent_levels(cfg, log, START) == {}
    assert len(warnings) == 1 and "OSError" in warnings[0] and "disk gone" in warnings[0]
    assert metrics.get("recent_level_load_failed") == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_nullable_int64_feature_columns_train_and_forecast(tmp_path, enabled):
    """s06b parquets can hold pandas nullable Int64 (with pd.NA) for mins_since_open."""
    cfg = _cfg(tmp_path, recent_level_enabled=enabled)
    df = _frame(rows_per_day=4)
    msopen = pd.array(df["mins_since_open"].tolist(), dtype="Int64")
    msopen[::7] = pd.NA
    df["mins_since_open"] = msopen
    df["season_year_encoded"] = pd.array(df["season_year_encoded"].tolist(), dtype="Int64")
    df["actual_time"] = pd.array(df["actual_time"].tolist(), dtype="Float64")
    df.loc[5, "actual_time"] = pd.NA
    assert df["mins_since_open"].isna().any()

    m = s07_training.baseline_matrix(df, cfg)
    assert all(m[c].dtype == np.float64 for c in s07_training.BASELINE_FEATURES_V6)
    assert m["mins_since_open"].iloc[0] == 0 and np.isnan(m["actual_time"].iloc[5])

    res = s07_training._train_baseline_model("AK85", df, cfg, 100, as_of=START + timedelta(days=N_DAYS))
    assert res is not None
    meta = json.loads((cfg.models_dir / "AK85" / "metadata_baseline.json").read_text())
    assert len(meta["features"]) == (7 if enabled else 6)

    grid = _grid()
    grid["season_year_encoded"] = pd.array(grid["season_year_encoded"].tolist(), dtype="Int64")
    out, key = _forecast(cfg, grid, {"AK85": 1.0})
    assert key == f"baseline_{7 if enabled else 6}feat" and len(out) == 12


# --- recent_level_kind = "yoy_ratio" (default) ------------------------------------------

YOY_DAYS = 500


def _long(rows_per_day=2, value=lambda i: 10.0 + i * 0.5, entity="AK85"):
    recs = []
    for i in range(YOY_DAYS):
        for k in range(rows_per_day):
            recs.append({"entity_code": entity, "park_date": START + timedelta(days=i),
                         "actual_time": value(i) + k, "mins_since_6am": 60 * (k + 3),
                         "mins_since_open": 60 * k, "date_group_id_encoded": i % 3,
                         "season_encoded": 1, "season_year_encoded": 2,
                         "is_synthetic": bool(k % 2)})
    return pd.DataFrame(recs)


def _window_mean(df, lo, hi, min_rows=50):
    """Reference M(lo, hi) on day offsets from START."""
    day = (pd.to_datetime(df["park_date"]) - pd.Timestamp(START)).dt.days
    y = df["actual_time"]
    m = (day >= lo) & (day < hi) & y.notna() & (y > 0)
    return float(y[m].mean()) if m.sum() >= min_rows else float("nan")


def test_config_defaults_and_kind_validation():
    cfg = PipelineConfig()
    assert cfg.recent_level_kind == "yoy_ratio" and cfg.recent_level_yoy_offset_days == 364
    assert rl.kind(cfg) == "yoy_ratio" and rl.kind(None) == "yoy_ratio"
    assert rl.kind(PipelineConfig(recent_level_kind="mean")) == "mean"
    with pytest.raises(ValueError):
        rl.kind(PipelineConfig(recent_level_kind="median"))
    with pytest.raises(ValueError):
        rl.load_recent_levels(".", START, kind="median")


def test_yoy_training_value_is_exact_ratio_of_both_windows():
    df = _long()
    out = rl.feature_for_rows(df["park_date"], df["actual_time"], PipelineConfig())
    day = (pd.to_datetime(df["park_date"]) - pd.Timestamp(START)).dt.days.to_numpy()
    checked = 0
    for i in (380, 390, 400, 420, 450, 499):
        num = _window_mean(df, i - 35, i - 7)
        den = _window_mean(df, i - 35 - 364, i - 7 - 364)
        got = out[day == i]
        if np.isnan(num) or np.isnan(den):
            assert np.isnan(got).all()
        else:
            np.testing.assert_allclose(got, num / den)
            checked += 1
    assert checked >= 3
    # i=380: denominator window [-19, 9) has 9 days * 2 rows = 18 < 50 -> NaN
    assert np.isnan(out[day == 380]).all()
    # kind "mean" on the same rows is the numerator alone
    mean = rl.feature_for_rows(df["park_date"], df["actual_time"], PipelineConfig(recent_level_kind="mean"))
    np.testing.assert_allclose(mean[day == 450], _window_mean(df, 450 - 35, 450 - 7))


@pytest.mark.parametrize("kind", ["yoy_ratio", "mean"])
def test_training_value_unchanged_when_days_at_or_after_d_minus_lag_mutate(kind):
    cfg = PipelineConfig(recent_level_kind=kind)
    df = _long()
    base = rl.feature_for_rows(df["park_date"], df["actual_time"], cfg)
    day = (pd.to_datetime(df["park_date"]) - pd.Timestamp(START)).dt.days.to_numpy()
    for i in (420, 460, 499):
        assert np.isfinite(base[day == i]).all()
        mutated = df["actual_time"].to_numpy().copy()
        mutated[day >= i - 7] *= 50.0
        new = rl.feature_for_rows(df["park_date"], mutated, cfg)
        np.testing.assert_array_equal(new[day == i], base[day == i])
        np.testing.assert_array_equal(new[day < i], base[day < i])


@pytest.mark.parametrize("side", ["numerator", "denominator"])
def test_yoy_min_rows_on_either_side_gives_nan(side):
    df = _long()
    i = 450
    lo, hi = (i - 35, i - 7) if side == "numerator" else (i - 35 - 364, i - 7 - 364)
    day = (pd.to_datetime(df["park_date"]) - pd.Timestamp(START)).dt.days
    # keep only 1 row per day in that window -> 28 < 50 qualifying rows
    df.loc[(day >= lo) & (day < hi) & (df.index % 2 == 0), "actual_time"] = np.nan
    out = rl.feature_for_rows(df["park_date"], df["actual_time"], PipelineConfig())
    assert np.isnan(out[(day == i).to_numpy()]).all()
    # forecast issued on i-7 reads exactly the same two windows
    as_of = START + timedelta(days=i - 7)
    assert np.isnan(rl.feature_at(df["park_date"], df["actual_time"], as_of, PipelineConfig()))


def test_yoy_denominator_zero_or_negative_gives_nan(monkeypatch):
    assert np.isnan(rl._ratio(5.0, 0.0)) and np.isnan(rl._ratio(5.0, -1.0))
    assert np.isnan(rl._ratio(np.nan, 2.0)) and np.isnan(rl._ratio(2.0, np.nan))
    assert rl._ratio(6.0, 2.0) == 3.0
    df = _long()
    real_rows, real_at = rl.recent_level_for_rows, rl.recent_level_at
    # denominator window (lag >= offset) returns 0
    monkeypatch.setattr(rl, "recent_level_for_rows",
                        lambda pd_, y, w, lag, m: np.zeros(len(pd_)) if lag >= 364 else real_rows(pd_, y, w, lag, m))
    assert np.isnan(rl.feature_for_rows(df["park_date"], df["actual_time"], PipelineConfig())).all()
    T = START + timedelta(days=450)
    monkeypatch.setattr(rl, "recent_level_at",
                        lambda pd_, y, a, w, m: 0.0 if a < T - timedelta(days=300) else real_at(pd_, y, a, w, m))
    assert np.isnan(rl.feature_at(df["park_date"], df["actual_time"], T, PipelineConfig()))


@pytest.mark.parametrize("kind", ["yoy_ratio", "mean"])
def test_forecast_ignores_rows_on_or_after_as_of(tmp_path, kind):
    cfg = PipelineConfig(recent_level_kind=kind)
    df = _long()
    T = START + timedelta(days=460)
    if kind == "yoy_ratio":
        expect = _window_mean(df, 460 - 28, 460) / _window_mean(df, 460 - 28 - 364, 460 - 364)
    else:
        expect = _window_mean(df, 460 - 28, 460)
    assert rl.feature_at(df["park_date"], df["actual_time"], T, cfg) == pytest.approx(expect)
    d = _write_training(tmp_path, df)
    assert rl.load_recent_levels(d, T, 28, 50, kind=kind)["AK85"] == pytest.approx(expect)

    day = (pd.to_datetime(df["park_date"]) - pd.Timestamp(START)).dt.days
    mutated = df.copy()
    mutated.loc[day >= 460, "actual_time"] = 9999.0
    assert rl.feature_at(mutated["park_date"], mutated["actual_time"], T, cfg) == pytest.approx(expect)
    # rows outside both windows (not just >= as_of) are never read either
    in_num = (day >= 460 - 28) & (day < 460)
    in_den = (day >= 460 - 28 - 364) & (day < 460 - 364)
    read = (in_num | in_den) if kind == "yoy_ratio" else in_num
    mutated.loc[~read, "actual_time"] = 9999.0
    d2 = tmp_path / "train2"
    d2.mkdir()
    mutated.to_parquet(d2 / "AK.parquet", index=False)
    assert rl.load_recent_levels(d2, T, 28, 50, kind=kind)["AK85"] == pytest.approx(expect)


def test_yoy_loader_min_rows_either_side_and_s08_wrapper(tmp_path):
    df = _long()
    T = START + timedelta(days=460)
    day = (pd.to_datetime(df["park_date"]) - pd.Timestamp(START)).dt.days
    sparse_den = df[~(((day >= 460 - 28 - 364) & (day < 460 - 364)) & (df.index % 2 == 0))]
    d = _write_training(tmp_path, sparse_den)
    assert rl.load_recent_levels(d, T, 28, 50, kind="yoy_ratio") == {}
    assert "AK85" in rl.load_recent_levels(d, T, 28, 50, kind="mean")  # numerator side is full
    cfg = PipelineConfig(output_base=tmp_path / "base")
    tdir = s08_forecast.paths.training_data_dir(cfg)
    tdir.mkdir(parents=True)
    df.to_parquet(tdir / "AK.parquet", index=False)
    got = s08_forecast._load_recent_levels(cfg, PipelineLogger("t"), T)
    assert got["AK85"] == pytest.approx(rl.yoy_ratio_at(df["park_date"], df["actual_time"], T, 28, 50, 364))


def test_yoy_end_to_end_metadata_and_spec_mismatch(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    T = START + timedelta(days=YOY_DAYS)
    assert s07_training._train_baseline_model("AK85", _long(rows_per_day=4), cfg, 100, as_of=T)
    meta_path = cfg.models_dir / "AK85" / "metadata_baseline.json"
    meta = json.loads(meta_path.read_text())
    assert meta["features"][-1] == "recent_level_28d"
    assert {k: meta[k] for k in rl.spec(cfg)} == {
        "recent_level_kind": "yoy_ratio", "recent_level_window_days": 28, "recent_level_lag_days": 7,
        "recent_level_min_rows": 50, "recent_level_yoy_offset_days": 364}

    seen = []
    real_predict = s08_forecast.xgb.XGBRegressor.predict

    def capture(self, X, *a, **k):
        seen.append(np.array(X, copy=True))
        return real_predict(self, X, *a, **k)

    monkeypatch.setattr(s08_forecast.xgb.XGBRegressor, "predict", capture)
    grid = _grid(entity_days=1).assign(park_date=T)
    stats = {}
    args = ("AK85", "AK", grid, cfg.models_dir, {}, {}, {"__global__": 0.7}, None)
    out, key = s08_forecast._forecast_entity_fast(*args, recent_levels={"AK85": 1.1}, as_of=T,
                                                  recent_level_spec=rl.spec(cfg), stats=stats)
    assert key == "baseline_7feat" and stats.get("recent_level_spec_mismatch", 0) == 0
    np.testing.assert_array_equal(seen[-1][:, -1], np.float32(1.1))
    # a model trained on the raw mean (metadata without recent_level_kind) gets NaN under yoy
    for k in rl.spec(cfg):
        meta.pop(k)
    meta_path.write_text(json.dumps(meta))
    out, key = s08_forecast._forecast_entity_fast(*args, recent_levels={"AK85": 1.1}, as_of=T,
                                                  recent_level_spec=rl.spec(cfg), stats=stats)
    assert len(out) == 4 and stats["recent_level_spec_mismatch"] == 1
    assert np.isnan(seen[-1][:, -1]).all()
    # ... and its level under kind "mean" with the same metadata
    out, key = s08_forecast._forecast_entity_fast(
        *args, recent_levels={"AK85": 30.0}, as_of=T,
        recent_level_spec=rl.spec(PipelineConfig(recent_level_kind="mean")), stats=stats)
    assert stats["recent_level_spec_mismatch"] == 1
    np.testing.assert_array_equal(seen[-1][:, -1], np.float32(30.0))
