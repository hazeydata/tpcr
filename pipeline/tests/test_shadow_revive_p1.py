"""TPCR-SHADOW-REVIVE-001 Phase 1 tests: local run, shadow_days rule, xgb-dow retired.

No xgboost, no network, no real pipeline data. Runs under pytest or unittest.
"""

import importlib.util
import json
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "rolling_shadow.py"
COMPETITION_DIR = REPO_ROOT / "pipeline" / "competition"


def load_rolling_shadow():
    spec = importlib.util.spec_from_file_location("rolling_shadow_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = load_rolling_shadow()

SETTINGS = {"min_shadow_days": 7, "auto_retire_if_worse_by": 1.0}


def make_registry(*names):
    return {
        "settings": dict(SETTINGS),
        "challengers": [{"name": n, "status": "shadow", "shadow_days": 0} for n in names],
        "retired": [],
    }


def good_metrics(n_matched=5):
    return {"n_matched": n_matched, "baseline_mae": 5.0, "challenger_mae": 4.0, "n_entities": 3}


class NoRemoteReferencesTest(unittest.TestCase):
    def test_grep_is_zero(self):
        pattern = re.compile(r"wilma|ssh|192\.168|@[0-9]", re.IGNORECASE)
        files = [SCRIPT] + sorted(COMPETITION_DIR.rglob("*.py"))
        self.assertTrue(len(files) > 1)
        hits = [
            f"{f.name}:{i}: {line.strip()}"
            for f in files
            for i, line in enumerate(f.read_text().splitlines(), 1)
            if pattern.search(line)
        ]
        self.assertEqual(hits, [])

    def test_requests_not_imported_at_module_load(self):
        self.assertFalse(hasattr(rs, "requests"))

    def test_config_defaults(self):
        from pipeline.competition import config

        home = Path.home()
        self.assertEqual(config.DEFAULT_OUTPUT_BASE, home / "hazeydata" / "pipeline")
        self.assertEqual(config.DEFAULT_COMPETITION_BASE, home / "hazeydata" / "pipeline" / "competition")


class PathsTest(unittest.TestCase):
    def test_defaults_under_home(self):
        base, repo, py = rs.default_paths({})
        home = Path.home()
        self.assertEqual(base, home / "hazeydata" / "pipeline")
        self.assertEqual(base / "competition", home / "hazeydata" / "pipeline" / "competition")
        self.assertEqual(repo, home / "theme-park-crowd-report")
        self.assertEqual(py, home / "theme-park-crowd-report" / ".venv" / "bin" / "python")

    def test_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"TPCR_PIPELINE_BASE": f"{d}/pipe", "TPCR_DIR": f"{d}/repo"}
            base, repo, py = rs.default_paths(env)
            self.assertEqual(base, Path(d) / "pipe")
            self.assertEqual(repo, Path(d) / "repo")
            self.assertEqual(py, Path(d) / "repo" / ".venv" / "bin" / "python")

    def test_configure_override(self):
        mod = load_rolling_shadow()
        with tempfile.TemporaryDirectory() as d:
            mod.configure(pipeline_base=d, tpcr_dir=f"{d}/repo", python=f"{d}/py")
            self.assertEqual(mod.COMPETITION_BASE, Path(d) / "competition")
            self.assertEqual(mod.REGISTRY_PATH, Path(d) / "competition" / "challenger_registry.json")
            self.assertEqual(mod.PYTHON, Path(d) / "py")


class RegistryIOTest(unittest.TestCase):
    def test_round_trip_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "competition" / "challenger_registry.json"
            reg = make_registry("chal-a")
            rs.write_registry(reg, path)
            self.assertEqual(json.loads(path.read_text()), reg)
            self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])  # no tmp left
            reg["challengers"][0]["shadow_days"] = 3
            rs.write_registry(reg, path)
            self.assertEqual(rs.load_registry(path)["challengers"][0]["shadow_days"], 3)

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(rs.load_registry(Path(d) / "nope.json"))

    def test_normalize_shape_and_idempotent(self):
        reg = make_registry("chal-a", "xgb-dow")
        reg["retired"].append({"name": "old", "status": "auto_retired"})
        once = rs.normalize_registry(reg)
        snapshot = json.dumps(once, sort_keys=True)
        self.assertEqual(json.dumps(rs.normalize_registry(once), sort_keys=True), snapshot)

        by_name = {c["name"]: c for c in once["challengers"]}
        a = by_name["chal-a"]
        self.assertEqual((a["id"], a["status"]), ("chal-a", "active"))
        for key in ("trained_at", "entity_count", "notes"):
            self.assertIn(key, a)
        self.assertEqual(by_name["xgb-dow"]["status"], "retired")
        self.assertEqual(by_name["xgb-dow"]["notes"], "in production via #470")
        self.assertEqual(once["retired"][0]["status"], "retired")


class ShadowDaysTest(unittest.TestCase):
    def days(self, reg):
        return reg["challengers"][0]["shadow_days"]

    def test_matched_pairs_helper(self):
        self.assertEqual(rs.matched_pairs(good_metrics(5)), 5)
        self.assertEqual(rs.matched_pairs(good_metrics(0)), 0)
        self.assertEqual(rs.matched_pairs("no_data"), 0)
        self.assertEqual(rs.matched_pairs(None), 0)
        self.assertEqual(rs.matched_pairs({"baseline_mae": 1.0}), 0)

    def test_non_matching_days_do_not_increment(self):
        for metrics in (good_metrics(0), "no_data", None):
            reg = make_registry("chal-a")
            rs.update_registry(reg, "chal-a", metrics)
            self.assertEqual(self.days(reg), 0, metrics)
            self.assertNotIn("daily_metrics", reg["challengers"][0])

    def test_five_pair_day_increments(self):
        reg = make_registry("chal-a")
        rs.update_registry(reg, "chal-a", good_metrics(5))
        self.assertEqual(self.days(reg), 1)
        self.assertEqual(len(reg["challengers"][0]["daily_metrics"]), 1)

    def test_zero_pair_day_between_real_days(self):
        reg = make_registry("chal-a")
        for m in (good_metrics(5), good_metrics(0), "no_data", None, good_metrics(2)):
            rs.update_registry(reg, "chal-a", m)
        self.assertEqual(self.days(reg), 2)
        self.assertEqual(len(reg["challengers"][0]["daily_metrics"]), 2)


class MainRunListTest(unittest.TestCase):
    def test_retired_xgb_dow_never_runs(self):
        calls = {"forecast": [], "archive": [], "evaluate": []}

        def fake_forecast(ch):
            calls["forecast"].append(ch["name"])
            return True

        def fake_archive(ch, run_date):
            calls["archive"].append(ch["name"])
            return True

        def fake_evaluate(ch, run_date):
            calls["evaluate"].append(ch["name"])
            return good_metrics(5)

        def fake_retrain(ch, run_date):  # Phase 2 retrain step: no model dir in this sandbox
            return {"trained_at": "2026-10-24T00:00:00+00:00", "entity_count": 3, "eligible_count": 3,
                    "trained_entities": ["MK01", "MK02", "MK03"], "skipped": {}}

        mod = load_rolling_shadow()
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "pipeline"
            registry_path = base / "competition" / "challenger_registry.json"
            # xgb-dow arrives as a legacy "shadow" entry; the run must retire it.
            mod.write_registry(make_registry("xgb-dow", "chal-a"), registry_path)

            with mock.patch.object(mod, "retrain_challenger", fake_retrain), \
                    mock.patch.object(mod, "production_trained_entities",
                                      lambda run_date, base=None: {"MK01", "MK02", "MK03"}), \
                    mock.patch.object(mod, "generate_forecasts", fake_forecast), \
                    mock.patch.object(mod, "archive_predictions", fake_archive), \
                    mock.patch.object(mod, "evaluate_previous_day", fake_evaluate), \
                    mock.patch.object(mod, "post_discord", lambda msg: None), \
                    mock.patch.object(mod, "load_env", lambda: None):
                mod.main(["--pipeline-base", str(base), "--tpcr-dir", str(Path(d) / "repo"),
                          "--run-date", date(2026, 10, 24).isoformat()])

            for step, names in calls.items():
                self.assertEqual(names, ["chal-a"], step)

            saved = json.loads(registry_path.read_text())
            by_name = {c["name"]: c for c in saved["challengers"]}
            self.assertEqual(by_name["xgb-dow"]["status"], "retired")
            self.assertEqual(by_name["xgb-dow"]["shadow_days"], 0)
            self.assertEqual(by_name["chal-a"]["shadow_days"], 1)
            self.assertEqual(by_name["chal-a"]["status"], "active")
            for sub in ("shadow", "scores", "summaries"):
                self.assertTrue((base / "competition" / sub).is_dir(), sub)

    def test_all_retired_does_nothing_but_persists_normalization(self):
        mod = load_rolling_shadow()
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "pipeline"
            registry_path = base / "competition" / "challenger_registry.json"
            mod.write_registry(make_registry("xgb-dow"), registry_path)
            boom = mock.Mock(side_effect=AssertionError("must not run"))
            with mock.patch.object(mod, "generate_forecasts", boom), \
                    mock.patch.object(mod, "post_discord", boom), \
                    mock.patch.object(mod, "load_env", lambda: None):
                mod.main(["--pipeline-base", str(base), "--tpcr-dir", str(Path(d) / "repo")])
            boom.assert_not_called()
            saved = json.loads(registry_path.read_text())
            self.assertEqual(saved["challengers"][0]["status"], "retired")


class ChallengerRegistryTest(unittest.TestCase):
    def test_xgb_dow_module_retired_and_filtered(self):
        from pipeline.competition.challengers import xgb_dow
        from pipeline.competition.registry import ChallengerDefinition, ChallengerRegistry

        self.assertEqual(xgb_dow.STATUS, "retired")
        reg = ChallengerRegistry()
        reg.challengers["xgb-dow"] = ChallengerDefinition("xgb-dow", xgb_dow)
        stub = SimpleNamespace(DESCRIPTION="live one", STATUS="active")
        reg.challengers["live"] = ChallengerDefinition("live", stub)
        self.assertEqual(reg.challengers["xgb-dow"].status, "retired")
        self.assertEqual(reg.challengers["live"].status, "active")
        self.assertEqual(reg.active_challengers(), ["live"])


if __name__ == "__main__":
    unittest.main()
