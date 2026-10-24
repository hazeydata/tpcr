"""TPCR-S8-FALLBACK-001 phase 1: S7/S8 eligibility, skip logging, failed semantics."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pipeline.config import PipelineConfig
from pipeline.core.eligibility import (
    SkipLog,
    is_operating_in_window,
    load_dimentity_extinct,
    warn_pre2005_extinct_on,
)
from pipeline.core.logging import PipelineLogger
from pipeline.core.validation import StageFailed, ValidationError
from pipeline.steps import s07_training, s08_forecast

TODAY = date.today()
BASE_REF = "origin/backup/mini-live-b4e6edd"


def _cfg(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        output_base=tmp_path / "pipeline",
        forecast_days=30,
        training_rounds=3,
        training_early_stopping=2,
    )


def _actuals(entity: str, start: date, n: int, valid: bool = True) -> pd.DataFrame:
    dates = [start + timedelta(days=i) for i in range(n)]
    rng = np.random.default_rng(0)
    return pd.DataFrame({
        "entity_code": entity,
        "park_date": pd.to_datetime(dates),
        "actual_time": rng.uniform(5, 60, n) if valid else np.full(n, np.nan),
        "mins_since_6am": rng.integers(0, 600, n),
        "mins_since_open": rng.integers(0, 600, n),
        "date_group_id_encoded": rng.integers(0, 5, n),
        "season_encoded": rng.integers(0, 3, n),
        "season_year_encoded": rng.integers(0, 3, n),
    })


def _write_dim(cfg: PipelineConfig, rows: dict[str, str]) -> None:
    cfg.dimension_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.dimension_dir / "dimentity.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "extinct_on"])
        for code, ext in rows.items():
            w.writerow([code, ext])


def _write_oc(cfg: PipelineConfig, rows: list[tuple[str, date, bool]]) -> None:
    d = cfg.output_base / "operating_calendar"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=["entity_code", "park_date", "is_operating"]).to_parquet(
        d / "operating_calendar.parquet", index=False
    )


def _s7_fixture(tmp_path: Path) -> PipelineConfig:
    cfg = _cfg(tmp_path)
    _write_dim(cfg, {
        "MK01": "", "MK02": "2019-01-01", "MK03": "", "MK04": "", "MK06": "", "BB01": "",
    })
    _write_oc(cfg, [
        ("MK01", TODAY + timedelta(days=1), True),
        ("MK03", TODAY - timedelta(days=400), True),   # operated, but not in window
    ])
    actuals = cfg.output_base / "matched_pairs" / "actuals_training_v2"
    actuals.mkdir(parents=True)
    mk = pd.concat([
        _actuals("MK01", date(2020, 1, 1), 300),
        _actuals("MK02", date(2020, 1, 1), 300),
        _actuals("MK03", date(2020, 1, 1), 300),
        # 300 rows pre-2016 cutoff + 10 after: below min_obs only once cutoff applies
        _actuals("MK04", date(2014, 1, 1), 300),
        _actuals("MK04", date(2020, 1, 1), 10),
        _actuals("MK06", date(2020, 1, 1), 300, valid=False),
    ])
    mk.to_parquet(actuals / "MK.parquet", index=False)
    _actuals("BB01", date(2020, 1, 1), 300).to_parquet(actuals / "BB.parquet", index=False)
    return cfg


def _skip_lines(log: PipelineLogger) -> list[str]:
    return [e["message"] for e in log.events if e["message"].startswith("SKIP ")]


def test_s7_eligibility_and_skip_log(tmp_path):
    cfg = _s7_fixture(tmp_path)
    log = PipelineLogger("t7")
    res = s07_training.run(cfg, log)

    # AC1/AC2: retired ride, not-operating ride and cutoff-year ride are excluded, none "failed"
    lines = _skip_lines(log)
    assert "SKIP stage=s07 entity=MK02 reason=extinct" in lines
    assert "SKIP stage=s07 entity=MK03 reason=not_operating" in lines
    assert "SKIP stage=s07 entity=MK04 reason=below_min_obs" in lines
    assert "SKIP stage=s07 entity=MK06 reason=no_training_data" in lines
    assert not any("BB01" in l for l in lines)  # ignored park is out of scope
    assert res["failed"] == 0
    assert res["eligible"] == 2  # MK01, MK06
    assert res["successful"] == 1
    assert (cfg.models_dir / "MK01" / "model_baseline.json").exists()

    # AC3: per-reason counts and the invariant
    assert res["skipped"] == {
        "extinct": 1, "not_operating": 1, "below_min_obs": 1, "no_training_data": 1,
    }
    post = res["skipped"]["no_training_data"]
    assert post == res["eligible"] - res["successful"]
    assert "SKIP_COUNT stage=s07 reason=extinct count=1" in [e["message"] for e in log.events]
    with open(cfg.logs_dir / f"skips_s07_{TODAY.isoformat()}.csv") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["stage", "entity_code", "reason"]
    assert ["s07", "MK02", "extinct"] in rows


def test_s7_injected_error_warns_and_continues(tmp_path, monkeypatch):
    cfg = _s7_fixture(tmp_path)
    real = s07_training._train_baseline_model

    def boom(entity_code, *a, **k):
        if entity_code == "MK01":
            raise RuntimeError("injected")
        return real(entity_code, *a, **k)

    monkeypatch.setattr(s07_training, "_train_baseline_model", boom)
    log = PipelineLogger("t7e")
    res = s07_training.run(cfg, log)  # must not raise
    assert res["failed"] == 1
    assert "MK01" in res["failed_entities"]
    msgs = [e["message"] for e in log.events]
    assert "SKIP stage=s07 entity=MK01 reason=error:RuntimeError" in msgs
    assert any(
        e["level"] == "error" and "S7 TRAINING ERRORS: 1 entities failed" in e["message"]
        for e in log.events
    )
    assert any(
        e["level"] == "error" and "MK01" in e["message"] and "RuntimeError" in e["message"]
        for e in log.events
    )
    assert "SKIP_COUNT stage=s07 reason=error:RuntimeError count=1" in msgs
    assert (cfg.logs_dir / f"skips_s07_{TODAY.isoformat()}.csv").exists()


# --- S8 ---------------------------------------------------------------------

def _s8_setup(cfg, monkeypatch, entities, extra_boom=None):
    monkeypatch.setattr(s08_forecast, "_load_date_features", lambda c, l: {})
    monkeypatch.setattr(s08_forecast, "_load_park_hours", lambda c, l: {})
    monkeypatch.setattr(s08_forecast, "_load_aggregates_indexed", lambda c, l: {})
    monkeypatch.setattr(s08_forecast, "_load_entity_list", lambda c, l: entities)
    monkeypatch.setattr(s08_forecast, "_load_fallback_ratios", lambda c, l: {"__global__": 0.678})
    # The real loader drops zero-operating-day entities, so inject an explicit empty set
    # (the "entity in calendar with zero dates" path).
    oc = {
        "MK01": {TODAY}, "MK03": set(), "MK05": {TODAY},
        "MK04": {TODAY - timedelta(days=400)},
    }
    monkeypatch.setattr(s08_forecast, "_load_operating_calendar_indexed", lambda c, l: oc)
    real = s08_forecast._forecast_entity_fast

    def wrapped(entity_code, *a, **k):
        if entity_code == extra_boom:
            raise KeyError("injected")
        return real(entity_code, *a, **k)

    monkeypatch.setattr(s08_forecast, "_forecast_entity_fast", wrapped)


def _s8_fixture(tmp_path):
    cfg = _cfg(tmp_path)
    _write_dim(cfg, {"MK01": "", "MK02": "2019-01-01", "MK03": "", "MK04": "", "MK05": ""})
    _write_oc(cfg, [
        ("MK01", TODAY, True),
        ("MK04", TODAY - timedelta(days=400), True),  # no dates in window
        ("MK05", TODAY, True),
    ])
    return cfg


def test_s8_skip_counts_sum_to_input_minus_output(tmp_path, monkeypatch):
    cfg = _s8_fixture(tmp_path)
    ents = ["MK01", "MK02", "MK03", "MK04"]
    _s8_setup(cfg, monkeypatch, ents)
    log = PipelineLogger("t8")
    res = s08_forecast.run(cfg, log)
    # Skip reasons come only from the operating calendar / time grid: MK03 has an empty
    # OC set (extinct), MK04 has no dates in the window (not_operating). MK02 is not in
    # the OC at all, so it is assumed operating and forecast (dimentity is not consulted).
    assert res["failed"] == 0
    assert res["entities"] == 2
    assert res["skipped"] == {"extinct": 1, "not_operating": 1}
    assert sum(res["skipped"].values()) == len(ents) - res["entities"]
    lines = _skip_lines(log)
    assert "SKIP stage=s08 entity=MK03 reason=extinct" in lines
    assert "SKIP stage=s08 entity=MK04 reason=not_operating" in lines
    assert not any("MK02" in l for l in lines)


def _load_base_s08(tmp_path: Path):
    """Load the base s08_forecast.py (pre-phase-1) from git into a temp module."""
    import importlib.util
    import subprocess

    repo = Path(__file__).resolve().parents[2]
    try:
        src = subprocess.run(
            ["git", "show", f"{BASE_REF}:pipeline/steps/s08_forecast.py"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        pytest.skip(f"git base version unavailable: {e}")
    path = tmp_path / "s08_forecast_base.py"
    path.write_text(src)
    spec = importlib.util.spec_from_file_location("s08_forecast_base", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_s8_output_unchanged_vs_base(tmp_path, monkeypatch):
    xgb = pytest.importorskip("xgboost")
    pytest.importorskip("sklearn")  # XGBRegressor (used by S8 model loading) requires it

    base = _load_base_s08(tmp_path)
    cfg = _cfg(tmp_path)

    # One tiny trained baseline model (MK01)
    rng = np.random.default_rng(1)
    X = rng.uniform(0, 600, (200, len(s08_forecast.FEATURES_BASELINE))).astype(np.float32)
    y = rng.uniform(5, 90, 200)
    model = xgb.XGBRegressor(n_estimators=5, max_depth=2)
    model.fit(X, y)
    mdir = cfg.models_dir / "MK01"
    mdir.mkdir(parents=True)
    model.save_model(str(mdir / "model_baseline.json"))

    agg = pd.DataFrame({
        "date_group_id": "UNKNOWN",
        "time_slot_15min": np.arange(96),
        "wait_median": np.linspace(10, 60, 96),
    })
    entities = ["MK01", "MK02", "MK03", "MK04", "MK05"]
    oc = {
        "MK01": {TODAY + timedelta(days=i) for i in range(0, 30, 2)},
        "MK03": set(),                                # empty OC set -> skipped
        "MK04": {TODAY - timedelta(days=400)},        # no dates in window -> skipped
        "MK05": {TODAY},
    }                                                 # MK02 absent from OC -> fallback_ratio
    hours = {("MK", TODAY): (9 * 60, 22 * 60)}

    def patch(mod):
        monkeypatch.setattr(mod, "_load_date_features", lambda c, l: {})
        monkeypatch.setattr(mod, "_load_park_hours", lambda c, l: hours)
        monkeypatch.setattr(mod, "_load_aggregates_indexed", lambda c, l: {"MK01": agg, "MK02": agg})
        monkeypatch.setattr(mod, "_load_entity_list", lambda c, l: entities)
        monkeypatch.setattr(mod, "_load_fallback_ratios", lambda c, l: {"__global__": 0.678, "MK05": 0.5})
        monkeypatch.setattr(mod, "_load_operating_calendar_indexed", lambda c, l: oc)

    out = cfg.forecast_dir / "all_forecasts.parquet"
    frames = {}
    for name, mod in (("current", s08_forecast), ("base", base)):
        patch(mod)
        mod.run(cfg, PipelineLogger(f"t8_{name}"))
        frames[name] = pd.read_parquet(out)
        out.unlink()

    assert set(frames["current"]["prediction_method"]) == {"model_baseline", "fallback_ratio"}
    assert set(frames["current"]["entity_code"]) == {"MK01", "MK02", "MK05"}
    cols = list(frames["base"].columns)
    srt = lambda d: d.sort_values(cols).reset_index(drop=True)  # noqa: E731
    pd.testing.assert_frame_equal(srt(frames["current"]), srt(frames["base"]))


def test_s8_error_counts_failed_and_raises(tmp_path, monkeypatch):
    cfg = _s8_fixture(tmp_path)
    _s8_setup(cfg, monkeypatch, ["MK01", "MK05"], extra_boom="MK05")
    log = PipelineLogger("t8e")
    with pytest.raises(StageFailed):
        s08_forecast.run(cfg, log)
    assert "SKIP stage=s08 entity=MK05 reason=error:KeyError" in _skip_lines(log)
    # outputs were written before the raise
    assert (cfg.forecast_dir / "all_forecasts.parquet").exists()


# --- eligibility helpers / pre-2005 extinct_on ------------------------------

PRE2005 = {
    "CA11": "2000-01-01", "CA18": "2000-01-01", "DL83": "2000-01-01", "DL84": "2000-01-01",
    "UH12": "2000-01-01", "EP58": "2000-01-01", "EP78": "2000-01-01", "HS09": "2000-01-01",
    "HS29": "2000-01-01",
    "CA32": "2003-06-15", "DL82": "2004-08-09", "MK81": "2004-10-01",
}


def test_warn_pre2005_extinct_on_never_raises(tmp_path):
    dim = tmp_path / "dimentity.csv"
    dim.write_text("code,extinct_on\nHS29,2000-01-01\nCA32,2003-06-15\nMK01,2019-05-01\nMK02,\n")
    m = load_dimentity_extinct(dim)
    assert m["HS29"] == date(2000, 1, 1)
    assert m["MK01"] == date(2019, 5, 1)
    assert m["MK02"] is None
    log = PipelineLogger("tw")
    found = warn_pre2005_extinct_on(m, log)
    assert found == [("CA32", date(2003, 6, 15)), ("HS29", date(2000, 1, 1))]
    msgs = [e["message"] for e in log.events if e["level"] == "warning"]
    assert "PRE2005_EXTINCT_ON entity=HS29 extinct_on=2000-01-01 treated_as=extinct sentinel=true" in msgs
    assert "PRE2005_EXTINCT_ON entity=CA32 extinct_on=2003-06-15 treated_as=extinct" in msgs


def test_s7_pre2005_extinct_on_production_rows(tmp_path):
    cfg = _cfg(tmp_path)
    normal = ["MK01", "MK03", "MK04"]
    _write_dim(cfg, {**PRE2005, **{c: "" for c in normal}, "MK05": "2019-01-01"})
    actuals = cfg.output_base / "matched_pairs" / "actuals_training_v2"
    actuals.mkdir(parents=True)
    by_park: dict[str, list[pd.DataFrame]] = {}
    for code in [*PRE2005, *normal, "MK05"]:
        by_park.setdefault(code[:2], []).append(_actuals(code, date(2020, 1, 1), 300))
    for park, frames in by_park.items():
        pd.concat(frames).to_parquet(actuals / f"{park}.parquet", index=False)

    log = PipelineLogger("t7p")
    res = s07_training.run(cfg, log)  # must not raise (incl. StageFailed)

    lines = _skip_lines(log)
    for code in PRE2005:
        assert f"SKIP stage=s07 entity={code} reason=extinct" in lines
    assert res["skipped"]["extinct"] == len(PRE2005) + 1  # + MK05 (2019)
    assert res["failed"] == 0
    assert res["eligible"] == len(normal) == res["successful"]
    for code in normal:
        assert (cfg.models_dir / code / "model_baseline.json").exists()

    warns = [e["message"] for e in log.events
             if e["level"] == "warning" and e["message"].startswith("PRE2005_EXTINCT_ON")]
    assert len(warns) == 12
    assert sum("sentinel=true" in w for w in warns) == 9
    msgs = [e["message"] for e in log.events]
    assert "SKIP_NOTE stage=s07 pre2005_extinct_on count=12" in msgs
    assert any(e["level"] == "metric" and e["message"] == "s07_pre2005_extinct_on" and e["value"] == 12
               for e in log.events)
    with open(cfg.logs_dir / f"skips_s07_{TODAY.isoformat()}.csv") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["stage", "entity_code", "reason"]
    assert all(r[2] == "extinct" for r in rows[1:] if r[1] in PRE2005)
