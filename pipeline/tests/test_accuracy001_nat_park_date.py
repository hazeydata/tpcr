"""TPCR-ACCURACY-001: park_date "NaT" strings must never crash or leak into training data."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from pipeline.core.encodings import refresh_mappings_file
from pipeline.tests.bias_fixtures import build_base

REPO = Path(__file__).resolve().parents[2]
for sub in ("scripts", "src"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))

from build_actuals_training_data import TrainingInputs, build_actuals_training_data  # noqa: E402
from convert_to_parquet import convert_month_to_parquet  # noqa: E402
from get_tp_wait_time_data_from_s3 import derive_park_date  # noqa: E402


def _base(path: Path) -> Path:
    base = build_base(path)
    enc = TrainingInputs.under(base).encodings_path
    refresh_mappings_file(enc, enc, base / "dimension_tables")  # as s06b does before the build
    return base


def _with_nat_rows(base: Path) -> tuple[int, int]:
    """Rewrite fixture facts/synthetic with VARCHAR park_date (as prod) plus bad rows."""
    facts_path = base / "fact_tables" / "parquet" / "facts.parquet"
    facts = pd.read_parquet(facts_path)
    facts["park_date"] = facts["park_date"].astype(str)
    bad_ts = datetime(2026, 7, 25, 12, 0)
    bad_facts = pd.DataFrame([
        # one all-null row, like source_file tds_2026-07-25 in prod
        {"entity_code": None, "park_date": "NaT", "observed_at": None, "observed_at_ts": pd.NaT,
         "wait_time_type": None, "wait_time_minutes": None},
        {"entity_code": "AK85", "park_date": "NaT", "observed_at": bad_ts.isoformat(),
         "observed_at_ts": bad_ts, "wait_time_type": "ACTUAL", "wait_time_minutes": 999},
        {"entity_code": "HS112", "park_date": "not-a-date", "observed_at": bad_ts.isoformat(),
         "observed_at_ts": bad_ts, "wait_time_type": "ACTUAL", "wait_time_minutes": 999},
    ])
    facts = pd.concat([facts, bad_facts], ignore_index=True)
    facts.to_parquet(facts_path, index=False)

    n_valid_synth = 0
    for park, ent in (("AK", "AK86"), ("HS", "HS147")):
        p = base / "synthetic_actuals" / f"{park}.parquet"
        synth = pd.read_parquet(p)
        n_valid_synth += len(synth)
        synth["park_date"] = synth["park_date"].astype(str)
        synth = pd.concat([synth, pd.DataFrame([
            {"entity_code": ent, "park_date": "NaT", "observed_at": str(bad_ts), "synthetic_actual": 999.0},
        ])], ignore_index=True)
        synth.to_parquet(p, index=False)
    return len(facts) - len(bad_facts), n_valid_synth


def test_nat_park_date_rows_are_dropped_counted_and_logged(tmp_path, caplog):
    base = _base(tmp_path / "prod")
    n_valid_facts, n_valid_synth = _with_nat_rows(base)
    logger = logging.getLogger("test_nat_park_date")

    with caplog.at_level(logging.WARNING, logger="test_nat_park_date"):
        receipt = build_actuals_training_data(
            logger, TrainingInputs.under(base), tmp_path / "out",
            lambda park: 2016,  # exercises the park_date::DATE year filter
            write_combined=False,
        )

    out = pd.read_parquet(tmp_path / "out" / "actuals_training_v2")
    assert "NaT" not in set(out["park_date"].astype(str))
    assert (out["actual_time"] != 999).all()
    assert int((~out["is_synthetic"]).sum()) == n_valid_facts
    assert int(out["is_synthetic"].sum()) == n_valid_synth
    assert set(out["entity_code"]) == {"AK85", "AK86", "AK25", "HS112", "HS147"}
    assert pd.api.types.is_string_dtype(out["park_date"]) or out["park_date"].dtype == object

    assert receipt["nat_park_date_dropped"] == {
        "facts": {"<null>": 1, "AK85": 1, "HS112": 1},
        "synthetic": {"AK86": 1, "HS147": 1},
    }
    msgs = [r.getMessage() for r in caplog.records]
    for line in (
        "nat_park_date_dropped source=facts entity=<null> n=1",
        "nat_park_date_dropped source=facts entity=AK85 n=1",
        "nat_park_date_dropped source=facts entity=HS112 n=1",
        "nat_park_date_dropped source=facts total=3",
        "nat_park_date_dropped source=synthetic entity=AK86 n=1",
        "nat_park_date_dropped source=synthetic total=2",
    ):
        assert line in msgs, line


def test_total_line_logged_even_when_nothing_dropped(tmp_path, caplog):
    base = _base(tmp_path / "prod")
    logger = logging.getLogger("test_nat_park_date_clean")
    with caplog.at_level(logging.WARNING, logger="test_nat_park_date_clean"):
        receipt = build_actuals_training_data(
            logger, TrainingInputs.under(base), tmp_path / "out", lambda park: 0, write_combined=False,
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert "nat_park_date_dropped source=facts total=0" in msgs
    assert "nat_park_date_dropped source=synthetic total=0" in msgs
    assert receipt["nat_park_date_dropped"] == {"facts": {}, "synthetic": {}}


def test_convert_to_parquet_drops_blank_observed_at_and_logs(tmp_path, caplog):
    month = tmp_path / "clean" / "2026-07"
    month.mkdir(parents=True)
    pd.DataFrame({
        "entity_code": ["mk101", "MK102", None],
        "observed_at": ["2026-07-25T10:00:00-04:00", "2026-07-25T11:00:00-04:00", None],
        "wait_time_type": ["POSTED", "ACTUAL", None],
        "wait_time_minutes": [30, 25, None],
    }).to_csv(month / "tds_2026-07-25.csv", index=False)
    out_dir = tmp_path / "parquet"
    out_dir.mkdir()

    with caplog.at_level(logging.WARNING):
        name, n = convert_month_to_parquet((month, out_dir))

    assert (name, n) == ("2026-07", 2)
    df = pd.read_parquet(out_dir / "2026-07.parquet")
    assert "NaT" not in set(df["park_date"])
    assert df["park_date"].isna().sum() == 0
    assert sorted(df["entity_code"]) == ["MK101", "MK102"]
    assert "  2026-07: nat_park_date_dropped n=1" in [r.getMessage() for r in caplog.records]


def test_derive_park_date_nat_is_missing_and_valid_dates_unchanged():
    tz = ZoneInfo("America/New_York")
    s = pd.Series([
        "2026-07-25T10:00:00-04:00",  # 10 AM local -> same day
        "2026-07-25T05:59:00-04:00",  # before 6 AM local -> previous day
        "2026-07-25T09:30:00Z",       # 05:30 local -> previous day
        None,
        "garbage",
    ])
    got = derive_park_date(s, tz)
    assert got.iloc[:3].tolist() == ["2026-07-25", "2026-07-24", "2026-07-24"]
    assert got.iloc[3:].isna().all()
    assert "NaT" not in got.astype(str).tolist()[:3]
    df = pd.DataFrame({"park_date": got}).dropna(subset=["park_date"])
    assert len(df) == 3
