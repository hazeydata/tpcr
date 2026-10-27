import io
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


def runner(crontab=None, launchctl=None):
    def run(argv):
        name = {"crontab": crontab, "launchctl": launchctl}[argv[0]]
        if name is None:
            return None, "unreadable"
        return (FIX / name).read_text(), ""
    return run


class CronSide(unittest.TestCase):
    def test_commented_allows_local(self):
        tg.preflight_local(runner(crontab="crontab_commented.txt"))

    def test_empty_allows_local(self):
        tg.preflight_local(runner(crontab="crontab_empty.txt"))

    def test_uncommented_blocks(self):
        with self.assertRaises(tg.GuardBlocked) as cm:
            tg.preflight_local(runner(crontab="crontab_uncommented.txt"))
        self.assertEqual(cm.exception.reason, "legacy_cron_active")

    def test_mixed_blocks_on_observed_only(self):
        with self.assertRaises(tg.GuardBlocked) as cm:
            tg.preflight_local(runner(crontab="crontab_mixed.txt"))
        self.assertEqual(cm.exception.reason, "legacy_cron_active")
        self.assertIn("wti_observed_tweet.py", cm.exception.detail)
        self.assertNotIn("wti_predicted_tweet.py", cm.exception.detail)

    def test_unreadable_fails_closed(self):
        with self.assertRaises(tg.GuardBlocked) as cm:
            tg.preflight_local(runner(crontab=None))
        self.assertEqual(cm.exception.reason, "crontab_unreadable")

    def test_real_crontab_missing_binary_fails_closed(self):
        with mock.patch.object(tg.subprocess, "run", side_effect=FileNotFoundError("crontab")):
            with self.assertRaises(tg.GuardBlocked) as cm:
                tg.preflight_local()
        self.assertEqual(cm.exception.reason, "crontab_unreadable")

    def test_cli_exit_code_and_reason(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = tg.main(["check-local"], runner(crontab="crontab_uncommented.txt"))
        self.assertNotEqual(rc, 0)
        self.assertIn("legacy_cron_active", err.getvalue())


class LaunchdSide(unittest.TestCase):
    def test_loaded_blocks_legacy(self):
        with self.assertRaises(tg.GuardBlocked) as cm:
            tg.preflight_legacy(runner(launchctl="launchctl_loaded.txt"))
        self.assertEqual(cm.exception.reason, "local_launchd_loaded")

    def test_clean_allows_legacy(self):
        tg.preflight_legacy(runner(launchctl="launchctl_clean.txt"))

    def test_unreadable_fails_closed(self):
        with self.assertRaises(tg.GuardBlocked) as cm:
            tg.preflight_legacy(runner(launchctl=None))
        self.assertEqual(cm.exception.reason, "launchctl_unreadable")


class Status(unittest.TestCase):
    def test_status_both_sides(self):
        out = tg.status(runner(crontab="crontab_mixed.txt", launchctl="launchctl_clean.txt"))
        self.assertIn("legacy_cron: active (1 line(s))", out)
        self.assertIn("local_launchd: not loaded", out)
        self.assertIn("allowed_local: no", out)
        self.assertIn("allowed_legacy: yes", out)

    def test_status_unreadable_allows_neither(self):
        out = tg.status(runner())
        self.assertIn("allowed_local: no", out)
        self.assertIn("allowed_legacy: no", out)


class LegacyScriptsGuarded(unittest.TestCase):
    def _run_script(self, name, launchctl_fixture):
        script = ROOT / "operations" / "scripts" / name
        bindir = Path(self.tmp) / "bin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / "launchctl"
        fake.write_text(f"#!/bin/sh\ncat '{FIX / launchctl_fixture}'\n")
        fake.chmod(0o755)
        stub = Path(self.tmp) / "stubs"
        (stub / "requests_oauthlib").mkdir(parents=True, exist_ok=True)
        (stub / "requests_oauthlib" / "__init__.py").write_text("OAuth1 = None\n")
        (stub / "requests.py").write_text(
            "def _boom(*a, **k):\n    raise SystemExit('network call attempted')\npost = get = _boom\n"
        )
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": self.tmp, "PYTHONPATH": str(stub)}
        return subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=60)

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_predicted_refuses_when_local_loaded(self):
        r = self._run_script("wti_predicted_tweet.py", "launchctl_loaded.txt")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("local_launchd_loaded", r.stderr)

    def test_observed_refuses_when_local_loaded(self):
        r = self._run_script("wti_observed_tweet.py", "launchctl_loaded.txt")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("local_launchd_loaded", r.stderr)

    def test_predicted_passes_guard_when_clean(self):
        r = self._run_script("wti_predicted_tweet.py", "launchctl_clean.txt")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("TWITTER_CONSUMER_KEY not set", r.stderr)


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
