import json
import os
import time
from datetime import date, time as dtime, timedelta
from pathlib import Path

import pandas as pd

from pipeline.config import PipelineConfig
from pipeline.core.logging import PipelineLogger
from pipeline.steps import s07a_calendar, s08_forecast
from pipeline.tests.bias_fixtures import RUN_DATE, build_base, tree_digest

CLOSED_DAY = date.today() + timedelta(days=3)
CLOSURES_CSV = (
    "object_type,object_code,start_date,finish_date\n"
    f"attraction,AK25,{CLOSED_DAY},{CLOSED_DAY}\n"
    f"attraction,HS147,{CLOSED_DAY},{CLOSED_DAY + timedelta(days=1)}\n"
)


def fake_runner(fail=(), real_calendar=True):
    def run(script, out):
        if script in fail:
            return 1
        if script == "get_closures_from_s3.py":
            d = out / "raw_closures"
            d.mkdir(parents=True, exist_ok=True)
            (d / "current_wdw_closures.csv").write_text(CLOSURES_CSV)
            return 0
        if script == "get_entity_table_from_s3.py":
            d = out / "dimension_tables"
            d.mkdir(parents=True, exist_ok=True)
            pd.DataFrame({"code": ["AK85", "AK86", "AK25", "HS112", "HS147", "AK99"],
                          "fastpass_booth": [False] * 6, "extinct_on": [None] * 6}
                         ).to_csv(d / "dimentity.csv", index=False)
            return 0
        if script == "build_operating_calendar.py" and real_calendar:
            return s07a_calendar.run_script(script, out)
        return 1
    return run


def _seed_previous(base: Path):
    (base / "raw_closures").mkdir()
    (base / "raw_closures/current_wdw_closures.csv").write_text("object_type,object_code,start_date,finish_date\n")
    old = time.time() - 86400 * 30
    os.utime(base / "raw_closures/current_wdw_closures.csv", (old, old))
    return {p: (base / p).read_bytes() for p in ("raw_closures/current_wdw_closures.csv",
                                                 "dimension_tables/dimentity.csv")}


def _run(base, runner, shadow=False):
    cfg = PipelineConfig(output_base=base, shadow=shadow,
                         shadow_output_base=base.parent / "shadow" if shadow else None)
    return cfg, s07a_calendar.run(cfg, PipelineLogger("t"), runner=runner, run_date=RUN_DATE)["receipt"]


def test_s3_success_refreshes_closures_calendar_and_reports_ok(tmp_path):
    base = build_base(tmp_path / "prod")
    _seed_previous(base)
    start = time.time()
    cfg, receipt = _run(base, fake_runner())
    assert receipt["status"] == "ok", receipt
    assert (base / "raw_closures/current_wdw_closures.csv").stat().st_mtime >= start - 1
    oc = pd.read_parquet(base / "operating_calendar/operating_calendar.parquet")
    closed = oc[(oc["entity_code"] == "AK25") & (pd.to_datetime(oc["park_date"]).dt.date == CLOSED_DAY)]
    assert not closed["is_operating"].any()
    assert not list(base.glob(".s07a_staging-*"))


def test_closed_entity_gets_no_forecast_for_the_closed_date(tmp_path):
    base = build_base(tmp_path / "prod")
    cfg, _ = _run(base, fake_runner())
    oc = s08_forecast._load_operating_calendar_indexed(cfg, PipelineLogger("t"))
    grid = pd.DataFrame({"park_date": [CLOSED_DAY, CLOSED_DAY + timedelta(days=5)],
                         "time_slot": [dtime(10, 0)] * 2, "time_slot_15min": [40, 40],
                         "hour_of_day": [10, 10], "mins_since_6am": [240, 240],
                         "date_group_id": ["DG_A"] * 2, "date_group_id_encoded": [0, 0],
                         "season_encoded": [1, 1], "season_year_encoded": [5, 5]})
    for entity, park in (("AK25", "AK"), ("HS147", "HS")):
        df, _ = s08_forecast._forecast_entity_fast(entity, park, grid, cfg.models_dir, {}, {},
                                                    {"__global__": 0.7}, oc)
        assert CLOSED_DAY not in set(df["park_date"])
        assert len(df) == 1


def test_s3_failure_is_degraded_and_keeps_previous_closures(tmp_path):
    base = build_base(tmp_path / "prod")
    prev = _seed_previous(base)
    cfg, receipt = _run(base, fake_runner(fail={"get_closures_from_s3.py"}))
    assert receipt["status"] == "degraded"
    assert "closures" in receipt["reason"]
    assert (base / "raw_closures/current_wdw_closures.csv").read_bytes() == prev["raw_closures/current_wdw_closures.csv"]
    assert receipt["parts"]["operating_calendar"]["status"] == "ok"


def test_s3_exit_zero_with_no_files_is_degraded(tmp_path):
    base = build_base(tmp_path / "prod")
    prev = _seed_previous(base)

    def silent(script, out):
        if script == "get_closures_from_s3.py":
            return 0
        return fake_runner()(script, out)

    _, receipt = _run(base, silent)
    assert receipt["parts"]["closures"]["status"] == "degraded"
    assert (base / "raw_closures/current_wdw_closures.csv").read_bytes() == prev["raw_closures/current_wdw_closures.csv"]


def test_dimentity_failure_is_degraded_and_keeps_previous_dimentity(tmp_path):
    base = build_base(tmp_path / "prod")
    prev = _seed_previous(base)
    _, receipt = _run(base, fake_runner(fail={"get_entity_table_from_s3.py"}))
    assert receipt["parts"]["dimentity"]["status"] == "degraded"
    assert (base / "dimension_tables/dimentity.csv").read_bytes() == prev["dimension_tables/dimentity.csv"]


def test_invalid_dimentity_download_is_not_promoted(tmp_path):
    base = build_base(tmp_path / "prod")
    prev = _seed_previous(base)

    def bad(script, out):
        if script == "get_entity_table_from_s3.py":
            (out / "dimension_tables").mkdir(parents=True, exist_ok=True)
            (out / "dimension_tables/dimentity.csv").write_text("name\nSplash Mountain\n")
            return 0
        return fake_runner()(script, out)

    _, receipt = _run(base, bad)
    assert receipt["parts"]["dimentity"]["status"] == "degraded"
    assert (base / "dimension_tables/dimentity.csv").read_bytes() == prev["dimension_tables/dimentity.csv"]


def test_calendar_failure_keeps_previous_calendar(tmp_path):
    base = build_base(tmp_path / "prod")
    _run(base, fake_runner())
    before = (base / "operating_calendar/operating_calendar.parquet").read_bytes()
    _, receipt = _run(base, fake_runner(real_calendar=False))
    assert receipt["parts"]["operating_calendar"]["status"] == "degraded"
    assert (base / "operating_calendar/operating_calendar.parquet").read_bytes() == before


def test_all_sources_failing_still_returns_and_receipt_is_written(tmp_path):
    base = build_base(tmp_path / "prod")
    _, receipt = _run(base, lambda s, o: 2)
    assert receipt["status"] == "degraded"
    saved = json.loads((base / f"logs/receipt_s07a_calendar_{RUN_DATE}.json").read_text())
    assert saved["status"] == "degraded"


def test_shadow_mode_writes_nothing_in_production(tmp_path):
    base = build_base(tmp_path / "prod")
    _seed_previous(base)
    before = tree_digest(base)
    cfg, receipt = _run(base, fake_runner(), shadow=True)
    assert receipt["status"] == "ok"
    assert tree_digest(base) == before
    assert (cfg.shadow_output_base / "operating_calendar/operating_calendar.parquet").exists()


def test_no_credentials_reach_logs_or_receipt(tmp_path, capsys, monkeypatch):
    secret = "FAKEKEYID0000TPCR/sekrit-value-xyz"
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secret)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", secret)
    base = build_base(tmp_path / "prod")
    _run(base, s07a_calendar.run_script)
    out = capsys.readouterr()
    assert secret not in out.out + out.err
    for p in base.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".log", ".md", ".jsonl", ".txt"):
            assert secret not in p.read_text(errors="ignore"), p


def test_mapping_refresh_adds_new_labels_and_reports_them(tmp_path):
    base = build_base(tmp_path / "prod")
    _, receipt = _run(base, lambda s, o: 1)
    enc = receipt["parts"]["encodings"]
    assert enc["status"] == "ok" and enc["n_added"] >= 2
    m = json.loads((base / "state/encoding_mappings.json").read_text())
    assert m["season_year"]["AFTER_EASTER_2010"] == 0
    assert m["season_year"]["AUTUMN_2026"] > 0


def test_run_script_reports_a_failing_script(tmp_path):
    assert s07a_calendar.run_script("does_not_exist_tpcr.py", tmp_path) != 0
