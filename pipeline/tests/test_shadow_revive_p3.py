"""TPCR-SHADOW-REVIVE-001 Phase 3 (AC9-AC11): scheduled shadow job.

Everything runs in tmp dirs with a fake clock/sleep. No network, no subprocesses,
no real scraper or pipeline files.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import plistlib
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
JOB_SCRIPT = REPO_ROOT / "scripts" / "shadow_job.py"
SHADOW_SCRIPT = REPO_ROOT / "scripts" / "rolling_shadow.py"
PLIST = REPO_ROOT / "launchd" / "com.hazeydata.tpcr-shadow.plist"

RUN_DATE = "2026-10-24"
EVAL_DATE = "2026-10-23"
PREV_DATE = "2026-10-22"


def load_job():
    spec = importlib.util.spec_from_file_location("shadow_job_p3_under_test", JOB_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClock:
    """monotonic clock that only advances through sleep()."""

    def __init__(self, on_sleep=None):
        self.t = 0.0
        self.sleeps = []
        self.on_sleep = on_sleep

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))


class JobCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name) / "pipe"
        self.comp = self.base / "competition"
        self.sj = load_job()
        self.rs = self.sj.rs
        self.addCleanup(self.rs.configure)  # back to env / home defaults
        patcher = mock.patch.object(self.sj.os, "nice", return_value=0)
        self.nice = patcher.start()
        self.addCleanup(patcher.stop)

        (self.base / "logs").mkdir(parents=True)
        (self.base / "state").mkdir()
        for sub in ("scores", "summaries"):
            (self.comp / sub).mkdir(parents=True)
        self.registry = {
            "settings": {"min_shadow_days": 7, "auto_retire_if_worse_by": 1.0},
            "challengers": [{"name": "c1", "status": "active", "shadow_days": 0}],
            "retired": [],
        }
        (self.comp / "challenger_registry.json").write_text(json.dumps(self.registry))
        self.calls = 0

    # -- helpers --
    def marker(self, status="done"):
        (self.base / "logs" / f"pipeline_metrics_{RUN_DATE}.json").write_text(json.dumps({"status": status}))

    def lock_file(self):
        return self.base / "state" / "processing_queue_times.lock"

    def hold_lock(self):
        self.lock_file().write_text("PID: 1\n")

    def fake_run_day(self, mid_run=None, raise_exc=None):
        """Stand-in for rolling_shadow.run_day: real registry update, files staged in out_base."""
        rs = self.rs

        def run_day(run_date, retrain=False, register_fallback=False, out_base=None, post=True):
            self.calls += 1
            self.assertFalse(post, "the job must never post to Discord")
            self.assertNotEqual(Path(out_base), rs.COMPETITION_BASE, "outputs must be staged")
            if mid_run:
                mid_run()
            if raise_exc:
                raise raise_exc
            eval_date = (run_date - __import__("datetime").timedelta(days=1)).isoformat()
            registry = rs.load_registry()
            metrics = {"n_matched": 10, "baseline_mae": 5.0, "challenger_mae": 4.0}
            rs.update_registry(registry, "c1", metrics, eval_date)
            rs.write_registry(registry, Path(out_base) / "challenger_registry.json")
            scores = pd.DataFrame({
                "date": [eval_date] * 3, "challenger_id": ["c1"] * 3, "entity_code": ["MK01"] * 3,
                "slot": [1, 2, 3], "actual": [1.0, 2.0, 3.0], "prod_pred": [1.5] * 3,
                "chal_pred": [1.2] * 3, "is_fallback_entity": [False] * 3,
            })
            (Path(out_base) / "scores").mkdir(parents=True, exist_ok=True)
            scores.to_parquet(Path(out_base) / "scores" / f"{eval_date}.parquet", index=False)
            summary = {"date": eval_date, "matched_pairs": 10, "forecast_entities": 1,
                       "coverage_pct": 100.0, "challengers": {}, "run_status": "ok", "errors": []}
            (Path(out_base) / "summaries").mkdir(parents=True, exist_ok=True)
            (Path(out_base) / "summaries" / f"{eval_date}.json").write_text(json.dumps(summary))
            return summary

        return run_day

    def run_job(self, clock=None, extra=(), run_day=None):
        clock = clock or FakeClock()
        patcher = mock.patch.object(self.sj.rs, "run_day", run_day or self.fake_run_day())
        patcher.start()
        self.addCleanup(patcher.stop)
        argv = ["--date", RUN_DATE, "--pipeline-base", str(self.base), "--tpcr-dir", str(REPO_ROOT), *extra]
        return self.sj.main(argv, sleep=clock.sleep, clock=clock.clock), clock

    def summary(self, d=EVAL_DATE):
        return json.loads((self.comp / "summaries" / f"{d}.json").read_text())

    def registry_bytes(self):
        return (self.comp / "challenger_registry.json").read_bytes()

    def scores_files(self):
        return sorted(p.name for p in (self.comp / "scores").iterdir())


class TestMarker(JobCase):
    def test_marker_done_runs_and_commits(self):
        self.marker("done")
        code, clock = self.run_job()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(self.summary()["run_status"], "ok")
        self.assertEqual(self.scores_files(), [f"{EVAL_DATE}.parquet"])
        self.assertEqual(json.loads(self.registry_bytes())["challengers"][0]["shadow_days"], 1)
        self.nice.assert_called_once_with(10)
        self.assertFalse((self.comp / "staging" / EVAL_DATE).exists())

    def test_marker_appears_after_wait(self):
        clock = FakeClock(on_sleep=lambda n: self.marker("done") if n == 3 else None)
        code, clock = self.run_job(clock)
        self.assertEqual(code, 0)
        self.assertEqual(len(clock.sleeps), 3)
        self.assertEqual(self.calls, 1)

    def test_marker_missing_30_min_skips(self):
        code, clock = self.run_job()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, 0)
        self.assertGreaterEqual(clock.t, 30 * 60)
        self.assertLess(clock.t, 31 * 60 + 1)
        s = self.summary()
        self.assertEqual(s["run_status"], "skipped_no_marker")
        self.assertEqual(s["errors"], [])
        self.assertEqual(self.scores_files(), [])
        self.assertEqual(json.loads(self.registry_bytes()), self.registry)

    def test_pipeline_failed_or_partial_skips_without_waiting(self):
        for status in ("failed", "sync_partial"):
            with self.subTest(status=status):
                self.marker(status)
                code, clock = self.run_job()
                self.assertEqual(code, 0)
                self.assertEqual(clock.sleeps, [])
                self.assertEqual(self.summary()["run_status"], "skipped_pipeline")
                self.assertEqual(self.scores_files(), [])
        self.assertEqual(self.calls, 0)
        self.assertEqual(sorted(p.name for p in (self.comp / "summaries").iterdir()), [f"{EVAL_DATE}.json"])

    def test_marker_wait_is_configurable(self):
        _, clock = self.run_job(extra=["--max-marker-wait-min", "2"])
        self.assertLess(clock.t, 3 * 60)
        self.assertEqual(self.summary()["run_status"], "skipped_no_marker")


class TestScraperLock(JobCase):
    def test_lock_held_then_released_runs(self):
        self.marker()
        self.hold_lock()
        clock = FakeClock(on_sleep=lambda n: self.lock_file().unlink() if n == 2 else None)
        code, clock = self.run_job(clock)
        self.assertEqual(code, 0)
        self.assertEqual(len(clock.sleeps), 2)
        self.assertEqual(self.summary()["run_status"], "ok")
        self.assertEqual(self.scores_files(), [f"{EVAL_DATE}.parquet"])

    def test_lock_held_throughout_skips_with_nothing_else_written(self):
        self.marker()
        self.hold_lock()
        before = self.registry_bytes()
        code, clock = self.run_job()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, 0)
        self.assertGreaterEqual(clock.t, 20 * 60)
        s = self.summary()
        self.assertEqual(s["run_status"], "skipped_lock")
        self.assertEqual(s["errors"], [])
        self.assertEqual(self.scores_files(), [])
        self.assertEqual(self.registry_bytes(), before)
        self.assertTrue(self.lock_file().exists(), "the scraper lock must not be touched")

    def test_lock_wait_option(self):
        self.marker()
        self.hold_lock()
        _, clock = self.run_job(extra=["--lock-wait-min", "3"])
        self.assertLess(clock.t, 4 * 60)

    def test_stale_lock_is_not_held(self):
        self.hold_lock()
        old = os.stat(self.lock_file()).st_mtime - 90000
        os.utime(self.lock_file(), (old, old))
        self.assertFalse(self.sj.scraper_lock_held(self.base))
        self.hold_lock()
        os.utime(self.lock_file())
        self.assertTrue(self.sj.scraper_lock_held(self.base))

    def test_lock_taken_mid_run_discards_staged_outputs(self):
        self.marker()
        before = self.registry_bytes()
        code, _ = self.run_job(run_day=self.fake_run_day(mid_run=self.hold_lock))
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.summary()["run_status"], "skipped_lock")
        self.assertEqual(self.scores_files(), [])
        self.assertEqual(self.registry_bytes(), before)
        self.assertFalse((self.comp / "staging" / EVAL_DATE).exists())


class TestFailureAndIdempotence(JobCase):
    def test_exception_writes_failed_and_keeps_previous_days(self):
        self.marker()
        prev_scores = self.comp / "scores" / f"{PREV_DATE}.parquet"
        prev_summary = self.comp / "summaries" / f"{PREV_DATE}.json"
        pd.DataFrame({"date": [PREV_DATE], "actual": [1.0]}).to_parquet(prev_scores, index=False)
        prev_summary.write_text(json.dumps({"run_status": "ok", "date": PREV_DATE}))
        before = [prev_scores.read_bytes(), prev_summary.read_bytes(), self.registry_bytes()]

        code, _ = self.run_job(run_day=self.fake_run_day(raise_exc=ValueError("boom")))

        self.assertEqual(code, 1)
        s = self.summary()
        self.assertEqual(s["run_status"], "failed")
        self.assertEqual(s["errors"], [{"type": "ValueError", "message": "boom"}])
        self.assertEqual([prev_scores.read_bytes(), prev_summary.read_bytes(), self.registry_bytes()], before)
        self.assertEqual(self.scores_files(), [f"{PREV_DATE}.parquet"])

    def test_failed_run_status_from_run_day_exits_nonzero(self):
        self.marker()
        inner = self.fake_run_day()

        def run_day(*a, **kw):
            summary = inner(*a, **kw)
            summary["run_status"] = "failed"
            summary["errors"] = ["c1: forecast failed"]
            (Path(kw["out_base"]) / "summaries" / f"{EVAL_DATE}.json").write_text(json.dumps(summary))
            return summary

        code, _ = self.run_job(run_day=run_day)
        self.assertEqual(code, 1)
        self.assertEqual(self.summary()["run_status"], "failed")

    def test_rerun_same_date_is_idempotent(self):
        self.marker()
        self.run_job()
        rows_1 = len(pd.read_parquet(self.comp / "scores" / f"{EVAL_DATE}.parquet"))
        self.run_job()
        rows_2 = len(pd.read_parquet(self.comp / "scores" / f"{EVAL_DATE}.parquet"))
        self.assertEqual(rows_1, rows_2)
        self.assertEqual(self.calls, 2)
        c1 = json.loads(self.registry_bytes())["challengers"][0]
        self.assertEqual(c1["shadow_days"], 1)
        self.assertEqual([m["date"] for m in c1["daily_metrics"]], [EVAL_DATE])

    def test_update_registry_counts_distinct_matched_dates(self):
        reg = self.rs.normalize_registry(json.loads(json.dumps(self.registry)))
        good = {"n_matched": 5, "baseline_mae": 5.0, "challenger_mae": 4.0}
        self.rs.update_registry(reg, "c1", good, "2026-10-21")
        self.rs.update_registry(reg, "c1", good, "2026-10-22")
        self.rs.update_registry(reg, "c1", good, "2026-10-22")  # rerun
        self.rs.update_registry(reg, "c1", {"n_matched": 0}, "2026-10-23")  # zero-pair day
        c1 = reg["challengers"][0]
        self.assertEqual(c1["shadow_days"], 2)
        self.assertEqual([m["date"] for m in c1["daily_metrics"]], ["2026-10-21", "2026-10-22"])

    def test_update_registry_without_date_keeps_old_behavior(self):
        reg = self.rs.normalize_registry(json.loads(json.dumps(self.registry)))
        good = {"n_matched": 5, "baseline_mae": 5.0, "challenger_mae": 4.0}
        self.rs.update_registry(reg, "c1", good)
        self.rs.update_registry(reg, "c1", good)
        self.assertEqual(reg["challengers"][0]["shadow_days"], 2)


class TestConcurrencyAndDryRun(JobCase):
    def test_second_job_exits_cleanly_while_first_holds_lock(self):
        self.marker()
        held = self.sj.take_job_lock(self.comp)
        self.assertIsNotNone(held)
        try:
            code, clock = self.run_job()
        finally:
            held.close()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, 0)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(list((self.comp / "summaries").iterdir()), [])
        self.assertEqual(self.scores_files(), [])
        # lock is free again once the holder closes
        again = self.sj.take_job_lock(self.comp)
        self.assertIsNotNone(again)
        again.close()

    def test_job_lock_lives_under_competition_locks(self):
        fd = self.sj.take_job_lock(self.comp)
        self.addCleanup(fd.close)
        self.assertTrue((self.comp / "locks" / "shadow_job.lock").exists())

    def test_dry_run_writes_nothing(self):
        code, clock = self.run_job(extra=["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(self.calls, 0)
        self.assertEqual(list((self.comp / "summaries").iterdir()), [])
        self.assertFalse((self.comp / "locks").exists())


class TestPlist(unittest.TestCase):
    def test_plist_parses_and_schedules_0620(self):
        data = plistlib.loads(PLIST.read_bytes())
        self.assertEqual(data["Label"], "com.hazeydata.tpcr-shadow")
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 6, "Minute": 20})
        self.assertIs(data["RunAtLoad"], False)
        self.assertEqual(data["Nice"], 10)
        self.assertIs(data["LowPriorityIO"], True)
        self.assertEqual(data["ProgramArguments"], [
            "__HOME__/theme-park-crowd-report/.venv/bin/python",
            "__HOME__/theme-park-crowd-report/scripts/shadow_job.py",
        ])
        for key in ("StandardOutPath", "StandardErrorPath"):
            self.assertTrue(data[key].startswith("__HOME__/hazeydata/pipeline/competition/logs/"))

    def test_plist_has_no_absolute_user_paths_or_ips(self):
        text = PLIST.read_text()
        for bad in ("/" + "workspace", "/home/", "/Users/"):
            self.assertNotIn(bad, text)
        self.assertIsNone(re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text))


class TestNoSharedLock(unittest.TestCase):
    """AC11: the shadow code never takes a pipeline or scraper lock."""

    FORBIDDEN_CALLS = {"acquire_lock", "release_lock"}
    FORBIDDEN_MODULES = ("get_tp_wait_time_data_from_s3", "get_wait_times_from_queue_times",
                         "pipeline.steps", "pipeline.pipeline", "src.utils.pipeline_status")
    WRITE_CALLS = {"open", "unlink", "touch", "write_text", "write_bytes", "replace", "rename",
                   "remove", "flock", "lockf", "rmtree", "atomic_write_json", "atomic_write_text"}
    SCRAPER_HINTS = ("scraper_lock", "processing_queue_times", "lock_file_name", "queue_times")

    def trees(self):
        for path in (JOB_SCRIPT, SHADOW_SCRIPT):
            yield path.name, ast.parse(path.read_text())

    @staticmethod
    def call_name(node):
        f = node.func
        return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None

    def test_no_lock_helper_calls_or_imports(self):
        for name, tree in self.trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    self.assertNotIn(self.call_name(node), self.FORBIDDEN_CALLS, name)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                    for mod in mods:
                        for bad in self.FORBIDDEN_MODULES:
                            self.assertNotIn(bad, mod, f"{name} imports {mod}")
                if isinstance(node, (ast.Name, ast.Attribute)):
                    ident = node.id if isinstance(node, ast.Name) else node.attr
                    self.assertNotIn(ident, self.FORBIDDEN_CALLS, name)

    def test_scraper_lock_never_passed_to_a_writing_call(self):
        for name, tree in self.trees():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                call = self.call_name(node)
                if call not in self.WRITE_CALLS:
                    continue
                args = " ".join(ast.unparse(a) for a in [*node.args, *[k.value for k in node.keywords]])
                if isinstance(node.func, ast.Attribute):
                    args += " " + ast.unparse(node.func.value)
                for hint in self.SCRAPER_HINTS:
                    self.assertNotIn(hint, args.lower(), f"{name}: {call}({args})")

    def test_only_lock_is_the_shadow_flock(self):
        text = JOB_SCRIPT.read_text()
        flocks = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call) and self.call_name(n) == "flock"]
        self.assertEqual(len(flocks), 1)
        self.assertEqual(ast.unparse(flocks[0].args[0]), "fd")
        self.assertIn('SHADOW_LOCK_NAME = "shadow_job.lock"', text)
        self.assertNotIn("flock", SHADOW_SCRIPT.read_text())

    def test_scraper_lock_name_matches_the_scraper(self):
        scraper = (REPO_ROOT / "src" / "get_wait_times_from_queue_times.py").read_text()
        m = re.search(r'LOCK_FILE_NAME\s*=\s*"([^"]+)"', scraper)
        self.assertIsNotNone(m)
        self.assertIn(f'SCRAPER_LOCK_NAME = "{m.group(1)}"', JOB_SCRIPT.read_text())


class TestStatics(unittest.TestCase):
    def test_no_absolute_user_paths_in_new_files(self):
        for path in (JOB_SCRIPT, REPO_ROOT / "launchd" / "README.md"):
            text = path.read_text()
            for bad in ("/" + "workspace", "/home/", "/Users/"):
                self.assertNotIn(bad, text, path.name)


if __name__ == "__main__":
    unittest.main()
