from datetime import date, timedelta

import numpy as np
import pandas as pd

from pipeline.backtest import decay_grid as g
from pipeline.config import PipelineConfig


def _tables(signed, park_mae):
    rides = pd.DataFrame([{"candidate": c, "park": "AK", "entity_code": e, "days": 30,
                           "signed": s, "mae": abs(s) + 5}
                          for c, by in signed.items() for e, s in by.items()])
    parks = pd.DataFrame([{"candidate": c, "park": p, "days": 30, "signed": 0.0, "wti_mae": m}
                          for c, by in park_mae.items() for p, m in by.items()])
    return rides, parks


BASE = g.BASELINE.name


def test_exactly_half_bias_cut_and_half_point_mae_rise_passes():
    rides, parks = _tables({BASE: {"AK85": 36.3, "AK86": 35.8}, "hl180_y2022": {"AK85": 18.15, "AK86": -17.9}},
                           {BASE: {"AK": 10.0, "HS": 8.0}, "hl180_y2022": {"AK": 10.5, "HS": 7.0}})
    v = g.evaluate(rides, parks).set_index("candidate")
    assert bool(v.loc["hl180_y2022", "passes"])
    assert g.recommend(v.reset_index(), parks) == "hl180_y2022"


def test_one_ride_short_of_half_cut_fails():
    rides, parks = _tables({BASE: {"AK85": 36.3, "AK86": 35.8}, "hl365_y2016": {"AK85": 10.0, "AK86": 18.0}},
                           {BASE: {"AK": 10.0}, "hl365_y2016": {"AK": 9.0}})
    v = g.evaluate(rides, parks).set_index("candidate")
    assert not bool(v.loc["hl365_y2016", "passes"])


def test_any_park_worsening_beyond_half_point_fails():
    rides, parks = _tables({BASE: {"AK85": 36.3, "AK86": 35.8}, "hl180_y2016": {"AK85": 1.0, "AK86": 1.0}},
                           {BASE: {"AK": 10.0, "EP": 6.0}, "hl180_y2016": {"AK": 5.0, "EP": 6.51}})
    v = g.evaluate(rides, parks).set_index("candidate")
    assert not bool(v.loc["hl180_y2016", "passes"])
    assert v.loc["hl180_y2016", "worst_park"] == "EP"


def test_baseline_never_passes_and_no_pass_means_no_recommendation():
    rides, parks = _tables({BASE: {"AK85": 36.3, "AK86": 35.8}}, {BASE: {"AK": 10.0}})
    v = g.evaluate(rides, parks)
    assert not v["passes"].any()
    assert g.recommend(v, parks) is None


def test_missing_bias_ride_fails_rather_than_passing():
    rides, parks = _tables({BASE: {"AK85": 36.3, "AK86": 35.8}, "hl365_y2022": {"AK85": 1.0}},
                           {BASE: {"AK": 10.0}, "hl365_y2022": {"AK": 9.0}})
    assert not g.evaluate(rides, parks).set_index("candidate").loc["hl365_y2022", "passes"]


def test_recommend_prefers_lowest_mean_park_mae():
    rides, parks = _tables({BASE: {"AK85": 36.3, "AK86": 35.8}, "hl365_y2016": {"AK85": 1.0, "AK86": 1.0},
                            "hl180_y2016": {"AK85": 1.0, "AK86": 1.0}},
                           {BASE: {"AK": 10.0}, "hl365_y2016": {"AK": 9.0}, "hl180_y2016": {"AK": 8.0}})
    assert g.recommend(g.evaluate(rides, parks), parks) == "hl180_y2016"


def test_grid_is_the_six_approved_configs():
    assert {c.name for c in g.GRID} == {f"hl{h}_y{y}" for h in (730, 365, 180) for y in (2016, 2022)}


def _drifting_park(days_back=900, end=date(2026, 10, 27)):
    rng = np.random.default_rng(0)
    rows = []
    for i in range(days_back, -1, -1):
        d = end - timedelta(days=i)
        level = 70.0 if i > 300 else 30.0
        for h in (10, 14):
            rows.append({"entity_code": "AK86", "park_date": d, "is_synthetic": False,
                         "actual_time": level + rng.normal(0, 1),
                         "mins_since_6am": (h - 6) * 60, "mins_since_open": (h - 8) * 60,
                         "date_group_id_encoded": 0, "season_encoded": 0,
                         "season_year_encoded": 0, "observed_at_ts": pd.Timestamp(d)})
    return pd.DataFrame(rows)


def test_shorter_half_life_reduces_bias_after_a_level_drop():
    cfg = PipelineConfig()
    df = _drifting_park()
    days = g.window(date(2026, 10, 25), date(2026, 10, 27))
    out = pd.concat([g.entity_days(df, c, days, cfg, min_samples=10)
                     for c in (g.BASELINE, g.Candidate(180, 2016))])
    rides, _ = g.summarise(out, lambda e: e[:2])
    signed = rides.set_index("candidate")["signed"]
    assert len(out) == 6
    assert signed[g.BASELINE.name] > signed["hl180_y2016"] > -5


def test_harness_never_trains_on_the_day_it_predicts(monkeypatch):
    seen = []
    real = g.fit_baseline

    def spy(train, cfg, min_samples, as_of, halflife_days):
        seen.append((pd.to_datetime(train["park_date"]).dt.date.max(), as_of))
        return real(train, cfg, min_samples, as_of, halflife_days)

    monkeypatch.setattr(g, "fit_baseline", spy)
    days = [date(2026, 10, 26), date(2026, 10, 27)]
    out = g.entity_days(_drifting_park(days_back=60), g.BASELINE, days, PipelineConfig(), min_samples=10)
    assert len(out) == 2
    assert all(last < as_of for last, as_of in seen)
