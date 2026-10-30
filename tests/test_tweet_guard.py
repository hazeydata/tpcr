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
        self.assertEqual(sorted(tg.load_config().labels), [OBS, PRED])

    def test_expected_command_per_label(self):
        cfg = tg.load_config(environ={"TPCR_TWEET_CHECKOUT": "/Users/fred/theme-park-crowd-report",
                                      "TPCR_TWEET_PYTHON": ".venv/bin/python"})
        self.assertEqual(cfg.jobs[OBS], ["/Users/fred/theme-park-crowd-report/.venv/bin/python",
                                         "/Users/fred/theme-park-crowd-report/operations/scripts/wti_observed_tweet.py"])
        self.assertEqual(cfg.jobs[PRED][1], "/Users/fred/theme-park-crowd-report/operations/scripts/wti_predicted_tweet.py")

    def test_default_checkout_is_home_repo(self):
        cfg = tg.load_config(environ={})
        self.assertEqual(cfg.jobs[OBS], [str(Path(os.path.realpath(Path.home() / "theme-park-crowd-report/.venv/bin")) / "python"),
                                         os.path.realpath(Path.home() / "tpcr-tweet/operations/scripts/wti_observed_tweet.py")])
        self.assertEqual(cfg.jobs[PRED][1], os.path.realpath(Path.home() / "tpcr-tweet/operations/scripts/wti_predicted_tweet.py"))

    def test_relative_python_override_is_inside_checkout(self):
        cfg = tg.load_config(environ={"TPCR_TWEET_CHECKOUT": "/c", "TPCR_TWEET_PYTHON": ".venv/bin/python"})
        self.assertEqual(cfg.jobs[OBS][0], "/c/.venv/bin/python")

    def test_python_override(self):
        cfg = tg.load_config(environ={"TPCR_TWEET_CHECKOUT": "/c", "TPCR_TWEET_PYTHON": "/opt/py/bin/python3"})
        self.assertEqual(cfg.jobs[OBS], ["/opt/py/bin/python3", "/c/operations/scripts/wti_observed_tweet.py"])


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


def body(*args):
    inner = "".join(f"\t\t{x}\n" for x in args)
    return f"gui/501/x = {{\n\tstate = not running\n\tprogram = {args[0] if args else ''}\n\targuments = {{\n{inner}\t}}\n}}\n"


def text_runner(jobs, crontab="", launchctl="launchctl_loaded.txt"):
    def run(argv):
        if argv[0] == "crontab":
            return crontab, ""
        if argv[1] == "list":
            return (FIX / launchctl).read_text(), ""
        return jobs.get(argv[2].rsplit("/", 1)[-1], (None, "rc=113"))
    return run


C = "/Users/fred/theme-park-crowd-report"
PY = f"{C}/.venv/bin/python"
GOOD = {OBS: (body(PY, f"{C}/operations/scripts/wti_observed_tweet.py"), ""),
        PRED: (body(PY, f"{C}/operations/scripts/wti_predicted_tweet.py"), "")}


class ExpectedProgram(unittest.TestCase):
    def check(self, obs_body, environ=None):
        jobs = dict(GOOD, **{OBS: (obs_body, "")})
        with mock.patch.dict(os.environ, environ or {}):
            with redirect_stderr(io.StringIO()) as err:
                rc = tg.main(["check"], text_runner(jobs))
        return rc, err.getvalue()

    def test_exact_repo_command_allows(self):
        self.assertEqual(self.check(GOOD[OBS][0]), (0, ""))

    def test_no_arguments_fails_closed(self):
        rc, err = self.check("gui/501/x = {\n state = not running\n}")
        self.assertEqual(rc, 3)
        self.assertIn("launchd_job_no_arguments", err)

    def test_program_without_arguments_block_fails_closed(self):
        rc, err = self.check(f"program = {PY}\n")
        self.assertEqual(rc, 3)
        self.assertIn("launchd_job_no_arguments", err)

    def assert_unexpected(self, obs_body):
        rc, err = self.check(obs_body)
        self.assertEqual(rc, 3)
        self.assertIn("launchd_unexpected_program", err)

    def test_n20_program_key_other_than_python_blocks(self):
        good = GOOD[OBS][0]
        self.assert_unexpected(good.replace(f"program = {PY}", "program = /bin/sh"))

    def test_n20_program_key_absent_or_python_allows(self):
        good = GOOD[OBS][0]
        self.assertEqual(self.check(good.replace(f"\tprogram = {PY}\n", ""))[0], 0)
        self.assertEqual(self.check(good)[0], 0)

    def test_n20_relative_or_repeated_program_key_blocks(self):
        good = GOOD[OBS][0]
        self.assert_unexpected(good.replace(f"program = {PY}", "program = python"))
        self.assert_unexpected(good.replace(f"\tprogram = {PY}\n", f"\tprogram = {PY}\n\tprogram = /bin/sh\n"))

    def test_n19_duplicate_arguments_blocks_block(self):
        good = GOOD[OBS][0]
        evil = "\targuments = {\n\t\t/bin/echo\n\t}\n"
        self.assert_unexpected(good.replace("\tprogram", evil + "\tprogram"))
        self.assert_unexpected(good[:-2] + evil + "}\n")

    def test_n16_tilde_in_args_blocks(self):
        home = "/Users/fred"
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assert_unexpected(body(PY.replace(home, "~"), f"{C}/operations/scripts/wti_observed_tweet.py"))
            self.assert_unexpected(body(PY, f"~/theme-park-crowd-report/operations/scripts/wti_observed_tweet.py"))

    def test_n12_relative_paths_block_even_from_checkout_cwd(self):
        # launchd's cwd is not ours: a relative path that happens to resolve from our cwd still blocks.
        cases = [(C, body(PY, "operations/scripts/wti_observed_tweet.py")),
                 (f"{C}/.venv/bin", body("python", f"{C}/operations/scripts/wti_observed_tweet.py")),
                 (C, body(".venv/bin/python", f"{C}/operations/scripts/wti_observed_tweet.py"))]
        for cwd, obs_body in cases:
            with self.subTest(cwd=cwd), mock.patch("os.getcwd", return_value=cwd):
                self.assert_unexpected(obs_body)

    def test_unrelated_program_blocks(self):
        rc, err = self.check(body("/bin/echo", "hi"))
        self.assertEqual(rc, 3)
        self.assertIn("launchd_unexpected_program", err)
        self.assertIn("/bin/echo hi", err)

    def test_swapped_script_blocks(self):
        rc, err = self.check(body(PY, f"{C}/operations/scripts/wti_predicted_tweet.py"))
        self.assertEqual((rc, "launchd_unexpected_program" in err), (3, True))

    def test_system_python_blocks(self):
        rc, _ = self.check(body("/usr/bin/python3", f"{C}/operations/scripts/wti_observed_tweet.py"))
        self.assertEqual(rc, 3)

    def test_other_checkout_blocks(self):
        rc, _ = self.check(body("/Users/fred/tpcr-tweet/.venv/bin/python", "/Users/fred/tpcr-tweet/operations/scripts/wti_observed_tweet.py"))
        self.assertEqual(rc, 3)

    def test_extra_argument_blocks(self):
        rc, _ = self.check(body(PY, f"{C}/operations/scripts/wti_observed_tweet.py", "--dry-run"))
        self.assertEqual(rc, 3)

    def test_extra_absolute_argument_blocks(self):
        self.assert_unexpected(body(PY, f"{C}/operations/scripts/wti_observed_tweet.py", "/tmp/extra"))

    def test_wrong_python_without_program_line_blocks(self):
        b = body("/usr/bin/python3", f"{C}/operations/scripts/wti_observed_tweet.py").replace("\tprogram = /usr/bin/python3\n", "")
        self.assert_unexpected(b)

    def test_n20_relative_program_blocks_even_from_venv_cwd(self):
        b = GOOD[OBS][0].replace(f"program = {PY}", "program = python")
        with mock.patch("os.getcwd", return_value=f"{C}/.venv/bin"):
            self.assert_unexpected(b)

    def test_script_only_blocks(self):
        rc, _ = self.check(body(f"{C}/operations/scripts/wti_observed_tweet.py"))
        self.assertEqual(rc, 3)

    def test_arguments_block_found_among_other_blocks(self):
        b = (f"gui/501/x = {{\n\tenvironment = {{\n\t\textra_arguments => /bin/echo\n\t}}\n"
             f"\targuments = {{\n\t\t{PY}\n\t\t{C}/operations/scripts/wti_observed_tweet.py\n\t}}\n}}\n")
        self.assertEqual(self.check(b), (0, ""))

    def test_unterminated_arguments_fails_closed(self):
        rc, err = self.check(f"arguments = {{\n\t\t{PY}\n")
        self.assertEqual(rc, 3)
        self.assertIn("launchd_job_no_arguments", err)

    def test_configured_checkout_allows_its_scripts_only(self):
        alt = "/Users/fred/tpcr-tweet"
        jobs = {OBS: (body(f"{alt}/.venv/bin/python", f"{alt}/operations/scripts/wti_observed_tweet.py"), ""),
                PRED: (body(f"{alt}/.venv/bin/python", f"{alt}/operations/scripts/wti_predicted_tweet.py"), "")}
        with mock.patch.dict(os.environ, {"TPCR_TWEET_CHECKOUT": alt, "TPCR_TWEET_PYTHON": ".venv/bin/python"}):
            tg.preflight(text_runner(jobs))
            with self.assertRaises(tg.GuardBlocked):
                tg.preflight(text_runner(GOOD))

    def test_default_config_allows_tpcr_tweet_with_shared_venv_only(self):
        home = str(Path.home())
        py = f"{home}/theme-park-crowd-report/.venv/bin/python"
        tweet = {OBS: (body(py, f"{home}/tpcr-tweet/operations/scripts/wti_observed_tweet.py"), ""),
                 PRED: (body(py, f"{home}/tpcr-tweet/operations/scripts/wti_predicted_tweet.py"), "")}
        pipeline = {OBS: (body(py, f"{home}/theme-park-crowd-report/operations/scripts/wti_observed_tweet.py"), ""),
                    PRED: tweet[PRED]}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TPCR_TWEET_CHECKOUT", None)
            os.environ.pop("TPCR_TWEET_PYTHON", None)
            tg.preflight(text_runner(tweet))
            with self.assertRaises(tg.GuardBlocked) as cm:
                tg.preflight(text_runner(pipeline))
        self.assertEqual(cm.exception.reason, "launchd_unexpected_program")
        self.assertIn(OBS, cm.exception.detail)

    def test_paths_compared_resolved(self):
        import shutil
        import tempfile
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        real = root / "real"
        (real / "operations" / "scripts").mkdir(parents=True)
        (real / ".venv" / "bin").mkdir(parents=True)
        (root / "link").symlink_to(real)
        jobs = {OBS: (body(f"{root}/link/.venv/bin/python", f"{root}/link/operations/scripts/../scripts/wti_observed_tweet.py"), ""),
                PRED: (body(f"{real}/.venv/bin/python", f"{real}/operations/scripts/wti_predicted_tweet.py"), "")}
        with mock.patch.dict(os.environ, {"TPCR_TWEET_CHECKOUT": str(real)}):
            tg.preflight(text_runner(jobs))
        with mock.patch.dict(os.environ, {"TPCR_TWEET_CHECKOUT": str(root / "link")}):
            tg.preflight(text_runner(jobs))

    def test_venv_python_symlink_not_followed(self):
        import shutil
        import tempfile
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / ".venv" / "bin").mkdir(parents=True)
        (root / ".venv" / "bin" / "python").symlink_to("/usr/bin/python3")
        jobs = dict(GOOD, **{OBS: (body("/usr/bin/python3", f"{root}/operations/scripts/wti_observed_tweet.py"), "")})
        with mock.patch.dict(os.environ, {"TPCR_TWEET_CHECKOUT": str(root)}):
            with self.assertRaises(tg.GuardBlocked) as cm:
                tg.preflight(text_runner(jobs))
        self.assertIn(OBS, cm.exception.detail)

    def test_status_names_each_verdict(self):
        jobs = {OBS: (body("/bin/echo", "hi"), ""), PRED: ("state = running\n", "")}
        out = tg.status(text_runner(jobs))
        self.assertIn(f"  {OBS}: runs unexpected program (/bin/echo hi)", out)
        self.assertIn(f"  {PRED}: no program arguments", out)
        self.assertNotIn("runs repo script", out)
        self.assertIn("allowed_post: no", out)


class RealRun(unittest.TestCase):
    def test_nonzero_rc_is_unreadable_with_stderr(self):
        r = mock.Mock(returncode=113, stdout="partial", stderr=" Could not find service \n")
        with mock.patch.object(tg.subprocess, "run", return_value=r):
            self.assertEqual(tg._run(["launchctl", "print", "x"]), (None, "Could not find service"))

    def test_nonzero_rc_without_stderr(self):
        r = mock.Mock(returncode=1, stdout="", stderr="")
        with mock.patch.object(tg.subprocess, "run", return_value=r):
            self.assertEqual(tg._run(["crontab", "-l"]), (None, "rc=1"))

    def test_nonzero_rc_blocks_check(self):
        r = mock.Mock(returncode=1, stdout="PID\tStatus\tLabel\n", stderr="boom")
        with mock.patch.object(tg.subprocess, "run", return_value=r), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(tg.main(["check"]), 3)
        self.assertIn("crontab_unreadable boom", err.getvalue())

    def test_zero_rc_returns_stdout(self):
        r = mock.Mock(returncode=0, stdout="out", stderr="")
        with mock.patch.object(tg.subprocess, "run", return_value=r):
            self.assertEqual(tg._run(["crontab", "-l"]), ("out", ""))


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

    def test_env_values_match_readme_table(self):
        import tweet_v2
        table = {None: False, "0": False, "1": True, "": True, " 0": True, "0 ": True, "false": True,
                 "true": True, "no": True, "yes": True}
        for value, dry in table.items():
            env = {} if value is None else {"TPCR_TWEET_DRY_RUN": value}
            with self.subTest(value=value):
                self.assertIs(tweet_v2.dry_run_requested(self.args(False), env), dry)
                self.assertIs(tweet_v2.dry_run_requested(self.args(True), env), True)

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
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": self.tmp, "PYTHONPATH": str(stub),
               "TPCR_TWEET_CHECKOUT": "/Users/fred/theme-park-crowd-report", "TPCR_TWEET_PYTHON": ".venv/bin/python",
               **(extra_env or {})}
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


_FIXTURE_CHECKOUT = mock.patch.dict(os.environ, {"TPCR_TWEET_CHECKOUT": "/Users/fred/theme-park-crowd-report",
                                                 "TPCR_TWEET_PYTHON": ".venv/bin/python"})


def setUpModule():
    _PAST_WINDOW.start()
    _FIXTURE_CHECKOUT.start()


def tearDownModule():
    _FIXTURE_CHECKOUT.stop()
    _PAST_WINDOW.stop()
