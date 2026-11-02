import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from pipeline.config import PipelineConfig
from pipeline.core.encodings import UnmappedLabelError, load_mappings
from pipeline.core.logging import PipelineLogger
from pipeline.steps import STEP_ORDER, s06b_training_data
from pipeline.tests.bias_fixtures import RUN_DATE, build_base, tree_digest

REPO = Path(__file__).resolve().parents[2]


def _cfg(base, shadow=False):
    return PipelineConfig(output_base=base, shadow=shadow,
                          shadow_output_base=base.parent / "shadow" if shadow else None)


def _run(cfg):
    return s06b_training_data.run(cfg, PipelineLogger("t"), run_date=RUN_DATE)


def test_step_order_puts_s06b_between_s06_and_s07_and_s07a_before_s08():
    i = STEP_ORDER.index
    assert i("s06_synthetic") < i("s06b_training_data") < i("s07_training")
    assert i("s07_training") < i("s07a_calendar") < i("s08_forecast")


def test_rebuild_reaches_run_date_minus_two_and_keeps_autumn_rows(tmp_path):
    base = build_base(tmp_path / "prod")
    receipt = _run(_cfg(base))["receipt"]
    assert receipt["status"] == "ok"
    for park, d in receipt["max_park_date_by_park"].items():
        assert date.fromisoformat(d) >= RUN_DATE - timedelta(days=2)
    ak = pd.read_parquet(base / "matched_pairs/actuals_training_v2/AK.parquet")
    oct_rows = ak[pd.to_datetime(ak["park_date"]).dt.month == 10]
    sy = load_mappings(base / "state/encoding_mappings.json")["season_year"]
    assert set(oct_rows["season_year_encoded"]) == {sy["AUTUMN_2026"]}
    assert sy["AUTUMN_2026"] != 0
    assert (base / f"logs/receipt_s06b_training_data_{RUN_DATE}.json").exists()


def test_real_rows_are_all_kept(tmp_path):
    base = build_base(tmp_path / "prod")
    facts = pd.read_parquet(base / "fact_tables/parquet/facts.parquet")
    receipt = _run(_cfg(base))["receipt"]
    for park in ("AK", "HS"):
        expected = int(facts["entity_code"].str.startswith(park).sum())
        assert receipt["real_rows_by_park"][park] == expected


def test_stale_facts_mark_the_step_degraded(tmp_path):
    base = build_base(tmp_path / "prod", last_actual=RUN_DATE - timedelta(days=3))
    receipt = _run(_cfg(base))["receipt"]
    assert receipt["status"] == "degraded"
    assert "AK" in receipt["reason"]


def test_two_runs_same_day_are_identical(tmp_path):
    base = build_base(tmp_path / "prod")
    first = _run(_cfg(base))["receipt"]
    second = _run(_cfg(base))["receipt"]
    for key in ("rows_by_park", "real_rows_by_park", "max_park_date_by_park"):
        assert first[key] == second[key]
    assert second["encodings"]["n_added"] == 0


def test_shadow_run_leaves_production_byte_identical(tmp_path):
    base = build_base(tmp_path / "prod")
    before = tree_digest(base)
    cfg = _cfg(base, shadow=True)
    _run(cfg)
    assert tree_digest(base) == before
    assert (cfg.shadow_output_base / "matched_pairs/actuals_training_v2/AK.parquet").exists()
    for p in (cfg.models_dir, cfg.forecast_dir, cfg.write_base):
        assert cfg.shadow_output_base in p.parents or p == cfg.shadow_output_base


def test_unmapped_label_fails_loudly_instead_of_dropping(tmp_path, monkeypatch):
    base = build_base(tmp_path / "prod")
    monkeypatch.setattr(s06b_training_data, "refresh_mappings_file",
                        lambda source, target, dim_dir: {"n_added": 0, "target": str(target)})
    with pytest.raises(UnmappedLabelError) as exc:
        _run(_cfg(base))
    assert "AUTUMN_2026" in str(exc.value)
    assert not (base / "matched_pairs/actuals_training_v2").exists() or not any(
        (base / "matched_pairs/actuals_training_v2").iterdir())


def test_failure_mid_build_keeps_previous_complete_files(tmp_path, monkeypatch):
    base = build_base(tmp_path / "prod")
    _run(_cfg(base))
    out = base / "matched_pairs"
    before = tree_digest(out)
    real = pd.DataFrame.to_parquet
    calls = {"n": 0}

    def boom(self, path, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(self, path, *a, **k)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)
    with pytest.raises(OSError):
        _run(_cfg(base))
    assert tree_digest(out) == before


def test_sigkill_mid_write_keeps_previous_complete_files(tmp_path):
    base = build_base(tmp_path / "prod")
    _run(_cfg(base))
    out = base / "matched_pairs"
    before = tree_digest(out)
    script = textwrap.dedent(f"""
        import sys, time
        sys.path[:0] = [{str(REPO)!r}, {str(REPO / 'src')!r}, {str(REPO / 'scripts')!r}]
        import pandas as pd
        real = pd.DataFrame.to_parquet
        def slow(self, path, *a, **k):
            real(self, path, *a, **k)
            open({str(tmp_path / 'wrote')!r}, 'w').close()
            time.sleep(60)
        pd.DataFrame.to_parquet = slow
        from datetime import date
        from pipeline.config import PipelineConfig
        from pipeline.core.logging import PipelineLogger
        from pipeline.steps import s06b_training_data
        s06b_training_data.run(PipelineConfig(output_base=__import__('pathlib').Path({str(base)!r})),
                               PipelineLogger('k'), run_date=date(2026, 10, 30))
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=str(REPO))
    try:
        deadline = time.time() + 60
        while not (tmp_path / "wrote").exists():
            assert proc.poll() is None and time.time() < deadline
            time.sleep(0.1)
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait(timeout=30)
    after = {k: v for k, v in tree_digest(out).items() if not k.startswith(".actuals_training_v2.staging-")}
    assert after == before


def test_build_script_has_no_pipeline_v3_dependency():
    src = (REPO / "scripts/build_actuals_training_data.py").read_text()
    assert "from pipeline_v3" not in src and "import pipeline_v3" not in src
    r = subprocess.run([sys.executable, str(REPO / "scripts/build_actuals_training_data.py"), "--help"],
                       capture_output=True, text=True, cwd="/")
    assert r.returncode == 0, r.stderr


def test_s07_uses_each_parks_own_half_life(tmp_path, monkeypatch):
    from pipeline.steps import s07_training
    base = build_base(tmp_path / "prod")
    cfg = PipelineConfig(output_base=base, geo_decay_halflife_days_per_park={"AK": 180})
    s06b_training_data.run(cfg, PipelineLogger("t"), run_date=RUN_DATE)
    used = {}

    def spy(entity_df, cfg, min_samples, as_of, halflife_days):
        used[entity_df["entity_code"].iloc[0][:2]] = halflife_days
        return None

    monkeypatch.setattr(s07_training, "fit_baseline", spy)
    s07_training.run(cfg, PipelineLogger("t"), as_of=RUN_DATE)
    assert used == {"AK": 180, "HS": 730}
