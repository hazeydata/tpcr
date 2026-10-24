import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from pipeline.models import bias_guard


def slots(entity, days, bias, noise, method="model_baseline", end=date(2026, 10, 19), evaluation="2026-10-20"):
    rows = []
    rng = np.random.default_rng(abs(hash(entity)) % 2**32)
    for d in range(days):
        day = (end - timedelta(days=d)).isoformat()
        for s in range(20):
            err = bias + rng.normal(0, noise)
            rows.append({"entity_code": entity, "park_date": day, "time_slot": s,
                         "prediction_method": method, "signed_error": err,
                         "absolute_error": abs(err), "evaluation_date": evaluation})
    return pd.DataFrame(rows)


class Guard(unittest.TestCase):
    as_of = date(2026, 10, 19)

    def test_pandora_like_guarded_others_not(self):
        df = pd.concat([
            slots("AK86", 30, 47.0, 3.0),
            slots("AK85", 30, 41.0, 3.0),
            slots("AK07", 30, 8.0, 10.0),
            slots("MK01", 30, -2.0, 6.0),
            slots("HS01", 30, 25.0, 40.0),
        ])
        res = bias_guard.evaluate(df, self.as_of)
        self.assertEqual(res["guarded"], ["AK85", "AK86"])
        self.assertEqual(res["reason"], "bias_guard")

    def test_threshold_is_strict_and_symmetric(self):
        df = pd.concat([slots("A", 30, 20.0, 0.0), slots("B", 30, -30.0, 0.0)])
        self.assertEqual(bias_guard.evaluate(df, self.as_of)["guarded"], ["B"])

    def test_mae_tolerance_boundary(self):
        base = slots("C", 30, 30.0, 0.0)
        base["absolute_error"] = 33.5
        self.assertEqual(bias_guard.evaluate(base, self.as_of)["guarded"], [])

    def test_window_excludes_older_days_and_fallback_rows(self):
        old = slots("D", 30, 50.0, 0.0, end=self.as_of - timedelta(days=30))
        fb = slots("E", 30, 50.0, 0.0, method="fallback_ratio")
        self.assertEqual(bias_guard.evaluate(pd.concat([old, fb]), self.as_of)["guarded"], [])

    def test_latest_evaluation_wins(self):
        a = slots("F", 30, 50.0, 0.0, evaluation="2026-10-01")
        b = slots("F", 30, 1.0, 5.0, evaluation="2026-10-20")
        self.assertEqual(bias_guard.evaluate(pd.concat([a, b]), self.as_of)["guarded"], [])

    def test_guard_carried_when_entity_only_has_fallback_rows(self):
        prev = bias_guard.evaluate(slots("AK86", 30, 47.0, 1.0), self.as_of)
        fb = slots("AK86", 30, -3.0, 1.0, method="fallback_ratio")
        res = bias_guard.evaluate(fb, self.as_of, prev["entities"])
        self.assertEqual(res["guarded"], ["AK86"])
        self.assertEqual(res["entities"]["AK86"]["basis"], "carried")

    def test_save_writes_state_and_appends_daily_log(self):
        tmp = Path(tempfile.mkdtemp())
        res = bias_guard.evaluate(slots("AK86", 30, 47.0, 1.0), self.as_of)
        bias_guard.save(res, tmp / "state", tmp / "accuracy")
        bias_guard.save(res, tmp / "state", tmp / "accuracy")
        self.assertEqual(bias_guard.guarded_entities(tmp / "state"), {"AK86"})
        lines = (tmp / "accuracy" / "bias_guard_log.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["reason"], "bias_guard")


class ForecastRouting(unittest.TestCase):
    def setUp(self):
        from pipeline.steps import s08_forecast
        self.s08 = s08_forecast
        self.tmp = Path(tempfile.mkdtemp())
        grid = pd.DataFrame({
            "park_date": [date(2026, 10, 21)] * 4,
            "time_slot": [0, 1, 2, 3],
            "date_group_id": [1] * 4,
            "time_slot_15min": [0, 1, 2, 3],
            "mins_since_6am": [180, 195, 210, 225],
            "hour_of_day": [9, 9, 9, 9],
        })
        self.grid = grid
        self.agg = {"AK86": pd.DataFrame({"date_group_id": [1] * 4, "time_slot_15min": [0, 1, 2, 3],
                                          "wait_median": [100.0, 90.0, 80.0, 70.0]})}

    def call(self, guarded):
        return self.s08._forecast_entity_fast(
            "AK86", "AK", self.grid, self.tmp, self.agg, {}, {"AK86": 0.5}, None, guarded)

    def test_guarded_uses_fallback_ratio_and_reason(self):
        df, key = self.call(True)
        self.assertEqual(key, "bias_guard")
        self.assertEqual(set(df["prediction_method"]), {"fallback_ratio"})
        self.assertEqual(df["predicted_actual"].tolist(), [50, 45, 40, 35])

    def test_unguarded_path_unchanged(self):
        df, key = self.call(False)
        self.assertEqual(key, "fallback")
        self.assertEqual(df["predicted_actual"].tolist(), [50, 45, 40, 35])


class NoGlobalBiasCorrection(unittest.TestCase):
    def test_pipeline_import_does_not_load_bias_correction(self):
        code = (
            "import sys, importlib; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
            "from pipeline.steps import STEP_ORDER\n"
            "import pipeline.pipeline\n"
            "[importlib.import_module('pipeline.steps.' + s) for s in STEP_ORDER]\n"
            "bad = [m for m in sys.modules if 'bias_correction' in m.lower() or 'bias_correct' in m.lower()]\n"
            "print(bad); sys.exit(1 if bad else 0)\n"
        ) % (str(REPO), str(REPO / "src"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_no_pipeline_source_references_bias_correction_module(self):
        hits = []
        for p in (REPO / "pipeline").rglob("*.py"):
            if p.name == "test_ak_bias.py":
                continue
            for line in p.read_text(errors="ignore").splitlines():
                s = line.strip()
                if s.startswith(("import ", "from ")) and "bias_correct" in s:
                    hits.append(f"{p}: {s}")
        self.assertEqual(hits, [])


def write_facts(base: Path):
    rows = []
    for d in pd.date_range("2026-01-05", "2026-04-30", freq="7D"):
        for code, post in (("AK86", 90.0), ("AK85", 60.0)):
            for hour in (9, 13, 18):
                ts = datetime(d.year, d.month, d.day, hour, 10)
                ratio = {9: 0.7, 13: 0.5, 18: 0.4}[hour]
                rows.append((code, d.date(), ts, "POSTED", post))
                rows.append((code, d.date(), ts + timedelta(minutes=5), "ACTUAL", post * ratio))
    rows.append(("AK86", date(2025, 6, 1), datetime(2025, 6, 1, 9), "ACTUAL", 1.0))
    df = pd.DataFrame(rows, columns=["entity_code", "park_date", "observed_at_ts", "wait_time_type", "wait_time_minutes"])
    out = base / "fact_tables" / "parquet"
    out.mkdir(parents=True)
    df.to_parquet(out / "facts.parquet", index=False)


class RatioStudyAndRefresh(unittest.TestCase):
    def setUp(self):
        from pipeline.diagnostics import pandora_ratio
        self.pr = pandora_ratio
        self.base = Path(tempfile.mkdtemp())
        write_facts(self.base)

    def test_study_hourly_ratio_and_n_from_2026_only(self):
        res = self.pr.study(self.base)
        self.assertAlmostEqual(res["ratios"]["AK86"]["13"]["ratio"], 0.5)
        self.assertAlmostEqual(res["ratios"]["AK85"]["18"]["ratio"], 0.4)
        self.assertEqual(res["ratios"]["AK86"]["9"]["n"], 17)
        self.assertTrue(res["source_window"]["start"].startswith("2026"))
        self.assertIn("| 13 | 17 |", self.pr.render_markdown(res))

    def test_refresh_after_freeze_is_byte_identical_and_versioned(self):
        self.pr.main(["study", "--output-base", str(self.base)])
        ratio = self.base / "accuracy" / "pandora_ratio_2026.json"
        self.pr.main(["refresh", "--output-base", str(self.base), "--ratio", str(ratio)])
        out = self.base / "synthetic_actuals" / "ratio_refresh" / "AK86.parquet"
        first = hashlib.sha256(out.read_bytes()).hexdigest()
        self.pr.main(["refresh", "--output-base", str(self.base), "--ratio", str(ratio)])
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), first)
        df = pd.read_parquet(out)
        self.assertTrue((df["park_date"] > "2026-03-20").all())
        self.assertAlmostEqual(df[df["observed_at"].str.endswith("13:10:00")]["synthetic_actual"].iloc[0], 45.0)
        import pyarrow.parquet as pq
        meta = json.loads(pq.read_schema(out).metadata[b"tpcr_ratio_refresh"])
        self.assertEqual(meta["ratio_version"], self.pr.ratio_version(ratio))
        self.assertEqual(meta["posted_window"]["after"], "2026-03-20")
        self.assertEqual(json.loads((out.with_suffix(".json")).read_text()), meta)

    def test_refresh_refuses_missing_hour(self):
        doc = self.pr.study(self.base)
        del doc["ratios"]["AK86"]["18"]
        p = self.base / "r.json"
        p.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):
            self.pr.refresh(self.base, p)


if __name__ == "__main__":
    unittest.main()
