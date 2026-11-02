import json
from datetime import date, timedelta

import pandas as pd
import pytest

from pipeline.config import PipelineConfig
from pipeline.core import paths
from pipeline.core.encodings import UnmappedLabelError, encode, extend_mappings, lookup
from pipeline.core.logging import PipelineLogger
from pipeline.steps import s08_forecast
from pipeline.tests.bias_fixtures import build_base, frozen_mappings


def test_extend_appends_without_renumbering_and_is_deterministic():
    m = frozen_mappings()
    ext, added = extend_mappings(m, {"season_year": ["AUTUMN_2026", "SUMMER_2026", "WINTER_2026"]})
    for k, v in m["season_year"].items():
        assert ext["season_year"][k] == v
    assert added["season_year"] == ["AUTUMN_2026", "SUMMER_2026"]
    assert ext["season_year"]["AUTUMN_2026"] == 3 and ext["season_year"]["SUMMER_2026"] == 4
    again, added2 = extend_mappings(ext, {"season_year": ["SUMMER_2026", "AUTUMN_2026"]})
    assert again == ext and added2["season_year"] == []


def test_extend_ignores_missing_labels():
    ext, added = extend_mappings(frozen_mappings(), {"season": [None, float("nan")]})
    assert added["season"] == []


def test_encode_raises_on_unknown_instead_of_zero():
    with pytest.raises(UnmappedLabelError) as e:
        encode(pd.Series(["WINTER_2026", "AUTUMN_2026"]), frozen_mappings()["season_year"], "season_year")
    assert e.value.unmapped == {"season_year": ["AUTUMN_2026"]}
    assert list(encode(pd.Series(["WINTER_2026"]), frozen_mappings()["season_year"], "season_year")) == [2]


def test_lookup_unknown_and_missing_are_none_not_zero():
    m = frozen_mappings()["season_year"]
    assert lookup(m, "AUTUMN_2026") is None
    assert lookup(m, None) is None
    assert lookup(m, float("nan")) is None
    assert lookup(m, "AFTER_EASTER_2010") == 0


def test_s08_skips_dates_with_unmapped_labels():
    df = pd.DataFrame({
        "park_date": [date(2026, 3, 1), date(2026, 10, 30)],
        "date_group_id": ["DG_A", "DG_A"],
        "season": ["WINTER", "AUTUMN"],
        "season_year": ["WINTER_2026", "AUTUMN_2026"],
    })
    feats = s08_forecast.encode_date_features(df, frozen_mappings())
    assert date(2026, 3, 1) in feats and date(2026, 10, 30) not in feats
    assert feats[date(2026, 3, 1)]["season_year_encoded"] == 2


def test_s08_time_grid_has_no_rows_for_unencoded_dates():
    start = date(2026, 10, 29)
    feats = {start: {"date_group_id": "DG_A", "date_group_id_encoded": 0,
                     "season_encoded": 1, "season_year_encoded": 7}}
    grid = s08_forecast._generate_park_time_grid(start, start + timedelta(days=1), feats, {}, "AK",
                                                 "America/New_York")
    assert set(grid["park_date"]) == {start}
    assert set(grid["season_year_encoded"]) == {7}
    assert s08_forecast.unmapped_forecast_dates(feats, start, start + timedelta(days=1)) == [start + timedelta(days=1)]


def test_shadow_config_routes_every_output_to_shadow(tmp_path):
    prod, shadow = tmp_path / "prod", tmp_path / "shadow"
    cfg = PipelineConfig(output_base=prod, shadow=True, shadow_output_base=shadow)
    for p in (cfg.models_dir, cfg.forecast_dir, cfg.state_dir, cfg.logs_dir, cfg.wti_dir,
              paths.training_data_dir(cfg), paths.encoding_mappings_write_path(cfg)):
        assert shadow in p.parents, p
    assert prod in paths.conversion_model_path(cfg).parents
    live = PipelineConfig(output_base=prod)
    assert live.models_dir == prod / "models"
    assert live.forecast_dir == prod / "curves" / "forecast_parquet"


def test_halflife_is_per_park_with_global_default():
    cfg = PipelineConfig(geo_decay_halflife_days_per_park={"AK": 180})
    assert cfg.get_halflife_days("AK") == 180
    assert cfg.get_halflife_days("MK") == 730


def test_s08_load_date_features_reads_refreshed_mapping_not_stale_prod(tmp_path):
    prod, shadow = build_base(tmp_path / "prod"), tmp_path / "shadow"
    refreshed, _ = extend_mappings(frozen_mappings(), {"season_year": ["AUTUMN_2026"]})
    cfg = PipelineConfig(output_base=prod, shadow=True, shadow_output_base=shadow)
    written = paths.encoding_mappings_write_path(cfg)
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(json.dumps(refreshed))
    stale = json.loads((prod / paths.ENCODING_MAPPINGS).read_text())
    assert "AUTUMN_2026" not in stale["season_year"]

    feats = s08_forecast._load_date_features(cfg, PipelineLogger("t"))
    autumn = [d for d in feats if d >= date(2026, 9, 1) and d.year == 2026]
    code = refreshed["season_year"]["AUTUMN_2026"]
    assert code != 0
    assert date(2026, 10, 30) in feats
    assert autumn and {feats[d]["season_year_encoded"] for d in autumn} == {code}
