"""dashboard/api.py reads model_aggregates.time_slot as a 15-min clock slot (TPCR-S4-AGG-001 PR 2)."""

from datetime import date

import pandas as pd
import pytest

from dashboard import api


@pytest.fixture
def agg(tmp_path, monkeypatch):
    p = tmp_path / "model_aggregates.parquet"
    pd.DataFrame({
        "entity_code": ["MK01", "MK01", "MK01", "MK02", "EP01"],
        "date_group_id": ["DGX"] * 5,
        "time_slot": pd.array([0, 36, 42, 42, 42], dtype="int32"),
        "wait_mean_weighted": [5.0, 30.0, 50.0, 70.0, 99.0],
    }).to_parquet(p, index=False)
    monkeypatch.setattr(api, "MODEL_AGG_PARQUET", p)
    monkeypatch.setitem(api.DATE_GROUP_MAP, "2026-10-24", "DGX")
    # Opening time must not shift the curve: make it something odd.
    monkeypatch.setattr(api, "_get_park_hours", lambda *a, **k: ("08:00", "23:00", None))
    return p


def test_entity_curve_uses_clock_slots(agg):
    got = api._curve_from_model_aggregates("MK", date(2026, 10, 24), "mk01")
    assert got == [
        {"time_slot": "00:00", "avg_wait": 5.0},
        {"time_slot": "09:00", "avg_wait": 30.0},
        {"time_slot": "10:30", "avg_wait": 50.0},
    ]


def test_park_curve_averages_entities_per_clock_slot(agg):
    got = api._curve_from_model_aggregates("MK", date(2026, 10, 24))
    assert {"time_slot": "10:30", "avg_wait": 60.0} in got
    assert all(r["time_slot"] != "11:30" for r in got)  # old reading: 08:00 + 42*5 min


def test_slot_bounds():
    assert api._agg_slot_to_hhmm(95) == "23:45"
    with pytest.raises(ValueError):
        api._agg_slot_to_hhmm(96)
