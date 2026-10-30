import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operations" / "scripts"))

import tweet_guard as tg

FIX = Path(__file__).resolve().parent / "fixtures" / "tweet_guard"


def runner(crontab="crontab_empty.txt", launchctl="launchctl_clean.txt", jobs=None):
    jobs = jobs or {}

    def run(argv):
        if argv[0] == "crontab":
            name = crontab
        elif argv[1] == "list":
            name = launchctl
        else:
            name = jobs.get(argv[2].rsplit("/", 1)[-1])
        if name is None:
            return None, "unreadable"
        return (FIX / name).read_text(), ""
    return run


OBS = "com.hazeydata.tpcr-tweet-observed"
PRED = "com.hazeydata.tpcr-tweet-predicted"
REPO_JOBS = {OBS: "print_observed_repo.txt", PRED: "print_predicted_repo.txt"}
LOCAL_JOBS = {OBS: "print_observed_local.txt", PRED: "print_predicted_local.txt"}


def reason(r):
    with unittest.TestCase().assertRaises(tg.GuardBlocked) as cm:
        tg.preflight(r)
    return cm.exception


class Config(unittest.TestCase):
    def test_labels_are_the_real_launchd_jobs(self):
        _, labels, _ = tg.load_config()
        self.assertEqual(sorted(labels), [OBS, PRED])


class CronSide(unittest.TestCase):
    def test_commented_allows(self):
        tg.preflight(runner(crontab="crontab_commented.txt"))

    def test_empty_allows(self):
        tg.preflight(runner(crontab="crontab_empty.txt"))

    def test_uncommented_blocks(self):
        self.assertEqual(reason(runner(crontab="crontab_uncommented.txt")).reason, "legacy_cron_active")

    def test_mixed_blocks_on_observed_only(self):
        e = reason(runner(crontab="crontab_mixed.txt"))
        self.assertEqual(e.reason, "legacy_cron_active")
        self.assertIn("wti_observed_tweet.py", e.detail)
        self.assertNotIn("wti_predicted_tweet.py", e.detail)

    def test_unreadable_fails_closed(self):
        self.assertEqual(reason(runner(crontab=None)).reason, "crontab_unreadable")

    def test_real_crontab_missing_binary_fails_closed(self):
        with mock.patch.object(tg.subprocess, "run", side_effect=FileNotFoundError("crontab")):
            self.assertEqual(reason(tg._run).reason, "crontab_unreadable")

    def test_cli_exit_code_and_reason(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = tg.main(["check"], runner(crontab="crontab_uncommented.txt"))
        self.assertEqual(rc, 3)
        self.assertIn("legacy_cron_active", err.getvalue())


class LaunchdSide(unittest.TestCase):
    def test_jobs_on_repo_scripts_allow(self):
        tg.preflight(runner(launchctl="launchctl_loaded.txt", jobs=REPO_JOBS))

    def test_jobs_not_loaded_allow_without_reading_them(self):
        tg.preflight(runner(launchctl="launchctl_clean.txt", jobs={}))

    def test_job_on_local_script_blocks(self):
        e = reason(runner(launchctl="launchctl_loaded.txt", jobs=LOCAL_JOBS))
        self.assertEqual(e.reason, "launchd_runs_local_script")
        self.assertIn(f"{OBS} -> wti_observed_tweet_local.py", e.detail)
        self.assertIn(f"{PRED} -> wti_predicted_tweet_local.py", e.detail)

    def test_one_job_still_local_blocks_naming_only_it(self):
        e = reason(runner(launchctl="launchctl_loaded.txt",
                          jobs={OBS: "print_observed_repo.txt", PRED: "print_predicted_local.txt"}))
        self.assertEqual(e.reason, "launchd_runs_local_script")
        self.assertIn(PRED, e.detail)
        self.assertNotIn(OBS, e.detail)

    def test_label_match_is_exact(self):
        tg.preflight(runner(launchctl="launchctl_observed_only.txt",
                            jobs={OBS: "print_observed_repo.txt", OBS + "-old": "print_observed_local.txt"}))

    def test_old_local_labels_are_not_tweet_jobs(self):
        tg.preflight(runner(launchctl="launchctl_old_labels.txt", jobs={}))

    def test_list_unreadable_fails_closed(self):
        self.assertEqual(reason(runner(launchctl=None)).reason, "launchctl_unreadable")

    def test_job_unreadable_fails_closed(self):
        e = reason(runner(launchctl="launchctl_loaded.txt", jobs={OBS: "print_observed_repo.txt"}))
        self.assertEqual(e.reason, "launchctl_unreadable")
        self.assertIn(PRED, e.detail)

    def test_cron_checked_before_launchd(self):
        e = reason(runner(crontab="crontab_uncommented.txt", launchctl="launchctl_loaded.txt", jobs=LOCAL_JOBS))
        self.assertEqual(e.reason, "legacy_cron_active")

    def test_reads_job_from_gui_domain_of_current_user(self):
        seen = []

        def run(argv):
            seen.append(argv)
            return runner(launchctl="launchctl_loaded.txt", jobs=REPO_JOBS)(argv)
        tg.preflight(run)
        self.assertIn(["launchctl", "print", f"gui/{os.getuid()}/{OBS}"], seen)
        self.assertIn(["launchctl", "print", f"gui/{os.getuid()}/{PRED}"], seen)


class Status(unittest.TestCase):
    def test_status_switched(self):
        out = tg.status(runner(launchctl="launchctl_loaded.txt", jobs=REPO_JOBS))
        self.assertIn("legacy_cron: inactive (0 line(s))", out)
        self.assertIn("launchd: 2 of 2 tweet job(s) loaded", out)
        self.assertIn(f"  {OBS}: runs repo script", out)
        self.assertIn(f"  {PRED}: runs repo script", out)
        self.assertIn("allowed_post: yes", out)

    def test_status_still_local(self):
        out = tg.status(runner(launchctl="launchctl_loaded.txt", jobs=LOCAL_JOBS))
        self.assertIn(f"  {OBS}: runs local script (wti_observed_tweet_local.py)", out)
        self.assertIn("allowed_post: no", out)

    def test_status_not_loaded(self):
        out = tg.status(runner())
        self.assertIn("launchd: 0 of 2 tweet job(s) loaded", out)
        self.assertIn(f"  {OBS}: not loaded", out)
        self.assertIn("allowed_post: yes", out)

    def test_status_cron_active_blocks(self):
        out = tg.status(runner(crontab="crontab_mixed.txt", launchctl="launchctl_loaded.txt", jobs=REPO_JOBS))
        self.assertIn("legacy_cron: active (1 line(s))", out)
        self.assertIn("allowed_post: no", out)

    def test_status_unreadable_allows_nothing(self):
        out = tg.status(runner(crontab=None, launchctl=None))
        self.assertIn("legacy_cron: unreadable", out)
        self.assertIn("launchd: unreadable", out)
        self.assertIn("allowed_post: no", out)

    def test_cli_status_exit_zero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tg.main(["status"], runner())
        self.assertEqual(rc, 0)
        self.assertIn("allowed_post: yes", buf.getvalue())

    def test_cli_unknown_command(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(tg.main(["check-local"], runner()), 2)


class DryRunSwitch(unittest.TestCase):
    def args(self, flag):
        return mock.Mock(dry_run=flag)

    def test_env_unset_follows_flag(self):
        import tweet_v2
        self.assertFalse(tweet_v2.dry_run_requested(self.args(False), {}))
        self.assertTrue(tweet_v2.dry_run_requested(self.args(True), {}))

    def test_env_one_forces_dry(self):
        import tweet_v2
        self.assertTrue(tweet_v2.dry_run_requested(self.args(False), {"TPCR_TWEET_DRY_RUN": "1"}))

    def test_env_zero_is_live(self):
        import tweet_v2
        self.assertFalse(tweet_v2.dry_run_requested(self.args(False), {"TPCR_TWEET_DRY_RUN": "0"}))

    def test_flag_wins_over_env_zero(self):
        import tweet_v2
        self.assertTrue(tweet_v2.dry_run_requested(self.args(True), {"TPCR_TWEET_DRY_RUN": "0"}))


class RepoScriptsGuarded(unittest.TestCase):
    def _run_script(self, name, launchctl_fixture, jobs=None, extra_env=None):
        script = ROOT / "operations" / "scripts" / name
        bindir = Path(self.tmp) / "bin"
        bindir.mkdir(exist_ok=True)
        cases = "".join(f"  *'/{label}') cat '{FIX / fx}' ;;\n" for label, fx in (jobs or {}).items())
        fake = bindir / "launchctl"
        fake.write_text(f"#!/bin/sh\nif [ \"$1\" = list ]; then cat '{FIX / launchctl_fixture}'; exit 0; fi\n"
                        f"case \"$2\" in\n{cases}  *) exit 113 ;;\nesac\n")
        fake.chmod(0o755)
        cron = bindir / "crontab"
        cron.write_text("#!/bin/sh\nexit 0\n")
        cron.chmod(0o755)
        stub = Path(self.tmp) / "stubs"
        (stub / "requests_oauthlib").mkdir(parents=True, exist_ok=True)
        (stub / "requests_oauthlib" / "__init__.py").write_text("OAuth1 = None\n")
        (stub / "requests.py").write_text(
            "def _boom(*a, **k):\n    raise SystemExit('network call attempted')\npost = get = _boom\nclass Session:\n    request = _boom\n"
        )
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": self.tmp, "PYTHONPATH": str(stub), **(extra_env or {})}
        return subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=60)

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_both_refuse_while_jobs_still_run_local_scripts(self):
        for name in ("wti_predicted_tweet.py", "wti_observed_tweet.py"):
            r = self._run_script(name, "launchctl_loaded.txt", LOCAL_JOBS)
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertIn("launchd_runs_local_script", r.stderr)

    def test_passes_guard_when_jobs_run_repo_scripts(self):
        for name in ("wti_predicted_tweet.py", "wti_observed_tweet.py"):
            r = self._run_script(name, "launchctl_loaded.txt", REPO_JOBS, {"TPCR_TWEET_DRY_RUN": "0"})
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("TWITTER_CONSUMER_KEY not set", r.stderr)

    def test_env_dry_run_skips_guard_and_credentials(self):
        for name in ("wti_predicted_tweet.py", "wti_observed_tweet.py"):
            r = self._run_script(name, "launchctl_loaded.txt", LOCAL_JOBS, {"TPCR_TWEET_DRY_RUN": "1"})
            self.assertIn("DRY RUN: building only", r.stdout)
            self.assertNotIn("TWITTER_CONSUMER_KEY not set", r.stderr)
            self.assertNotIn("launchd_runs_local_script", r.stderr)
            self.assertNotEqual(r.returncode, 3)


if __name__ == "__main__":
    unittest.main()


# TPCR-TWEET-RESTYLE-CUTOVER: the suites above test the new-format path, so they run as if the old format had
# already retired (config/tweet_format.json window moved to the past). The window itself is tested in
# tests/test_tweet_format.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "operations" / "scripts"))
import tweet_format as _tf  # noqa: E402
from datetime import datetime as _dtm, timezone as _tzu  # noqa: E402
_PAST_WINDOW = mock.patch.multiple(_tf, FIRST_NEW_RUN=_dtm(2000, 1, 1, tzinfo=_tzu.utc), OLD_RETIRES=_dtm(2000, 1, 2, tzinfo=_tzu.utc))


def setUpModule():
    _PAST_WINDOW.start()


def tearDownModule():
    _PAST_WINDOW.stop()
