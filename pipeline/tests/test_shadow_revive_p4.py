"""TPCR-SHADOW-REVIVE-001 Phase 4 (AC12, AC13, AC15): flag rule and no-posting.

Everything runs in tmp dirs. No network, no subprocesses, no real pipeline data.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.competition import flag_rule

REPO_ROOT = Path(__file__).resolve().parents[2]
SHADOW_SCRIPT = REPO_ROOT / "scripts" / "rolling_shadow.py"
JOB_SCRIPT = REPO_ROOT / "scripts" / "shadow_job.py"
COMPETITION_DIR = REPO_ROOT / "pipeline" / "competition"

EVAL_DATE = "2026-10-23"


def load_rs():
    spec = importlib.util.spec_from_file_location("rolling_shadow_p4_under_test", SHADOW_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def score_rows(day, name, e_prod, e_chal, fallback=None):
    """One row per entity: actual 0, so the abs errors are e_prod / e_chal."""
    n = len(e_prod)
    return pd.DataFrame({
        "date": [day] * n, "challenger_id": [name] * n,
        "entity_code": [f"E{i:03d}" for i in range(n)], "slot": [1] * n,
        "actual": [0.0] * n, "prod_pred": list(e_prod), "chal_pred": list(e_chal),
        "is_fallback_entity": list(fallback) if fallback is not None else [False] * n,
    })


def winning_errors(n=40):
    """Challenger better on every entity by 1.0-1.4 min."""
    e_chal = np.full(n, 5.0)
    e_prod = e_chal + 1.0 + 0.1 * (np.arange(n) % 5)
    return e_prod, e_chal


class FlagRuleUnitTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(flag_rule.MIN_STREAK, 7)
        self.assertEqual(flag_rule.BOOTSTRAP_RESAMPLES, 10_000)
        self.assertEqual(flag_rule.MIN_GAIN_MIN, 0.3)
        self.assertEqual(flag_rule.BOOTSTRAP_SEED, 20261024)

    def test_per_entity_gain(self):
        gains = flag_rule.per_entity_gain(
            ["B", "A", "A", "B"], [0, 0, 0, 0], [4, 3, 5, 6], [1, 3, 1, 2])
        # A: (|3|-|3| + |5|-|1|)/2 = 2.0 ; B: (|4|-|1| + |6|-|2|)/2 = 3.5
        self.assertEqual(list(gains), [2.0, 3.5])

    def test_bootstrap_deterministic_with_seed(self):
        gains = np.random.default_rng(1).normal(0.5, 2.0, size=60)
        a = flag_rule.bootstrap_ci(gains, 2000, seed=flag_rule.BOOTSTRAP_SEED)
        b = flag_rule.bootstrap_ci(gains, 2000, seed=flag_rule.BOOTSTRAP_SEED)
        c = flag_rule.bootstrap_ci(gains, 2000, seed=flag_rule.BOOTSTRAP_SEED + 1)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertLess(a[0], a[1])

    def test_bootstrap_default_is_deterministic_and_covers_mean(self):
        gains = np.linspace(-1.0, 3.0, 50)
        a = flag_rule.bootstrap_ci(gains)
        self.assertEqual(a, flag_rule.bootstrap_ci(gains))
        self.assertLess(a[0], gains.mean())
        self.assertGreater(a[1], gains.mean())

    def test_bootstrap_empty(self):
        self.assertEqual(flag_rule.bootstrap_ci([]), (None, None))

    def test_should_flag_each_single_failure_prevents(self):
        self.assertTrue(flag_rule.should_flag(True, 7, 0.1, 0.5))
        self.assertTrue(flag_rule.should_flag(True, 9, 0.1, 0.3))
        self.assertFalse(flag_rule.should_flag(True, 6, 0.1, 0.5), "6-day streak")
        self.assertFalse(flag_rule.should_flag(True, 7, -0.1, 0.5), "CI includes 0")
        self.assertFalse(flag_rule.should_flag(True, 7, 0.0, 0.5), "CI touches 0")
        self.assertFalse(flag_rule.should_flag(True, 7, 0.1, 0.29), "gain 0.29")
        self.assertFalse(flag_rule.should_flag(False, 7, 0.1, 0.5), "no win today")
        self.assertFalse(flag_rule.should_flag(True, 7, None, 0.5), "no CI")

    def test_streak_breaks_on_non_ok_or_missing_day(self):
        def summaries(overrides):
            out = {}
            for back in range(1, 12):
                day = (date.fromisoformat(EVAL_DATE) - timedelta(days=back)).isoformat()
                out[day] = {"run_status": "ok", "challengers": {"c1": {"win": True}}}
            out.update(overrides)
            return lambda d: out.get(d)

        day = lambda back: (date.fromisoformat(EVAL_DATE) - timedelta(days=back)).isoformat()  # noqa: E731
        self.assertEqual(flag_rule.streak_length(EVAL_DATE, False, summaries({}), "c1"), 0)
        self.assertEqual(flag_rule.streak_length(EVAL_DATE, True, summaries({day(4): None}), "c1"), 4)
        for status in ("skipped_lock", "failed", "skipped_no_marker"):
            skipped = {day(3): {"run_status": status, "challengers": {"c1": {"win": True}}}}
            self.assertEqual(flag_rule.streak_length(EVAL_DATE, True, summaries(skipped), "c1"), 3, status)
        no_data = {day(2): {"run_status": "ok", "challengers": {"c1": {"win": False}}}}
        self.assertEqual(flag_rule.streak_length(EVAL_DATE, True, summaries(no_data), "c1"), 2)
        self.assertEqual(flag_rule.streak_length(EVAL_DATE, True, summaries({}), "other"), 1)

    def test_float_mae_gain_of_exactly_point_three_flags(self):
        self.assertTrue(flag_rule.should_flag(True, 7, 0.1, flag_rule.gain_min(5.3, 5.0)))
        self.assertFalse(flag_rule.should_flag(True, 7, 0.1, flag_rule.gain_min(5.29, 5.0)))

    def test_significant_gain_text(self):
        self.assertEqual(flag_rule.significant_gain_text([]), "None yet")
        self.assertEqual(flag_rule.significant_gain_text(["a"]), "Significant gain: a")
        self.assertEqual(flag_rule.significant_gain_text(["a", "b"]), "Significant gain: a, b")


class DaySummaryCase(unittest.TestCase):
    """write_day_outputs end to end in a tmp competition dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.rs = load_rs()
        self.rs.configure(pipeline_base=self._tmp.name, tpcr_dir=f"{self._tmp.name}/repo")
        self.addCleanup(self.rs.configure)
        self.comp = self.rs.COMPETITION_BASE
        for sub in ("scores", "summaries"):
            (self.comp / sub).mkdir(parents=True)

    def day(self, back):
        return (date.fromisoformat(EVAL_DATE) - timedelta(days=back)).isoformat()

    def seed_history(self, name, e_prod, e_chal, prior_days=6, fallback=None, broken=None):
        """Committed scores and ok/win summaries for the `prior_days` days before EVAL_DATE."""
        broken = broken or {}
        for back in range(1, prior_days + 1):
            d = self.day(back)
            score_rows(d, name, e_prod, e_chal, fallback).to_parquet(
                self.comp / "scores" / f"{d}.parquet", index=False)
            summary = {"date": d, "run_status": broken.get(back, "ok"),
                       "challengers": {name: {"win": True}}}
            (self.comp / "summaries" / f"{d}.json").write_text(json.dumps(summary))

    def run_today(self, name, e_prod, e_chal, fallback=None, scope=None, errors=(), registry=None):
        df = score_rows(EVAL_DATE, name, e_prod, e_chal, fallback)
        staged = self.rs.staging_path(name, EVAL_DATE)
        staged.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(staged, index=False)
        fb = df[df["is_fallback_entity"]]
        metrics = {
            "n_matched": len(df), "n_entities": len(df), "coverage_pct": 100.0,
            "forecast_entities": len(df),
            "baseline_mae": float(np.mean(e_prod)), "challenger_mae": float(np.mean(e_chal)),
            "mae_fallback_prod": float(fb["prod_pred"].mean()) if len(fb) else None,
            "mae_fallback_chal": float(fb["chal_pred"].mean()) if len(fb) else None,
        }
        challenger = {"name": name}
        if scope:
            challenger["score_scope"] = scope
        summary = self.rs.write_day_outputs(EVAL_DATE, [(challenger, metrics)], list(errors))
        on_disk = json.loads((self.comp / "summaries" / f"{EVAL_DATE}.json").read_text())
        self.assertEqual(on_disk, json.loads(json.dumps(summary)))
        return summary

    def test_flagged_when_all_three_hold(self):
        e_prod, e_chal = winning_errors()
        self.seed_history("c1", e_prod, e_chal, prior_days=6)
        summary = self.run_today("c1", e_prod, e_chal)
        block = summary["challengers"]["c1"]
        self.assertTrue(block["win"])
        self.assertEqual(block["streak"], 7)
        self.assertGreater(block["ci95_low"], 0)
        self.assertGreater(block["ci95_high"], block["ci95_low"])
        self.assertGreaterEqual(block["gain_min"], 0.3)
        self.assertTrue(block["flagged"])
        self.assertEqual(summary["significant_gain"], ["c1"])
        self.assertEqual(summary["significant_gain_text"], "Significant gain: c1")

        registry = {"challengers": [{"name": "c1", "status": "active"}], "retired": []}
        self.assertEqual(self.rs.apply_flags(registry, summary), ["c1"])
        self.assertEqual(registry["challengers"][0]["status"], "flagged")
        self.assertEqual(registry["challengers"][0]["flagged_on"], EVAL_DATE)

    def test_six_day_streak_does_not_flag(self):
        e_prod, e_chal = winning_errors()
        self.seed_history("c1", e_prod, e_chal, prior_days=5)
        summary = self.run_today("c1", e_prod, e_chal)
        block = summary["challengers"]["c1"]
        self.assertEqual(block["streak"], 6)
        self.assertGreater(block["ci95_low"], 0)
        self.assertGreaterEqual(block["gain_min"], 0.3)
        self.assertFalse(block["flagged"])
        self.assertEqual(summary["significant_gain"], [])
        self.assertEqual(summary["significant_gain_text"], "None yet")

    def test_ci_including_zero_does_not_flag(self):
        e_prod, e_chal = [10.0, 1.0, 6.0], [5.0, 5.0, 4.0]  # per-entity gains 5, -4, 2
        self.seed_history("c1", e_prod, e_chal, prior_days=6)
        block = self.run_today("c1", e_prod, e_chal)["challengers"]["c1"]
        self.assertEqual(block["streak"], 7)
        self.assertTrue(block["win"])
        self.assertGreaterEqual(block["gain_min"], 0.3)
        self.assertLessEqual(block["ci95_low"], 0)
        self.assertFalse(block["flagged"])

    def test_gain_029_does_not_flag(self):
        e_chal = np.full(30, 5.0)
        e_prod = e_chal + 0.29
        self.seed_history("c1", e_prod, e_chal, prior_days=6)
        block = self.run_today("c1", e_prod, e_chal)["challengers"]["c1"]
        self.assertEqual(block["streak"], 7)
        self.assertGreater(block["ci95_low"], 0)
        self.assertAlmostEqual(block["gain_min"], 0.29, places=6)
        self.assertFalse(block["flagged"])

    def test_skipped_or_failed_day_breaks_streak(self):
        e_prod, e_chal = winning_errors()
        for status in ("skipped_lock", "failed"):
            with self.subTest(status=status):
                self.seed_history("c1", e_prod, e_chal, prior_days=6, broken={3: status})
                block = self.run_today("c1", e_prod, e_chal)["challengers"]["c1"]
                self.assertEqual(block["streak"], 3)
                self.assertFalse(block["flagged"])

    def test_failed_run_today_never_flags(self):
        e_prod, e_chal = winning_errors()
        self.seed_history("c1", e_prod, e_chal, prior_days=6)
        summary = self.run_today("c1", e_prod, e_chal, errors=["other: forecast failed"])
        self.assertEqual(summary["run_status"], "failed")
        block = summary["challengers"]["c1"]
        self.assertEqual((block["win"], block["streak"], block["flagged"]), (False, 0, False))
        self.assertEqual(summary["significant_gain"], [])

    def test_losing_day_is_not_a_win(self):
        e_prod, e_chal = winning_errors()
        self.seed_history("c1", e_prod, e_chal, prior_days=6)
        block = self.run_today("c1", e_chal, e_prod)["challengers"]["c1"]  # sides swapped
        self.assertEqual((block["win"], block["streak"], block["flagged"]), (False, 0, False))
        self.assertLess(block["gain_min"], 0)

    def test_fallback_scope_uses_fallback_entities_only(self):
        n = 40
        fallback = [i < 20 for i in range(n)]
        e_chal = np.full(n, 5.0)
        e_prod = e_chal + 1.0 + 0.1 * (np.arange(n) % 5)
        # non-fallback entities: challenger far worse, so an all-entity pool would not flag
        e_prod[20:], e_chal[20:] = 5.0, 12.0
        self.seed_history("fb", e_prod, e_chal, prior_days=6, fallback=fallback)
        block = self.run_today("fb", e_prod, e_chal, fallback=fallback, scope="fallback")["challengers"]["fb"]
        self.assertTrue(block["win"])
        self.assertEqual(block["streak"], 7)
        self.assertGreater(block["ci95_low"], 0)
        self.assertTrue(block["flagged"])

    def test_no_data_block_has_default_flag_fields(self):
        summary = self.rs.write_day_outputs(
            EVAL_DATE, [({"name": "c1"}, "no_data")], [])
        block = summary["challengers"]["c1"]
        self.assertEqual(
            {k: block[k] for k in ("win", "streak", "ci95_low", "ci95_high", "gain_min", "flagged")},
            {"win": False, "streak": 0, "ci95_low": None, "ci95_high": None,
             "gain_min": None, "flagged": False})
        self.assertEqual(summary["significant_gain"], [])
        self.assertEqual(summary["significant_gain_text"], "None yet")

    def test_apply_flags_unflags_and_skips_retired(self):
        registry = {"challengers": [
            {"name": "a", "status": "flagged", "flagged_on": "2026-10-20"},
            {"name": "b", "status": "active"},
            {"name": "c", "status": "retired"},
        ], "retired": []}
        summary = {"date": EVAL_DATE, "challengers": {
            "a": {"flagged": False}, "b": {"flagged": True}, "c": {"flagged": True}}}
        self.assertEqual(self.rs.apply_flags(registry, summary), ["b"])
        status = {c["name"]: c["status"] for c in registry["challengers"]}
        self.assertEqual(status, {"a": "active", "b": "flagged", "c": "retired"})
        self.assertNotIn("flagged_on", registry["challengers"][0])

    def test_legacy_promotion_candidate_is_normalized_to_active(self):
        registry = {"challengers": [{"name": "a", "status": "promotion_candidate"}], "retired": []}
        self.assertEqual(self.rs.normalize_registry(registry)["challengers"][0]["status"], "active")
        self.assertIn("flagged", self.rs.RUNNABLE_STATUSES)


class NothingPostsTest(unittest.TestCase):
    def test_grep_is_zero(self):
        words = ["dis" + "cord", "requests" + r"\." + "post", "web" + "hook"]
        pattern = re.compile("|".join(words), re.IGNORECASE)
        files = [SHADOW_SCRIPT, JOB_SCRIPT] + sorted(COMPETITION_DIR.rglob("*.py"))
        self.assertGreater(len(files), 3)
        hits = [
            f"{f.name}:{i}: {line.strip()}"
            for f in files
            for i, line in enumerate(f.read_text().splitlines(), 1)
            if pattern.search(line)
        ]
        self.assertEqual(hits, [])

    def test_run_day_has_no_post_parameter(self):
        import inspect

        rs = load_rs()
        self.assertEqual(
            list(inspect.signature(rs.run_day).parameters),
            ["run_date", "retrain", "register_fallback", "out_base"])


if __name__ == "__main__":
    unittest.main()
