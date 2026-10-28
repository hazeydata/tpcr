"""TPCR-TWEET-RESTYLE-CUTOVER: parallel transition (Fred, Oct 27 2026, 4:41 PM ET; dates moved +24h by the contingent shift, Fred Oct 27 10:22 PM ET).

New originals from the Oct 29 4 PM ET run; a reply uses its original's format; the old format retires at
00:00 ET Nov 1. No network: requests.post is a local fake, builds and renders are mocked."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.test_tweet_v2 import Base, D, ROOT, requests  # noqa: F401 - Base sets up content, state, fake build
import tweet_format as tf
import tweet_v1_legacy
import tweet_v2
import wti_observed_tweet as wot
import wti_predicted_tweet as wpt

ET = timezone(timedelta(hours=-4))  # EDT until 02:00 Nov 1 2026 (the retire instant, 00:00 Nov 1, is still EDT)
OLD_ID, NEW_ID = "1850000000000000001", "1850000000000000002"
BEFORE = "2026-10-28T20:00:01Z"  # Oct 28 4 PM ET (old run)
AFTER = "2026-10-29T20:00:05Z"   # Oct 29 4 PM ET (first new run)
SENTINEL = "SENTINEL-discord-token-0xD15C"
ENV = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")}


def et(*a):
    return datetime(*a, tzinfo=ET)


class Window(unittest.TestCase):
    def test_constants_come_from_one_config(self):
        cfg = json.loads((ROOT / "config" / "tweet_format.json").read_text())
        self.assertEqual(tf.FIRST_NEW_RUN, datetime.fromisoformat(cfg["first_new_run"]))
        self.assertEqual(tf.OLD_RETIRES, datetime.fromisoformat(cfg["old_retires"]))
        self.assertEqual(tf.FIRST_NEW_RUN, et(2026, 10, 29, 16))
        self.assertEqual(tf.OLD_RETIRES, et(2026, 11, 1, 0))
        # the dates appear nowhere else in code (single constant)
        code = subprocess.run(["git", "grep", "-l", "-E", r"2026-11-01T|2026, 11, 1[,)]|2026-10-29T16", "--", "*.py", "*.sh", "*.js"],
                              cwd=ROOT, capture_output=True, text=True).stdout.split()
        self.assertEqual([c for c in code if not c.startswith("tests/")], [])


class ReplyFormat(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="tweetfmt-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "tweet_state.json"
        self.state.write_text(json.dumps({"predicted": {
            "2026-10-29": {"tweet_id": OLD_ID, "posted_at": BEFORE},
            "2026-10-30": {"tweet_id": NEW_ID, "posted_at": AFTER, "format": "new"}}}))
        self.log = []

    def fmt(self, tid, now):
        return tf.reply_format(tid, self.state, now, log=self.log.append)[0]

    def tag(self, tid, f, posted):
        tf.record(self.state, tid, f, "predicted", "x", posted, log=self.log.append)

    # inside the window
    def test_old_original_gets_old_reply_inside_window(self):
        self.tag(OLD_ID, "old", BEFORE)
        self.assertEqual(self.fmt(OLD_ID, et(2026, 10, 30, 8, 30)), "old")

    def test_new_original_gets_new_reply_inside_window(self):
        self.tag(NEW_ID, "new", AFTER)
        self.assertEqual(self.fmt(NEW_ID, et(2026, 10, 31, 8, 30)), "new")

    # untagged originals
    def test_untagged_before_cutover_is_old(self):
        self.assertFalse(tf.store_path(self.state).exists())
        self.assertEqual(self.fmt(OLD_ID, et(2026, 10, 30, 8, 30)), "old")

    def test_untagged_without_posted_at_is_old_inside_window(self):
        self.assertEqual(self.fmt("999", et(2026, 10, 31, 8, 30)), "old")

    def test_untagged_posted_after_cutover_is_new_inside_window(self):
        self.assertEqual(self.fmt(NEW_ID, et(2026, 10, 31, 8, 30)), "new")

    def test_tag_new_but_posted_before_cutover_is_old(self):
        self.tag(OLD_ID, "new", BEFORE)
        self.assertEqual(self.fmt(OLD_ID, et(2026, 10, 30, 8, 30)), "old")

    def test_untagged_after_retirement_is_new_and_logged(self):
        self.assertEqual(self.fmt(OLD_ID, et(2026, 11, 2, 8, 30)), "new")
        self.assertTrue(any("NOTE untagged original" in m for m in self.log), self.log)

    def test_tagged_old_after_retirement_is_new(self):
        self.tag(OLD_ID, "old", BEFORE)
        self.assertEqual(self.fmt(OLD_ID, et(2026, 11, 2, 8, 30)), "new")
        self.assertFalse(any("NOTE untagged" in m for m in self.log))

    # exact boundaries
    def test_first_new_run_boundary(self):
        self.assertEqual(tf.original_format(et(2026, 10, 29, 15, 59, 59)), "old")
        self.assertEqual(tf.original_format(et(2026, 10, 29, 16, 0, 0)), "new")
        self.assertEqual(tf.original_format(datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)), "new")
        self.assertEqual(tf.original_format(datetime(2026, 10, 29, 19, 59, 59, 999999, tzinfo=timezone.utc)), "old")

    def test_retirement_boundary(self):
        self.tag(OLD_ID, "old", BEFORE)
        self.assertEqual(self.fmt(OLD_ID, et(2026, 10, 31, 23, 59, 59)), "old")
        self.assertEqual(self.fmt(OLD_ID, et(2026, 11, 1, 0, 0, 0)), "new")
        self.assertEqual(self.fmt(OLD_ID, datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)), "new")

    # kt1 mutants m02 / m04
    def test_reply_to_original_posted_exactly_at_first_new_run_is_new(self):
        at = "2026-10-29T20:00:00Z"  # exactly 16:00:00 ET Oct 29
        self.state.write_text(json.dumps({"predicted": {"2026-10-30": {"tweet_id": "777", "posted_at": at}}}))
        self.assertEqual(self.fmt("777", et(2026, 10, 31, 8, 30)), "new")  # untagged, by posted_at
        self.tag("777", "new", at)
        self.assertEqual(self.fmt("777", et(2026, 10, 31, 8, 30)), "new")  # tagged

    def test_tagged_old_original_without_posted_at_stays_old(self):
        self.state.write_text(json.dumps({"predicted": {"2026-10-29": {"tweet_id": "888"}}}))
        self.tag("888", "old", None)
        self.assertEqual(tf.reply_format("888", self.state, et(2026, 10, 31, 8, 30), log=self.log.append),
                         ("old", "tagged"))

    # naive clock: taken as ET, never raises
    def test_naive_now_is_treated_as_et(self):
        self.tag(OLD_ID, "old", BEFORE)
        self.assertEqual(self.fmt(OLD_ID, datetime(2026, 10, 31, 23, 59, 59)), "old")
        self.assertEqual(self.fmt(OLD_ID, datetime(2026, 11, 1, 0, 0, 0)), "new")

    def test_unusable_now_never_raises(self):
        f, reason = tf.reply_format(OLD_ID, self.state, "not-a-time", log=self.log.append)
        self.assertIn(f, ("old", "new"))
        self.assertEqual(reason, "lookup failed")

    # missing or corrupt store: fail safe, never raise
    def test_missing_store_falls_back_to_posted_at(self):
        self.assertEqual(self.fmt(OLD_ID, et(2026, 10, 30, 8, 30)), "old")
        self.assertEqual(self.fmt(NEW_ID, et(2026, 10, 31, 8, 30)), "new")

    def test_corrupt_store_falls_back_and_never_raises(self):
        for junk in ("{not json", "[1, 2]", "", "\x00\xff"):
            with self.subTest(junk=junk):
                tf.store_path(self.state).write_text(junk)
                self.assertEqual(self.fmt(OLD_ID, et(2026, 10, 30, 8, 30)), "old")
                self.assertEqual(self.fmt(NEW_ID, et(2026, 10, 31, 8, 30)), "new")
        self.assertTrue(any("unreadable" in m for m in self.log))

    def test_corrupt_store_entry_and_state_never_raise(self):
        tf.store_path(self.state).write_text(json.dumps({OLD_ID: "new", NEW_ID: None}))
        self.state.write_text("{broken")
        self.assertEqual(self.fmt(OLD_ID, et(2026, 10, 30, 8, 30)), "old")
        self.assertEqual(self.fmt(OLD_ID, et(2026, 11, 1, 0, 0)), "new")

    def test_store_is_a_directory_never_raises(self):
        tf.store_path(self.state).mkdir()
        self.assertEqual(self.fmt(OLD_ID, et(2026, 10, 30, 8, 30)), "old")
        self.assertFalse(tf.record(self.state, "1", "new", "predicted", D, AFTER, log=self.log.append))

    def test_migrate_tags_by_posted_at_and_is_idempotent(self):
        added = tf.migrate(self.state, log=self.log.append)
        self.assertEqual({k: v["format"] for k, v in added.items()}, {OLD_ID: "old", NEW_ID: "new"})
        self.assertEqual(tf.migrate(self.state, log=self.log.append), {})
        self.assertEqual(json.loads(tf.store_path(self.state).read_text())[OLD_ID]["format"], "old")

    def test_migrate_dry_run_writes_nothing(self):
        tf.migrate(self.state, dry=True, log=self.log.append)
        self.assertFalse(tf.store_path(self.state).exists())


class ConfigFailSafe(unittest.TestCase):
    """A missing or corrupt config/tweet_format.json must not crash the scripts at import: ERROR on stderr,
    CONFIG_ERROR set, every reply is new, and the 4 PM original run is skipped until a new original exists (D7)."""
    PROBE = (
        "import sys, types; sys.path.insert(0, 'operations/scripts');"
        "m = types.ModuleType('requests_oauthlib'); m.OAuth1 = object; sys.modules.setdefault('requests_oauthlib', m);"
        "import tweet_format as tf, wti_predicted_tweet, wti_observed_tweet;"
        "from datetime import datetime, timezone;"
        "now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc);"
        "print(bool(tf.CONFIG_ERROR), tf.reply_format('1', '/nonexistent/s.json', now, log=lambda m: 0)[0],"
        " now < tf.FIRST_NEW_RUN, tf.STORE_NAME)")

    def probe(self, content):
        import tempfile, shutil
        d = Path(tempfile.mkdtemp(prefix="tweetfmt-cfg-"))
        self.addCleanup(shutil.rmtree, d, True)
        cfg = d / "tweet_format.json"
        if content is not None:
            cfg.write_text(content)
        r = subprocess.run([sys.executable, "-c", self.PROBE], cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, "TPCR_TWEET_FORMAT_CONFIG": str(cfg)})
        return r

    def test_missing_config(self):
        r = self.probe(None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["True", "new", "False", "tweet_formats.json"])
        self.assertIn("ERROR tweet_format: config", r.stderr)

    def test_corrupt_config(self):
        for bad in ("{{{", "[]", '{"first_new_run": "nope"}', '{"first_new_run": "2026-10-29T16:00:00", "old_retires": "2026-11-01T00:00:00"}'):
            r = self.probe(bad)
            self.assertEqual(r.returncode, 0, (bad, r.stderr))
            self.assertEqual(r.stdout.split()[:3], ["True", "new", "False"], bad)
            self.assertIn("ERROR tweet_format", r.stderr)

    def test_custom_store_name_is_used(self):
        cfg = json.loads((ROOT / "config" / "tweet_format.json").read_text())
        cfg["store_name"] = "custom_formats.json"
        r = self.probe(json.dumps(cfg))
        self.assertEqual(r.stdout.split(), ["False", "old", "False", "custom_formats.json"], r.stderr)

    def test_bad_config_reason_names_the_config(self):
        probe = ("import sys; sys.path.insert(0, 'operations/scripts'); import tweet_format as tf;"
                 "from datetime import datetime, timezone;"
                 "print(tf.reply_format('1', '/nonexistent/s.json', datetime(2026, 10, 30, 12, tzinfo=timezone.utc),"
                 " log=lambda m: 0)[1])")
        import tempfile, shutil
        d = Path(tempfile.mkdtemp(prefix="tweetfmt-cfg-"))
        self.addCleanup(shutil.rmtree, d, True)
        r = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, "TPCR_TWEET_FORMAT_CONFIG": str(d / "missing.json")})
        self.assertIn("config missing/invalid", r.stdout, r.stderr)
        self.assertNotIn("after retirement", r.stdout)

    def test_good_config_has_no_error(self):
        r = self.probe((ROOT / "config" / "tweet_format.json").read_text())
        self.assertEqual(r.stdout.split()[:3], ["False", "old", "False"], r.stderr)


class OldAltText(unittest.TestCase):
    def test_old_video_alt_lists_crowd_labels(self):
        a = tweet_v1_legacy.alt_text({"target_date": "2026-10-29", "parks": [
            {"park_name": "EPCOT", "wti": 12.0, "wti_label": "Quiet"}, {"park_code": "MK", "wti": 31.0, "wti_label": "Busy"}]})
        self.assertIn("EPCOT 12.0 (Quiet)", a)
        self.assertIn("Magic Kingdom 31.0 (Busy)", a)


    def test_old_video_alt_does_not_promise_missing_labels(self):
        some = tweet_v1_legacy.alt_text({"target_date": "2026-10-29", "parks": [
            {"park_name": "EPCOT", "wti": 12.0, "wti_label": "Quiet"}, {"park_code": "MK", "wti": 31.0}]})
        none = tweet_v1_legacy.alt_text({"target_date": "2026-10-29", "parks": [
            {"park_name": "EPCOT", "wti": 12.0}, {"park_code": "MK", "wti": 31.0, "wti_label": ""}]})
        for a in (some, none):
            self.assertNotIn("with its crowd label", a)
        self.assertIn("where one is available", some)
        self.assertIn("EPCOT 12.0 (Quiet); Magic Kingdom 31.0.", some)
        self.assertNotIn("label", none)
        self.assertIn("each park's observed WTI. EPCOT 12.0; Magic Kingdom 31.0.", none)


class LookupFailure(unittest.TestCase):
    """reply_format's except branch: a lookup failure uses the run clock (old inside the window, new after)."""

    def run_at(self, now):
        log = []
        with mock.patch.object(tf, "load_store", side_effect=RuntimeError("boom")):
            return tf.reply_format(OLD_ID, "/nonexistent/tweet_state.json", now, log=log.append), log

    def test_lookup_failure_inside_window_is_old(self):
        for now in (et(2026, 10, 29, 16), et(2026, 10, 30, 8, 30), et(2026, 10, 31, 23, 59, 59)):
            (fmt, why), log = self.run_at(now)
            self.assertEqual((fmt, why), ("old", "lookup failed"), now)
            self.assertIn("using old", log[-1])

    def test_lookup_failure_after_retirement_is_new(self):
        (fmt, why), _ = self.run_at(et(2026, 11, 1, 0))
        self.assertEqual((fmt, why), ("new", "lookup failed"))


class Scripts(Base):
    """Through the real scripts (build, render and X calls faked locally)."""

    def at(self, when):
        p = mock.patch.object(tweet_v2, "now", return_value=when); p.start(); self.addCleanup(p.stop)

    def dry_observed(self):
        with mock.patch.object(tweet_v2, "build", self.fake_build), \
             mock.patch.object(tweet_v1_legacy, "render_video", side_effect=AssertionError("dry run rendered")):
            rc, out, _ = self.run_main(wot, ["--dry-run", "--date", D, "--content-dir", str(self.cdir), "--history", "h",
                                             "--state-file", str(self.state), "--out", str(self.tmp / "o")])
        return rc, out, json.loads((self.tmp / "o" / "dry_run.json").read_text())

    def test_old_original_dry_run_builds_old_reply_with_alt_text(self):
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": OLD_ID, "posted_at": BEFORE}}}))
        self.at(et(2026, 10, 30, 8, 30))
        rc, out, m = self.dry_observed()
        self.assertEqual((rc, m.get("format"), m["reply_to_tweet_id"]), (0, "old", OLD_ID))
        self.assertTrue(m["tweet_text"].startswith(f"How it actually went \u2014 {D}:"))
        self.assertIn("Animal Kingdom 27.0", m["alt_text"])
        self.assertIn("Reply format for ref=2026-10-28: old", out)

    def test_new_original_dry_run_builds_new_reply(self):
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": NEW_ID, "posted_at": AFTER, "format": "new"}}}))
        tf.record(self.state, NEW_ID, "new", "predicted", D, AFTER)
        self.at(et(2026, 10, 31, 8, 30))
        rc, out, m = self.dry_observed()
        self.assertEqual((rc, m["alt_text"], m["reply_to_tweet_id"]), (0, "R alt", NEW_ID))
        self.assertNotIn("format", m)

    def test_corrupt_store_never_crashes_the_reply(self):
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": OLD_ID, "posted_at": BEFORE}}}))
        tf.store_path(self.state).write_text("{{{")
        self.at(et(2026, 10, 30, 8, 30))
        rc, _, m = self.dry_observed()
        self.assertEqual((rc, m.get("format")), (0, "old"))

    def post_fake(self, calls, tid):
        def post(url, **kw):
            calls.append((url, kw))
            if url == wot.TWITTER_UPLOAD_API:
                return self.resp(200, {"media_id_string": "M1"})
            if url == tweet_v2.TWITTER_METADATA_API:
                return self.resp(200, {})
            return self.resp(201, {"data": {"id": tid}})
        return post

    def resp(self, code, body):
        r = mock.Mock(status_code=code, text=json.dumps(body)); r.json.return_value = body; return r

    def run_main_caught(self, mod, argv):
        """run_main, but the run's outcome is the return value: sys.exit(n) -> ("exit", n) and an exception that
        escapes main -> ("escaped", type name). So a broken alert wrapper fails an assertion, not the test harness."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = mod.main(argv)
            except SystemExit as e:
                rc = ("exit", e.code)
            except Exception as e:  # noqa: BLE001
                rc = ("escaped", type(e).__name__)
        return rc, out.getvalue(), err.getvalue()

    def test_live_old_reply_posts_old_video_with_alt_text_and_tags_it(self):
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": OLD_ID, "posted_at": BEFORE}}}))
        self.at(et(2026, 10, 30, 8, 30))
        mp4 = self.tmp / "old.mp4"; mp4.write_bytes(b"mp4")
        calls = []
        with mock.patch.object(wot, "load_env"), mock.patch.object(wot, "enforce"), mock.patch.dict("os.environ", ENV), \
             mock.patch.object(requests, "post", self.post_fake(calls, "OBS-OLD")), mock.patch.object(wot, "post_discord"), \
             mock.patch.object(tweet_v2, "build", side_effect=AssertionError("v2 build for an old original")), \
             mock.patch.object(tweet_v1_legacy, "render_video", return_value=mp4):
            self.run_main(wot, ["--date", D, "--content-dir", str(self.cdir), "--history", "h", "--state-file", str(self.state)])
        meta = [kw["json"] for u, kw in calls if u == tweet_v2.TWITTER_METADATA_API]
        self.assertEqual(len(meta), 1); self.assertIn("Walt Disney World", meta[0]["alt_text"]["text"])
        tweet = [kw["json"] for u, kw in calls if u == wot.TWITTER_API][0]
        self.assertEqual(tweet["reply"], {"in_reply_to_tweet_id": OLD_ID})
        self.assertEqual(json.loads(tf.store_path(self.state).read_text())["OBS-OLD"]["format"], "old")

    def test_predicted_live_before_first_new_run_posts_nothing(self):
        self.at(et(2026, 10, 29, 15, 59, 59))
        post = mock.Mock(side_effect=AssertionError("posted before cutover"))
        with mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"), mock.patch.dict("os.environ", ENV), \
             mock.patch.object(requests, "post", post):
            rc, out, _ = self.run_main(wpt, ["--date", "2026-10-30", "--content-dir", str(self.cdir), "--history", "h",
                                             "--state-file", str(self.state)])
        self.assertEqual(rc, 0); self.assertIn("skipped_before_cutover", out)

    def bad_config_predicted(self, when, extra=()):
        self.at(when)
        post = mock.Mock(side_effect=AssertionError("posted with a bad config before any new original"))
        with mock.patch.object(tf, "CONFIG_ERROR", "FileNotFoundError: x"), \
             mock.patch.object(tf, "FIRST_NEW_RUN", datetime.min.replace(tzinfo=timezone.utc)), \
             mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"), mock.patch.dict("os.environ", ENV), \
             mock.patch.object(requests, "post", post), mock.patch.object(wpt, "post_discord") as discord:
            rc, out, err = self.run_main(wpt, ["--date", "2026-10-30", "--content-dir", str(self.cdir), "--history", "h",
                                             "--state-file", str(self.state)])
        self.discord = discord
        return rc, out, err, post

    def test_bad_config_skip_sends_one_discord_alert_naming_config(self):
        rc, out, err, post = self.bad_config_predicted(et(2026, 10, 29, 16, 0))
        self.assertEqual(rc, 0); self.assertIn("skipped_bad_config", out); post.assert_not_called()
        self.assertEqual(self.discord.call_count, 1)
        msg = self.discord.call_args[0][0]
        self.assertIn(str(tf.CONFIG), msg); self.assertIn("forecast original was skipped", msg)

    def test_bad_config_skip_alert_failure_is_logged_and_exits_0(self):  # m37
        for exc in (requests.exceptions.Timeout("t"), requests.exceptions.ConnectionError("c"), RuntimeError("r")):
            self.at(et(2026, 10, 29, 16, 0))
            post = mock.Mock(side_effect=AssertionError("posted with a bad config"))
            with mock.patch.object(tf, "CONFIG_ERROR", "FileNotFoundError: x"), \
                 mock.patch.object(tf, "FIRST_NEW_RUN", datetime.min.replace(tzinfo=timezone.utc)), \
                 mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"), mock.patch.dict("os.environ", ENV), \
                 mock.patch.object(requests, "post", post), mock.patch.object(wpt, "post_discord", side_effect=exc) as d:
                rc, out, err = self.run_main_caught(wpt, ["--date", "2026-10-30", "--content-dir", str(self.cdir), "--history", "h",
                                                 "--state-file", str(self.state)])
            self.assertEqual(rc, 0); self.assertIn("skipped_bad_config", out); post.assert_not_called()
            self.assertEqual(d.call_count, 1)
            self.assertIn(f"Discord alert failed ({type(exc).__name__})", err)
        # Real HTTP failures through the real post_discord: a 500 and a 4xx must be logged too, never silent.
        for code in (500, 403):
            self.at(et(2026, 10, 29, 16, 0))
            calls = []
            post = self.discord_status_post(calls, code)
            with mock.patch.object(tf, "CONFIG_ERROR", "FileNotFoundError: x"), \
                 mock.patch.object(tf, "FIRST_NEW_RUN", datetime.min.replace(tzinfo=timezone.utc)), \
                 mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"), \
                 mock.patch.dict("os.environ", dict(ENV, DISCORD_BOT_TOKEN=SENTINEL)), \
                 mock.patch.object(requests, "post", post):
                rc, out, err = self.run_main(wpt, ["--date", "2026-10-30", "--content-dir", str(self.cdir), "--history", "h",
                                                 "--state-file", str(self.state)])
            self.assertEqual(rc, 0); self.assertIn("skipped_bad_config", out)
            self.assertEqual(len(calls), 1); self.assertTrue(calls[0].startswith(wpt.DISCORD_API))
            self.assertIn("Discord alert failed (HTTPError)", err)
            self.assertNotIn(SENTINEL, out + err); self.assertNotIn("discord-error-body", out + err)

    def discord_status_post(self, calls, code):
        def post(url, **kw):
            calls.append(url)
            if not url.startswith(wpt.DISCORD_API):
                raise AssertionError(f"unexpected non-Discord post: {url}")
            r = requests.models.Response(); r.status_code = code; r._content = b"discord-error-body"; r.url = url
            return r
        return post

    def test_post_discord_raises_on_non_2xx_without_token_or_body(self):
        for code in (500, 502, 400, 401, 404, 429):
            with mock.patch.dict("os.environ", {"DISCORD_BOT_TOKEN": SENTINEL}), \
                 mock.patch.object(requests, "post", self.discord_status_post([], code)):
                with self.assertRaises(requests.exceptions.HTTPError) as cm:
                    wpt.post_discord("m")
            self.assertIn(str(code), str(cm.exception))
            self.assertNotIn(SENTINEL, str(cm.exception)); self.assertNotIn("discord-error-body", str(cm.exception))
        for code in (200, 204):
            calls = []
            with mock.patch.dict("os.environ", {"DISCORD_BOT_TOKEN": SENTINEL}), \
                 mock.patch.object(requests, "post", self.discord_status_post(calls, code)):
                self.assertIsNone(wpt.post_discord("m"))
            self.assertEqual(len(calls), 1)
        with mock.patch.dict("os.environ", {}, clear=False), mock.patch.object(requests, "post") as p:
            os.environ.pop("DISCORD_BOT_TOKEN", None)
            wpt.post_discord("m"); p.assert_not_called()

    def test_safe_alert_logs_type_only_and_returns_false(self):
        for exc in (requests.exceptions.HTTPError("Discord alert HTTP 500"), requests.exceptions.Timeout(SENTINEL)):
            err = io.StringIO()
            with mock.patch.object(wpt, "post_discord", side_effect=exc), redirect_stderr(err):
                try:
                    result = wpt.safe_alert("m")
                except Exception as e:  # noqa: BLE001 - a re-raise is a wrong result, not a harness crash
                    result = ("raised", type(e).__name__)
            self.assertIs(result, False)
            self.assertIn(f"Discord alert failed ({type(exc).__name__})", err.getvalue())
            self.assertNotIn(SENTINEL, err.getvalue())
        with mock.patch.object(wpt, "post_discord") as d:
            self.assertTrue(wpt.safe_alert("m")); d.assert_called_once_with("m")

    def test_predicted_success_alert_failure_keeps_tweet_and_state(self):
        # The posted-confirmation alert fails (Discord 500) after the tweet is out: no crash, state saved, rc None.
        D2 = "2026-10-30"
        from tests.test_tweet_v2 import content
        (self.cdir / f"predicted_{D2}.json").write_text(content(D2, [20.1, 21.0, 26.0, 33.3], generated_at="2026-10-29T10:00:00-04:00"))
        self.at(et(2026, 10, 29, 16, 0, 0))
        calls = []
        with mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"), mock.patch.dict("os.environ", ENV), \
             mock.patch.object(requests, "post", self.post_fake(calls, "PRED-ALERTFAIL")), \
             mock.patch.object(wpt, "post_discord", side_effect=requests.exceptions.HTTPError("Discord alert HTTP 500")) as d, \
             mock.patch.object(tweet_v2, "parquet_last_date", return_value=D2), \
             mock.patch.object(tweet_v2, "build", self.fake_build), mock.patch.object(wpt, "STATE_FILE", self.state):
            rc, out, err = self.run_main_caught(wpt, ["--date", D2, "--content-dir", str(self.cdir), "--history", "h",
                                             "--state-file", str(self.state)])
        self.assertIn(rc, (None, 0)); self.assertTrue(any(u == wpt.TWITTER_API for u, _ in calls))
        self.assertEqual(json.loads(self.state.read_text())["predicted"][D2]["tweet_id"], "PRED-ALERTFAIL")
        self.assertGreaterEqual(d.call_count, 1); self.assertIn("Discord alert failed (HTTPError)", err)

    def test_predicted_park_hours_alert_failure_still_posts(self):
        D2 = "2026-10-30"
        from tests.test_tweet_v2 import content
        (self.cdir / f"predicted_{D2}.json").write_text(content(D2, [20.1, 21.0, 26.0, 33.3], generated_at="2026-10-29T10:00:00-04:00"))
        self.at(et(2026, 10, 29, 16, 0, 0))
        calls = []
        with mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"), mock.patch.dict("os.environ", ENV), \
             mock.patch.object(requests, "post", self.post_fake(calls, "PRED-PH")), \
             mock.patch.object(wpt, "post_discord", side_effect=requests.exceptions.HTTPError("Discord alert HTTP 500")), \
             mock.patch.object(tweet_v2, "parquet_last_date", return_value=D2), \
             mock.patch.object(tweet_v2, "build", self.fake_build), mock.patch.object(wpt, "STATE_FILE", self.state):
            rc, out, err = self.run_main_caught(wpt, ["--date", D2, "--content-dir", str(self.cdir), "--history", "h",
                                             "--state-file", str(self.state), "--park-hours", str(self.tmp / "no-such-hours.csv")])
        self.assertIn(rc, (None, 0))
        self.assertIn("park_hours_unreadable", err); self.assertIn("Discord alert failed (HTTPError)", err)
        self.assertTrue(any(u == wpt.TWITTER_API for u, _ in calls))
        self.assertEqual(json.loads(self.state.read_text())["predicted"][D2]["tweet_id"], "PRED-PH")

    def test_bad_config_skip_without_discord_token_is_stderr_only(self):
        self.at(et(2026, 10, 29, 16, 0))
        post = mock.Mock(side_effect=AssertionError("network call"))
        with mock.patch.object(tf, "CONFIG_ERROR", "FileNotFoundError: x"), \
             mock.patch.object(tf, "FIRST_NEW_RUN", datetime.min.replace(tzinfo=timezone.utc)), \
             mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"), mock.patch.dict("os.environ", ENV), \
             mock.patch.object(requests, "post", post):
            os.environ.pop("DISCORD_BOT_TOKEN", None)
            rc, out, err = self.run_main(wpt, ["--date", "2026-10-30", "--content-dir", str(self.cdir), "--history", "h",
                                             "--state-file", str(self.state)])
        self.assertEqual(rc, 0); self.assertIn("skipped_bad_config", out); self.assertIn("ERROR", err)
        post.assert_not_called()

    def test_bad_config_unblocks_on_state_only_new_original(self):  # m30
        with mock.patch.object(tf, "CONFIG_ERROR", "ValueError: x"):
            self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": NEW_ID, "posted_at": AFTER, "format": "new"}}}))
            self.assertFalse(tf.store_path(self.state).exists())
            self.assertIsNone(tf.original_run_blocked(self.state))

    def test_bad_config_new_observed_or_reply_tag_does_not_unblock(self):  # m32
        with mock.patch.object(tf, "CONFIG_ERROR", "ValueError: x"):
            tf.record(self.state, "OBS-NEW", "new", "observed", D, AFTER)
            self.assertIn("config missing/invalid", tf.original_run_blocked(self.state))
            tf.record(self.state, "REPLY-NEW", "new", "reply", D, AFTER)
            self.assertIn("config missing/invalid", tf.original_run_blocked(self.state))

    def test_bad_config_before_first_new_run_skips_original(self):
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": OLD_ID, "posted_at": BEFORE}}}))
        tf.record(self.state, OLD_ID, "old", "predicted", D, BEFORE)
        rc, out, err, post = self.bad_config_predicted(et(2026, 10, 29, 15, 0))
        self.assertEqual(rc, 0)
        self.assertIn("skipped_bad_config", out)
        self.assertIn("config missing/invalid", err)
        post.assert_not_called()

    def test_bad_config_blocks_only_until_a_new_original_exists(self):
        self.assertIsNone(tf.original_run_blocked(self.state))  # good config: never blocked
        with mock.patch.object(tf, "CONFIG_ERROR", "ValueError: x"):
            self.assertIn("config missing/invalid", tf.original_run_blocked(self.tmp / "missing" / "s.json"))
            tf.record(self.state, NEW_ID, "new", "predicted", D, AFTER)
            self.assertIsNone(tf.original_run_blocked(self.state))

    def test_predicted_live_at_first_new_run_posts_and_tags_new(self):
        D2 = "2026-10-30"
        from tests.test_tweet_v2 import content
        (self.cdir / f"predicted_{D2}.json").write_text(content(D2, [20.1, 21.0, 26.0, 33.3], generated_at="2026-10-29T10:00:00-04:00"))
        self.at(et(2026, 10, 29, 16, 0, 0))
        calls = []
        with mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"), mock.patch.dict("os.environ", ENV), \
             mock.patch.object(requests, "post", self.post_fake(calls, "PRED-NEW")), mock.patch.object(wpt, "post_discord"), \
             mock.patch.object(tweet_v2, "parquet_last_date", return_value=D2), \
             mock.patch.object(tweet_v2, "build", self.fake_build), mock.patch.object(wpt, "STATE_FILE", self.state):
            self.run_main(wpt, ["--date", D2, "--content-dir", str(self.cdir), "--history", "h", "--state-file", str(self.state)])
        self.assertTrue(any(u == wpt.TWITTER_API for u, _ in calls))
        self.assertEqual(json.loads(self.state.read_text())["predicted"][D2]["format"], "new")
        self.assertEqual(json.loads(tf.store_path(self.state).read_text())["PRED-NEW"]["format"], "new")

    def test_fixing_config_resumes_next_4pm_original(self):
        rc, out, _, post = self.bad_config_predicted(et(2026, 10, 29, 16, 0))
        self.assertIn("skipped_bad_config", out); post.assert_not_called()
        rc, out, _, post = self.bad_config_predicted(et(2026, 10, 30, 16, 0))
        self.assertIn("skipped_bad_config", out)  # still skipped while the config is broken
        # config fixed (real tweet_format values, no CONFIG_ERROR): the next 4 PM run, Oct 31, posts a new original
        D3 = "2026-11-01"
        from tests.test_tweet_v2 import content
        (self.cdir / f"predicted_{D3}.json").write_text(content(D3, [20.1, 21.0, 26.0, 33.3], generated_at="2026-10-31T10:00:00-04:00"))
        self.at(et(2026, 10, 31, 16, 0, 0))
        calls = []
        with mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"), mock.patch.dict("os.environ", ENV), \
             mock.patch.object(requests, "post", self.post_fake(calls, "PRED-RESUMED")), mock.patch.object(wpt, "post_discord"), \
             mock.patch.object(tweet_v2, "parquet_last_date", return_value=D3), \
             mock.patch.object(tweet_v2, "build", self.fake_build), mock.patch.object(wpt, "STATE_FILE", self.state):
            self.run_main(wpt, ["--date", D3, "--content-dir", str(self.cdir), "--history", "h", "--state-file", str(self.state)])
        self.assertTrue(any(u == wpt.TWITTER_API for u, _ in calls))
        self.assertEqual(json.loads(tf.store_path(self.state).read_text())["PRED-RESUMED"]["format"], "new")


class AlertFailureLeavesOutcome(Base):
    """Every safe_alert site in wti_predicted_tweet.main (:325 closures, :331 no_data, :336 stale, :339 park hours,
    :384 silent, :389 upload failed, :394 tweet failed, :402 success): an alert that raises, network error or not,
    leaves the run outcome (exit code, saved state, no-data streak, X calls, skip markers) equal to a run whose
    alert succeeds. post_discord is mocked; no X or Discord traffic leaves the process."""

    D2 = "2026-10-30"
    FAILS = (requests.exceptions.HTTPError("Discord alert HTTP 500"), requests.exceptions.ConnectionError("c"),
             RuntimeError("r"), ValueError("v"), KeyError("k"))
    MARK = re.compile(r"skipped_\w+|closures_error|park_hours_unreadable|SILENT: [^\n(]*|State saved: [^\n]*|Done\.")

    at = Scripts.at
    post_fake = Scripts.post_fake
    resp = Scripts.resp

    def outcome(self, n, effect, generated_at="2026-10-29T10:00:00-04:00", content=True, nodata=None, extra=(),
                patches=()):
        from tests.test_tweet_v2 import content as mk
        d = self.tmp / f"run{n}"; cdir = d / "content"; cdir.mkdir(parents=True)
        state = d / "tweet_state.json"; state.write_text(self.state.read_text())
        if content:
            (cdir / f"predicted_{self.D2}.json").write_text(mk(self.D2, [20.1, 21.0, 26.0, 33.3], generated_at=generated_at))
        if nodata is not None:
            tweet_v2.no_data_state_path(state).write_text(json.dumps(nodata))
        self.at(et(2026, 10, 29, 16, 0, 0))
        calls = []
        with contextlib.ExitStack() as st:
            for cm in (mock.patch.object(wpt, "load_env"), mock.patch.object(wpt, "enforce"),
                       mock.patch.dict("os.environ", ENV), mock.patch.object(wpt, "STATE_FILE", state),
                       mock.patch.object(requests, "post", self.post_fake(calls, "PRED-OUT")),
                       mock.patch.object(tweet_v2, "parquet_last_date", return_value=self.D2),
                       mock.patch.object(tweet_v2, "build", self.fake_build)) + tuple(f() for f in patches):
                st.enter_context(cm)
            disc = st.enter_context(mock.patch.object(wpt, "post_discord", side_effect=effect))
            rc, out, err = Scripts.run_main_caught(self, wpt, ["--date", self.D2, "--content-dir", str(cdir),
                                                              "--history", "h", "--state-file", str(state), *extra])
        nd = tweet_v2.no_data_state_path(state)
        def wallclock_free(v):  # posted_at is the real clock at save time; everything else must match exactly
            if isinstance(v, dict):
                return {k: ("<time>" if k.endswith("_at") else wallclock_free(x)) for k, x in v.items()}
            return v
        res = {"rc": rc, "markers": self.MARK.findall(out + err), "state": wallclock_free(json.loads(state.read_text())),
               "no_data": json.loads(nd.read_text()) if nd.exists() else None,
               "x_calls": [u for u, _ in calls], "alerts": disc.call_count}
        return res, err

    def check(self, **kw):
        base, base_err = self.outcome("base", None, **kw)
        self.assertGreaterEqual(base["alerts"], 1, "scenario never reaches its alert site")
        self.assertNotIn("Discord alert failed", base_err)
        for i, exc in enumerate(self.FAILS):
            got, err = self.outcome(i, exc, **kw)
            self.assertEqual(got, base, f"alert raising {type(exc).__name__} changed the run outcome")
            self.assertIn(f"Discord alert failed ({type(exc).__name__})", err)
        return base

    def test_closures_alert_failure_keeps_exit_1(self):  # :325
        base = self.check(extra=("--closures", str(self.tmp / "no-such-closures.csv")))
        self.assertEqual(base["rc"], ("exit", 1)); self.assertIn("closures_error", base["markers"])
        self.assertEqual(base["x_calls"], [])

    def test_no_data_alert_failure_keeps_skip_and_streak(self):  # :331
        nodata = {"predicted": {"last_skipped": "2026-10-29", "streak": 1, "alerted": False}}
        base = self.check(content=False, nodata=nodata)
        self.assertEqual(base["rc"], 0); self.assertIn("skipped_no_data", base["markers"])
        self.assertEqual(base["no_data"]["predicted"]["streak"], 2); self.assertEqual(base["x_calls"], [])

    def test_stale_alert_failure_keeps_skip(self):  # :336
        base = self.check(generated_at="2026-10-20T10:00:00-04:00")
        self.assertEqual(base["rc"], 0); self.assertIn("skipped_stale", base["markers"]); self.assertEqual(base["x_calls"], [])

    def test_park_hours_alert_failure_still_posts(self):  # :339
        base = self.check(extra=("--park-hours", str(self.tmp / "no-such-hours.csv")))
        self.assertIn(base["rc"], (None, 0)); self.assertIn("park_hours_unreadable", base["markers"])
        self.assertEqual(base["state"]["predicted"][self.D2]["tweet_id"], "PRED-OUT")

    def test_silent_alert_failure_keeps_silence(self):  # :384
        base = self.check(patches=(lambda: mock.patch.object(tweet_v2, "build", side_effect=RuntimeError("no card")),))
        self.assertIsNone(base["rc"]); self.assertIn("SILENT: no forecast card for 2026-10-30 ", base["markers"])
        self.assertEqual(base["x_calls"], []); self.assertNotIn(self.D2, base["state"]["predicted"])

    def test_upload_failed_alert_failure_keeps_exit_1(self):  # :389
        base = self.check(patches=(lambda: mock.patch.object(wpt, "upload_media", return_value=None),))
        self.assertEqual(base["rc"], ("exit", 1)); self.assertIn("SILENT: video upload failed; nothing posted", base["markers"])
        self.assertNotIn(self.D2, base["state"]["predicted"])

    def test_tweet_failed_alert_failure_keeps_exit_1(self):  # :394
        base = self.check(patches=(lambda: mock.patch.object(wpt, "upload_media", return_value="M1"),
                                   lambda: mock.patch.object(wpt, "post_tweet", return_value=None)))
        self.assertEqual(base["rc"], ("exit", 1)); self.assertNotIn(self.D2, base["state"]["predicted"])

    def test_success_alert_failure_keeps_tweet_and_state(self):  # :402
        base = self.check()
        self.assertIsNone(base["rc"]); self.assertIn("Done.", base["markers"])
        self.assertEqual(base["state"]["predicted"][self.D2]["tweet_id"], "PRED-OUT")


if __name__ == "__main__":
    unittest.main()


class ReplyTiers(Base):
    """New-format reply tiers A/B come only from config/tweet_format.json; unset or invalid = skipped_bad_config."""

    def test_parse(self):
        self.assertEqual(tf._tiers({"reply_tier_a": 2, "reply_tier_b": 5.5}), ((2.0, 5.5), None))
        for a, b in ((None, None), (2, None), (5, 2), (2, 2), (0, 3), ("2", 5), (True, 5)):
            t, err = tf._tiers({"reply_tier_a": a, "reply_tier_b": b})
            self.assertIsNone(t, (a, b))
            self.assertIn("reply_tier_a/reply_tier_b", err)
        self.assertIsNone(tf._tiers({})[0])

    def test_shipped_config_has_freds_tiers(self):
        """Fred, Oct 28 2026 2:48 PM ET: A = 3, B = 6. The shipped config loads with 0 < A < B and no error."""
        cfg = json.loads((ROOT / "config" / "tweet_format.json").read_text())
        self.assertEqual((cfg["reply_tier_a"], cfg["reply_tier_b"]), (3.0, 6.0))
        self.assertEqual(tf._tiers(cfg), ((3.0, 6.0), None))
        a, b = tf._tiers(cfg)[0]
        self.assertLess(a, b)

    def live_observed(self):
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": NEW_ID, "posted_at": AFTER, "format": "new"}}}))
        tf.record(self.state, NEW_ID, "new", "predicted", D, AFTER)
        sent, post = [], mock.Mock(side_effect=AssertionError("posted"))
        keys = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")}
        with mock.patch.object(tweet_v2, "now", return_value=et(2026, 10, 30, 8, 30)), \
             mock.patch.object(tf, "REPLY_TIERS", None), mock.patch.object(tf, "TIERS_ERROR", "reply_tier_a/reply_tier_b unset"), \
             mock.patch.object(wot, "load_env"), mock.patch.object(wot, "enforce"), mock.patch.dict("os.environ", keys), \
             mock.patch.object(requests, "post", post), mock.patch.object(wot, "post_discord", side_effect=sent.append), \
             mock.patch.object(tweet_v2, "build", side_effect=AssertionError("built")):
            rc, out, err = self.run_main(wot, ["--date", D, "--content-dir", str(self.cdir), "--history", "h",
                                               "--state-file", str(self.state), "--out", str(self.tmp / "o")])
        return rc, out, err, sent, post

    def test_unset_tiers_skip_new_reply_with_one_alert(self):
        rc, out, err, sent, post = self.live_observed()
        self.assertEqual(rc, 0)
        self.assertIn("skipped_bad_config ref=2026-10-28: nothing posted", out)
        self.assertEqual(len(sent), 1)
        self.assertIn("reply_tier_a/reply_tier_b", sent[0])
        post.assert_not_called()
        self.assertNotIn("observed", json.loads(self.state.read_text()))

    def test_unset_tiers_leave_old_reply_alone(self):
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": OLD_ID, "posted_at": BEFORE}}}))
        with mock.patch.object(tf, "REPLY_TIERS", None), mock.patch.object(tf, "TIERS_ERROR", "unset"), \
             mock.patch.object(tweet_v2, "now", return_value=et(2026, 10, 29, 8, 30)), \
             mock.patch.object(tweet_v2, "build", self.fake_build), \
             mock.patch.object(tweet_v1_legacy, "render_video", side_effect=AssertionError("dry run rendered")):
            rc, out, _ = self.run_main(wot, ["--dry-run", "--date", D, "--content-dir", str(self.cdir), "--history", "h",
                                             "--state-file", str(self.state), "--out", str(self.tmp / "o")])
        m = json.loads((self.tmp / "o" / "dry_run.json").read_text())
        self.assertEqual((rc, m.get("format")), (0, "old"))
        self.assertTrue(m["tweet_text"].startswith(f"How it actually went \u2014 {D}:"))
        self.assertNotIn("skipped_bad_config", out)

    def test_module_reads_tiers_from_config(self):
        import shutil
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="tweetfmt-tiers-"))
        self.addCleanup(shutil.rmtree, d, True)
        probe = "import sys; sys.path.insert(0, 'operations/scripts'); import tweet_format as tf; print(tf.REPLY_TIERS, bool(tf.TIERS_ERROR))"
        cfg = json.loads((ROOT / "config" / "tweet_format.json").read_text())
        for a, b, want in ((None, None, "None True"), (1.5, 4, "(1.5, 4.0) False")):
            (d / "c.json").write_text(json.dumps({**cfg, "reply_tier_a": a, "reply_tier_b": b}))
            r = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                               env={**os.environ, "TPCR_TWEET_FORMAT_CONFIG": str(d / "c.json")})
            self.assertEqual(r.stdout.strip(), want, r.stderr)
