"""TPCR-TWEET-V2: timing rule, reply-target lookup, dry-run (no X writes), alt text (fail closed), #494 guard,
silent on missing data, tie naming, and the closed row (TPCR-TWEET-V2-CLOSED-ROW-001)."""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operations" / "scripts"))
sys.path.insert(0, str(ROOT / "operations" / "tweet_v2"))
sys.modules.setdefault("requests_oauthlib", type(sys)("requests_oauthlib")).OAuth1 = lambda *a: "auth"

import requests  # noqa: E402

import tweet_guard as tg  # noqa: E402
import tweet_v2  # noqa: E402
import wti_observed_tweet as wot  # noqa: E402
import wti_predicted_tweet as wpt  # noqa: E402

REAL_PARQUET_LAST_DATE = tweet_v2.parquet_last_date

GFIX = Path(__file__).resolve().parent / "fixtures" / "tweet_guard"
PARKS = [("MK", "Magic Kingdom"), ("EP", "EPCOT"), ("HS", "Hollywood Studios"), ("AK", "Animal Kingdom")]
D = "2026-10-28"


GEN = "2026-10-27T10:00:00-04:00"  # content generated_at for D (fresh at NOW)
NOW = "2026-10-27T16:00:00-04:00"  # 4 PM ET on D-1, the forecast run


def content(d, vals, status="ready", generated_at=GEN):
    return json.dumps({"status": status, "target_date": d, "generated_at": generated_at,
                       "parks": [{"park_code": c, "park_name": n, "wti": v} for (c, n), v in zip(PARKS, vals)]})


def keep_requests():
    """Dry-run monkeypatches the requests module; restore it after each test."""
    names = ("get", "post", "put", "delete", "patch", "request")
    saved = {n: getattr(requests, n) for n in names}
    saved_sess = requests.Session.request

    def restore():
        for n, f in saved.items():
            setattr(requests, n, f)
        requests.Session.request = saved_sess
    return restore


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tweetv2-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(keep_requests())
        from datetime import datetime
        p1 = mock.patch.object(tweet_v2, "now", return_value=datetime.fromisoformat(NOW))
        p2 = mock.patch.object(tweet_v2, "parquet_last_date", return_value=D)  # fake history "h" ends on D
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        self.cdir = self.tmp / "content"
        self.cdir.mkdir()
        (self.cdir / f"predicted_{D}.json").write_text(content(D, [20.1, 21.0, 26.0, 33.3]))
        (self.cdir / f"observed_{D}.json").write_text(content(D, [19.0, 22.5, 25.0, 27.0]))
        self.state = self.tmp / "tweet_state.json"
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": "1850000000000000001"}}}))

    def fake_build(self, d, out, history, pred, obs=None, park_hours=None, closures=None):
        out = Path(out)
        arts = {}
        metas = {"F": {"date": d, "headline": "Animal Kingdom busy", "named": ["Animal Kingdom"], "dirs": ["busy"],
                       "open": [n for _, n in PARKS], "closed": [],
                       "parks": {"Animal Kingdom": {"wti": 33.3, "lo": 20.9, "hi": 27.7}}},
                 "R": {"date": d, "headline": "Three parks on target. Animal Kingdom 6.3 low"}}
        for k in (["F"] if obs is None else ["F", "R"]):
            (out / f"{k}-{d}.mp4").write_bytes(b"mp4")
            arts[k] = {"mp4": out / f"{k}-{d}.mp4", "png": out / f"{k}-{d}.png", "alt": f"{k} alt", "meta": metas[k]}
        self.built = (d, pred, obs)
        return arts

    def run_main(self, mod, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mod.main(argv)
        return rc, out.getvalue(), err.getvalue()


class TimingRule(unittest.TestCase):
    def test_forecast_is_for_tomorrow_reply_is_for_yesterday(self):
        today = date(2026, 10, 27)
        self.assertEqual(tweet_v2.default_reference_date("predicted", today), "2026-10-28")
        self.assertEqual(tweet_v2.default_reference_date("observed", today), "2026-10-26")

    def test_predicted_never_falls_back_to_today(self):
        cdir = Path(tempfile.mkdtemp(prefix="tweetv2-test-"))
        self.addCleanup(shutil.rmtree, cdir, True)
        (cdir / "predicted_2026-10-27.json").write_text(content("2026-10-27", [1, 2, 3, 4]))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wpt.load_content("2026-10-28", str(cdir)), (None, None))


class ReplyTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tweetv2-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sf = self.tmp / "s.json"

    def test_by_reference_day(self):
        self.sf.write_text(json.dumps({"predicted": {"2026-10-27": {"tweet_id": "A"}, "2026-10-28": {"tweet_id": "B"}},
                                       "last_predicted": {"reference_date": "2026-10-29", "tweet_id": "C"}}))
        self.assertEqual(tweet_v2.lookup_predicted_tweet(self.sf, "2026-10-28")[0], "B")
        self.assertEqual(tweet_v2.lookup_predicted_tweet(self.sf, "2026-10-29")[0], "C")
        self.assertIsNone(tweet_v2.lookup_predicted_tweet(self.sf, "2026-10-30")[0])

    def test_missing_or_bad_state(self):
        self.assertIsNone(tweet_v2.lookup_predicted_tweet(self.sf, D)[0])
        self.sf.write_text("{nope")
        self.assertIsNone(tweet_v2.lookup_predicted_tweet(self.sf, D)[0])


class DryRun(Base):
    def test_predicted_dry_run_posts_nothing(self):
        before = self.state.read_text()
        with mock.patch.object(tweet_v2, "build", self.fake_build):
            rc, out, _ = self.run_main(wpt, ["--dry-run", "--date", D, "--content-dir", str(self.cdir),
                                             "--history", "h.parquet", "--state-file", str(self.tmp / "none.json"),
                                             "--out", str(self.tmp / "o")])
        self.assertEqual(rc, 0)
        m = json.loads((self.tmp / "o" / "dry_run.json").read_text())
        self.assertEqual(m["posts_at"], "2026-10-27 16:00 America/New_York")
        self.assertEqual(m["alt_text"], "F alt")
        self.assertIsNone(self.built[2])
        self.assertEqual(self.state.read_text(), before)
        with self.assertRaises(tweet_v2.DryRunWrite):
            requests.post("https://api.twitter.com/2/tweets", json={})

    def test_observed_dry_run_computes_reply_target(self):
        with mock.patch.object(tweet_v2, "build", self.fake_build):
            rc, out, _ = self.run_main(wot, ["--dry-run", "--date", D, "--content-dir", str(self.cdir),
                                             "--history", "h.parquet", "--state-file", str(self.state),
                                             "--out", str(self.tmp / "o")])
        self.assertEqual(rc, 0)
        m = json.loads((self.tmp / "o" / "dry_run.json").read_text())
        self.assertEqual(m["reply_to_tweet_id"], "1850000000000000001")
        self.assertEqual(m["posts_at"], "2026-10-29 08:30 America/New_York")
        self.assertEqual(m["alt_text"], "R alt")
        self.assertEqual(m["tweet_text"], "Three parks on target. Animal Kingdom 6.3 low. Yesterday, Wed Oct 28, vs our forecast. Bar = observed − forecast.")
        self.assertNotIn("observed", json.loads(self.state.read_text()))

    def test_dry_run_needs_no_credentials_and_skips_env(self):
        with mock.patch.object(tweet_v2, "build", self.fake_build), \
             mock.patch.object(wpt, "load_env", side_effect=AssertionError("dry-run read ~/.env")):
            rc, _, _ = self.run_main(wpt, ["--dry-run", "--date", D, "--content-dir", str(self.cdir),
                                           "--history", "h", "--state-file", str(self.tmp / "n.json"),
                                           "--out", str(self.tmp / "o")])
        self.assertEqual(rc, 0)


class Guard494(Base):
    """The #494 mutual-exclusion guard still runs first on every live run of both scripts."""

    def loaded(self, argv):
        return (GFIX / "launchctl_loaded.txt").read_text(), ""

    def test_live_runs_blocked_when_local_launchd_loaded(self):
        for mod in (wpt, wot):
            post = mock.Mock(side_effect=AssertionError("posted despite guard"))
            with mock.patch.object(tg, "read_launchctl", lambda runner=None: self.loaded(None)), \
                 mock.patch.object(mod, "load_env"), mock.patch.object(requests, "post", post), \
                 mock.patch.object(tweet_v2, "build", side_effect=AssertionError("built despite guard")):
                with self.assertRaises(SystemExit) as cm:
                    self.run_main(mod, ["--date", D, "--content-dir", str(self.cdir), "--state-file", str(self.state)])
            self.assertEqual(cm.exception.code, 3, mod.__name__)
            post.assert_not_called()

    def test_dry_run_reports_guard_status_without_enforcing(self):
        with mock.patch.object(tg, "read_launchctl", lambda runner=None: self.loaded(None)), \
             mock.patch.object(tg, "read_crontab", lambda runner=None: ("", "")), \
             mock.patch.object(tweet_v2, "build", self.fake_build):
            rc, _, _ = self.run_main(wot, ["--dry-run", "--date", D, "--content-dir", str(self.cdir),
                                           "--history", "h", "--state-file", str(self.state), "--out", str(self.tmp / "o")])
        m = json.loads((self.tmp / "o" / "dry_run.json").read_text())
        self.assertIn("allowed_legacy: no", m["guard_status"])
        self.assertEqual(rc, 0)


class LivePosting(Base):
    def resp(self, code, body):
        r = mock.Mock(status_code=code, text=json.dumps(body))
        r.json.return_value = body
        return r

    def test_observed_uploads_mp4_with_alt_text_replies_and_is_idempotent(self):
        calls = []

        def post(url, **kw):
            calls.append((url, kw))
            if url == wot.TWITTER_UPLOAD_API:
                return self.resp(200, {"media_id_string": "M1"})
            if url == tweet_v2.TWITTER_METADATA_API:
                return self.resp(200, {})
            return self.resp(201, {"data": {"id": "OBS1"}})
        env = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")}
        with mock.patch.object(wot, "load_env"), mock.patch.object(wot, "enforce"), \
             mock.patch.dict("os.environ", env), mock.patch.object(requests, "post", post), \
             mock.patch.object(tweet_v2, "build", self.fake_build), mock.patch.object(wot, "post_discord"):
            self.run_main(wot, ["--date", D, "--content-dir", str(self.cdir), "--history", "h", "--state-file", str(self.state)])
            meta = [kw["json"] for u, kw in calls if u == tweet_v2.TWITTER_METADATA_API]
            self.assertEqual(meta, [{"media_id": "M1", "alt_text": {"text": "R alt"}}])
            tweet = [kw["json"] for u, kw in calls if u == wot.TWITTER_API]
            self.assertEqual(tweet[0]["reply"], {"in_reply_to_tweet_id": "1850000000000000001"})
            self.assertEqual(tweet[0]["media"], {"media_ids": ["M1"]})
            self.assertEqual(json.loads(self.state.read_text())["observed"][D]["tweet_id"], "OBS1")
            n = len(calls)
            _, out, _ = self.run_main(wot, ["--date", D, "--content-dir", str(self.cdir), "--history", "h", "--state-file", str(self.state)])
            self.assertEqual(len(calls), n)
            self.assertIn("Already posted observed", out)


    def live(self, mod, post, build=None, argv=None):
        env = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")}
        with mock.patch.object(mod, "load_env"), mock.patch.object(mod, "enforce"), \
             mock.patch.dict("os.environ", env), mock.patch.object(requests, "post", post), \
             mock.patch.object(tweet_v2, "build", build or self.fake_build), mock.patch.object(mod, "post_discord"):
            try:
                return self.run_main(mod, argv or ["--date", D, "--content-dir", str(self.cdir), "--history", "h",
                                                   "--state-file", str(self.state)])
            except SystemExit as e:
                return e.code, "", ""

    def test_alt_text_failure_means_no_tweet(self):
        """Fail closed: metadata/create fails -> no tweet, for both scripts."""
        for mod in (wpt, wot):
            calls = []

            def post(url, **kw):
                calls.append(url)
                if url == mod.TWITTER_UPLOAD_API:
                    return self.resp(200, {"media_id_string": "M1"})
                if url == tweet_v2.TWITTER_METADATA_API:
                    return self.resp(400, {"errors": ["bad"]})
                raise AssertionError("tweeted without alt text")
            self.state.write_text(json.dumps({"predicted": {"2026-01-01": {"tweet_id": "1"}, D: {"tweet_id": "P1"}}})
                                  if mod is wot else "{}")
            rc, _, _ = self.live(mod, post)
            self.assertEqual(rc, 1, mod.__name__)
            self.assertNotIn(mod.TWITTER_API, calls)

    def test_silent_on_missing_data_days(self):
        """Fred, Oct 26: 'Stay silent on missing-data days'. No content, no card or no forecast tweet -> no post."""
        post = mock.Mock(side_effect=AssertionError("posted on a missing-data day"))
        (self.cdir / f"predicted_{D}.json").unlink()
        for mod in (wpt, wot):
            self.live(mod, post)
        (self.cdir / f"predicted_{D}.json").write_text(content(D, [20.1, 21.0, 26.0, None]))
        no_card = mock.Mock(side_effect=RuntimeError("cards.py: AK forecast wti missing"))
        self.state.write_text("{}")
        self.live(wpt, post, build=no_card)
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": "P1"}}}))
        self.live(wot, post, build=no_card)
        self.state.write_text("{}")  # card fine, but no forecast tweet to reply to
        self.live(wot, post)
        post.assert_not_called()

    def test_all_parks_closed_skips_both_posts(self):
        """Closed-row R1: every park closed -> no forecast, no reply, log skipped_all_closed."""
        post = mock.Mock(side_effect=AssertionError("posted with every park closed"))
        allclosed = mock.Mock(return_value={"skipped_all_closed": True})
        for mod in (wpt, wot):
            self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": "P1"}}}) if mod is wot else "{}")
            rc, out, _ = self.live(mod, post, build=allclosed)
            self.assertIn("skipped_all_closed", out, mod.__name__)
        post.assert_not_called()

    def test_skipped_no_data_exits_0_and_alerts_after_two_days(self):
        """AC 5a: missing content -> exit 0, log skipped_no_data; one alert on the 2nd consecutive day (state persisted)."""
        from datetime import timedelta
        post = mock.Mock(side_effect=AssertionError("posted on a no-data day"))
        d0 = date.fromisoformat(D)
        for mod, kind in ((wpt, "predicted"), (wot, "observed")):
            for f in self.cdir.iterdir():
                f.unlink()
            sent = []
            for i in range(3):
                day = (d0 + timedelta(days=i)).isoformat()
                with mock.patch.object(mod, "post_discord", side_effect=sent.append):
                    env = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET",
                                            "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")}
                    with mock.patch.object(mod, "load_env"), mock.patch.object(mod, "enforce"), \
                         mock.patch.dict("os.environ", env), mock.patch.object(requests, "post", post):
                        rc, out, _ = self.run_main(mod, ["--date", day, "--content-dir", str(self.cdir),
                                                         "--history", "h", "--state-file", str(self.state)])
                self.assertEqual(rc, 0, kind)
                self.assertIn("skipped_no_data", out)
                self.assertEqual(len(sent), 0 if i == 0 else 1, (kind, i))  # one alert, on day 2 only
            st = json.loads(tweet_v2.no_data_state_path(self.state).read_text())
            self.assertEqual(st[kind]["streak"], 3)
            # a gap resets the streak: no new alert on the next isolated skip
            self.assertEqual(tweet_v2.record_no_data(kind, (d0 + timedelta(days=10)).isoformat(), self.state,
                                                     sent.append), 1)
            self.assertEqual(len(sent), 1)
            tweet_v2.clear_no_data(kind, self.state)
            self.assertNotIn(kind, json.loads(tweet_v2.no_data_state_path(self.state).read_text()))
        post.assert_not_called()

    def test_stale_forecast_and_observed_skip_exit_0_with_alert(self):
        """AC 16: Sep 4 content for today's target and a parquet ending Sep 4 both skip (skipped_stale, exit 0, alert)."""
        post = mock.Mock(side_effect=AssertionError("posted stale data"))
        build = mock.Mock(side_effect=AssertionError("built stale data"))
        (self.cdir / f"predicted_{D}.json").write_text(content(D, [20.1, 21.0, 26.0, 33.3],
                                                                generated_at="2026-09-04T06:00:00-04:00"))
        self.state.write_text("{}")
        sent = []
        with mock.patch.object(wpt, "post_discord", side_effect=sent.append):
            rc, out = self.live_alerts(wpt, post, build)
        self.assertEqual(rc, 0); self.assertIn("skipped_stale", out); self.assertEqual(len(sent), 1)
        self.assertIn("skipped_stale", sent[0])
        (self.cdir / f"predicted_{D}.json").write_text(content(D, [20.1, 21.0, 26.0, 33.3]))
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": "P1"}}}))
        sent.clear()
        with mock.patch.object(tweet_v2, "parquet_last_date", return_value="2026-09-04"), \
             mock.patch.object(wot, "post_discord", side_effect=sent.append):
            rc, out = self.live_alerts(wot, post, build)
        self.assertEqual(rc, 0); self.assertIn("skipped_stale", out); self.assertEqual(len(sent), 1)
        build.assert_not_called(); post.assert_not_called()

    def test_forecast_staleness_rules(self):
        from datetime import datetime
        at = datetime.fromisoformat(NOW)
        ok = content(D, [1, 2, 3, 4])
        self.assertIsNone(tweet_v2.forecast_staleness(ok, D, at))
        self.assertIn("old", tweet_v2.forecast_staleness(content(D, [1, 2, 3, 4], generated_at="2026-10-26T03:00:00-04:00"), D, at))
        self.assertIn("minus 1 day", tweet_v2.forecast_staleness(content(D, [1, 2, 3, 4], generated_at="2026-10-26T23:00:00-04:00"), D, at))
        self.assertIn("missing", tweet_v2.forecast_staleness(json.dumps({"target_date": D}), D, at))

    def test_observed_staleness_reads_parquet_last_date(self):
        try:
            import pandas as pd
            pd.DataFrame({"park_date": ["2026-09-03", "2026-09-04"]}).to_parquet(self.tmp / "w.parquet")
        except ImportError:
            self.skipTest("pandas/pyarrow not installed")
        with mock.patch.object(tweet_v2, "parquet_last_date", REAL_PARQUET_LAST_DATE):
            self.assertEqual(tweet_v2.parquet_last_date(self.tmp / "w.parquet"), "2026-09-04")
            self.assertIn("2026-09-04 != target", tweet_v2.observed_staleness(self.tmp / "w.parquet", D))
            self.assertIsNone(tweet_v2.observed_staleness(self.tmp / "w.parquet", "2026-09-04"))
            self.assertIn("unreadable", tweet_v2.observed_staleness(self.tmp / "missing.parquet", D))

    def test_park_hours_unreadable_alerts_once(self):
        """AC 2a: unreadable hours -> one park_hours_unreadable alert per run; nothing shows Closed."""
        sent = []
        self.state.write_text("{}")
        with mock.patch.object(wpt, "post_discord", side_effect=sent.append):
            self.live_alerts(wpt, mock.Mock(side_effect=AssertionError("no post expected")),
                             mock.Mock(side_effect=RuntimeError("stop after checks")),
                             extra=["--park-hours", str(self.tmp / "no-such-hours.csv")])
        self.assertEqual(sum("park_hours_unreadable" in m for m in sent), 1)
        import park_status as ps
        self.assertEqual(set(ps.load_statuses(str(self.tmp / "no-such-hours.csv"), ["MK", "EP"], D).values()), {"unknown"})

    def live_alerts(self, mod, post, build, extra=()):
        """Live run with post_discord left to the caller's patch."""
        env = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")}
        with mock.patch.object(mod, "load_env"), mock.patch.object(mod, "enforce"), \
             mock.patch.dict("os.environ", env), mock.patch.object(requests, "post", post), \
             mock.patch.object(tweet_v2, "build", build):
            try:
                rc, out, _ = self.run_main(mod, ["--date", D, "--content-dir", str(self.cdir), "--history", "h",
                                                 "--state-file", str(self.state), *extra])
            except SystemExit as e:
                return e.code, ""
        return rc, out

    def test_api_error_still_exits_1(self):
        """AC 5a: crashes and API errors are not skips; exit 1 is unchanged."""
        def post(url, **kw):
            return self.resp(500, {"errors": ["down"]})
        self.state.write_text("{}")
        rc, _, _ = self.live(wpt, post)
        self.assertEqual(rc, 1)

    # --- Kitty T2 gaps (A04, A05, A06, A08, F05, F10, H03) ---
    def nodata_run(self, mod, day, sent, extra=(), build=None):
        """Live run for one reference day; alerts go to sent; posting is an error unless state says already posted."""
        post = mock.Mock(side_effect=AssertionError("posted in a streak test"))
        env = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")}
        with mock.patch.object(mod, "post_discord", side_effect=sent.append), mock.patch.object(mod, "load_env"), \
             mock.patch.object(mod, "enforce"), mock.patch.dict("os.environ", env), \
             mock.patch.object(requests, "post", post), mock.patch.object(tweet_v2, "build", build or self.fake_build):
            return self.run_main(mod, ["--date", day, "--content-dir", str(self.cdir), "--history", "h",
                                       "--state-file", str(self.state), *extra])

    def run_sequence(self, steps):
        """Run (day_offset, 'skip'|'stale'|'ok') steps through both scripts; return {kind: [no-data alerts]}.
        Each day's run happens at 4 PM ET on day-1; 'ok' content is fresh and the build stops before any post."""
        from datetime import datetime, timedelta
        d0 = date.fromisoformat(D)
        result = {}
        for mod, kind in ((wpt, "predicted"), (wot, "observed")):
            self.state.write_text("{}")
            tweet_v2.no_data_state_path(self.state).unlink(missing_ok=True)
            sent = []
            for off, what in steps:
                day = (d0 + timedelta(days=off)).isoformat()
                prev = (d0 + timedelta(days=off - 1)).isoformat()
                for f in self.cdir.iterdir():
                    f.unlink()
                gen = "2026-09-04T06:00:00-04:00" if what == "stale" and kind == "predicted" else f"{prev}T10:00:00-04:00"
                if what != "skip":
                    (self.cdir / f"predicted_{day}.json").write_text(content(day, [20, 21, 26, 33], generated_at=gen))
                    (self.cdir / f"observed_{day}.json").write_text(content(day, [19, 22, 25, 27]))
                last = "2026-09-04" if what == "stale" else day
                stop = mock.Mock(side_effect=RuntimeError("stop before posting"))
                with mock.patch.object(tweet_v2, "now", return_value=datetime.fromisoformat(f"{prev}T16:00:00-04:00")), \
                     mock.patch.object(tweet_v2, "parquet_last_date", return_value=last):
                    rc, out, _ = self.nodata_run(mod, day, sent, build=stop)
                self.assertEqual(rc if what != "ok" else 0, 0, (kind, off, what))
                want = {"skip": "skipped_no_data", "stale": "skipped_stale", "ok": "stop before posting"}[what]
                if what != "ok":
                    self.assertIn(want, out, (kind, off, what))
            result[kind] = [m for m in sent if "skipped_no_data" in m]
        return result

    def test_ac5b_skip_stale_skip_sends_no_data_alert(self):
        """AC 5b (R5): a stale day neither counts nor resets the streak: skip, stale, skip -> one no-data alert."""
        for kind, alerts in self.run_sequence([(0, "skip"), (1, "stale"), (2, "skip")]).items():
            self.assertEqual(len(alerts), 1, kind)

    def test_ac5b_skip_ok_skip_sends_no_alert(self):
        """AC 5b (R5): a fresh data day ends the streak: skip, ok, skip -> no no-data alert."""
        for kind, alerts in self.run_sequence([(0, "skip"), (1, "ok"), (2, "skip")]).items():
            self.assertEqual(alerts, [], kind)

    def test_ac5b_stale_day_is_not_counted_as_a_skip(self):
        """B04: skip, stale -> no alert (a stale day must not count as a second no-data day)."""
        for kind, alerts in self.run_sequence([(0, "skip"), (1, "stale")]).items():
            self.assertEqual(alerts, [], kind)
        for kind, alerts in self.run_sequence([(0, "skip"), (1, "skip"), (2, "stale"), (3, "skip")]).items():
            self.assertEqual(len(alerts), 1, kind)

    def test_ac5b_stale_day_after_a_gap_does_not_bridge(self):
        """B05: skip D, (no run D+1), stale D+2, skip D+3 -> no alert: a stale day bridges only the next day."""
        for kind, alerts in self.run_sequence([(0, "skip"), (2, "stale"), (3, "skip")]).items():
            self.assertEqual(alerts, [], kind)

    def test_ac5b_dry_stale_run_writes_no_bridge_state(self):
        """B06: a dry stale run must not write bridge state; the same call live does."""
        p = tweet_v2.no_data_state_path(self.state)
        seed = json.dumps({k: {"last_skipped": "2026-10-27", "streak": 1, "alerted": False}
                           for k in ("predicted", "observed")}, indent=2) + "\n"
        p.write_text(seed)
        tweet_v2.record_stale("observed", "2026-10-28", "test", lambda m: None, self.state, True)
        self.assertEqual(p.read_text(), seed)
        tweet_v2.record_stale("observed", "2026-10-28", "test", lambda m: None, self.state, False)
        self.assertEqual(json.loads(p.read_text())["observed"]["bridged_to"], "2026-10-28")  # live run does bridge

    def test_data_rerun_clears_streak_so_next_skip_does_not_alert(self):
        """A05/A06: skip D, rerun D with data, skip D+1 -> streak restarts at 1, no 2-day alert (both scripts)."""
        for kind, alerts in self.run_sequence([(0, "skip"), (0, "ok"), (1, "skip")]).items():
            self.assertEqual(alerts, [], kind)

    def test_same_day_skip_rerun_does_not_double_count(self):
        """A04: two no-data runs for the same day keep streak 1 and send no alert."""
        (self.cdir / f"predicted_{D}.json").unlink()
        sent = []
        for _ in range(2):
            rc, _, _ = self.nodata_run(wpt, D, sent)
            self.assertEqual(rc, 0)
        self.assertEqual(sent, [])
        self.assertEqual(json.loads(tweet_v2.no_data_state_path(self.state).read_text())["predicted"]["streak"], 1)

    def test_dry_run_no_data_writes_no_streak_state(self):
        """A08: a dry-run skip must not create or change the no-data streak file."""
        for f in self.cdir.iterdir():
            f.unlink()
        p = tweet_v2.no_data_state_path(self.state)
        for mod in (wpt, wot):
            rc, out, _ = self.run_main(mod, ["--dry-run", "--date", D, "--content-dir", str(self.cdir), "--history", "h",
                                             "--state-file", str(self.state), "--out", str(self.tmp / "dry")])
            self.assertEqual(rc, 0); self.assertIn("skipped_no_data", out)
            self.assertFalse(p.exists(), mod.__name__)
        p.write_text(json.dumps({"predicted": {"last_skipped": "2026-10-27", "streak": 1, "alerted": False}}) + "\n")
        before = p.read_text()
        self.run_main(wpt, ["--dry-run", "--date", D, "--content-dir", str(self.cdir), "--history", "h",
                            "--state-file", str(self.state), "--out", str(self.tmp / "dry")])
        self.assertEqual(p.read_text(), before)

    def test_naive_generated_at_is_stale(self):
        """F05: generated_at with no timezone is stale (fail safe), even when its wall time looks fresh."""
        from datetime import datetime
        why = tweet_v2.forecast_staleness(content(D, [1, 2, 3, 4], generated_at="2026-10-27T10:00:00"), D,
                                          datetime.fromisoformat(NOW))
        self.assertIsNotNone(why)
        self.assertIn("timezone", why)

    def test_parquet_ending_after_target_is_stale(self):
        """F10: wti.parquet whose last date is after D is not 'ends on D' -> skipped_stale, no build, no post."""
        with mock.patch.object(tweet_v2, "parquet_last_date", return_value="2026-10-29"):
            self.assertIn("2026-10-29 != target", tweet_v2.observed_staleness("h", D))
            sent = []
            build = mock.Mock(side_effect=AssertionError("built on a parquet ending after D"))
            with mock.patch.object(wot, "post_discord", side_effect=sent.append):
                rc, out = self.live_alerts(wot, mock.Mock(side_effect=AssertionError("posted")), build)
        self.assertEqual(rc, 0); self.assertIn("skipped_stale", out); self.assertEqual(len(sent), 1)
        build.assert_not_called()

    def test_observed_park_hours_unreadable_alerts_once(self):
        """H03: the observed script also checks park hours: one park_hours_unreadable alert per run."""
        sent = []
        with mock.patch.object(wot, "post_discord", side_effect=sent.append):
            self.live_alerts(wot, mock.Mock(side_effect=AssertionError("no post expected")),
                             mock.Mock(side_effect=RuntimeError("stop after checks")),
                             extra=["--park-hours", str(self.tmp / "no-such-hours.csv")])
        self.assertEqual(sum("park_hours_unreadable" in m for m in sent), 1)



class Closures(unittest.TestCase):
    """Closed-row R3 (AC 1b-1d): data/closures.csv is the only closure source, and it is strict."""
    H = "park,date,source_url,note\n"

    def setUp(self):
        import park_status
        self.ps = park_status
        self.tmp = Path(tempfile.mkdtemp(prefix="tweetv2-closures-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def f(self, body):
        p = self.tmp / "closures.csv"
        p.write_text(self.H + body)
        return p

    def test_committed_file_is_header_only(self):
        self.assertEqual(self.ps.DEFAULT_CLOSURES.read_text(), self.H)
        self.assertEqual(self.ps.load_closures(self.ps.DEFAULT_CLOSURES), set())

    def test_empty_file_gives_no_closed_parks(self):
        cl = self.ps.load_closures(self.f(""))
        self.assertEqual(cl, set())
        self.assertNotIn("closed", self.ps.load_statuses(None, ["MK", "EP", "HS", "AK"], D, cl).values())

    def test_one_row_closes_that_park_that_date_only(self):
        cl = self.ps.load_closures(self.f(f"HS,{D},https://disneyworld.disney.go.com/alerts/,hurricane\n"))
        self.assertEqual(self.ps.load_statuses(None, ["MK", "EP", "HS", "AK"], D, cl),
                         {"MK": "open", "EP": "open", "HS": "closed", "AK": "open"})
        self.assertEqual(self.ps.park_status(None, "HS", "2026-10-29", cl), "open")

    def test_bad_url_unknown_park_bad_date_and_missing_file_are_errors(self):
        bad = [f"HS,{D},http://example.com/x,plain http\n", f"HS,{D},,no url\n", f"HS,{D},not a url,\n",
               f"XX,{D},https://example.com/x,unknown park\n", "HS,2026-13-01,https://example.com/x,bad date\n",
               "HS,10/28/2026,https://example.com/x,bad date\n"]
        for body in bad:
            with self.assertRaises(self.ps.ClosuresError, msg=body):
                self.ps.load_closures(self.f(body))
        with self.assertRaises(self.ps.ClosuresError):
            self.ps.load_closures(self.tmp / "missing.csv")

    def test_tokyo_date_edge(self):
        from datetime import datetime, timezone
        cl = self.ps.load_closures(self.f("TDL,2026-10-28,https://www.tokyodisneyresort.jp/,typhoon\n"
                                          "MK,2026-10-28,https://disneyworld.disney.go.com/,hurricane\n"))
        at = datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)  # 11 PM ET Oct 27 = noon Oct 28 in Tokyo
        self.assertTrue(self.ps.is_closed(cl, "TDL", at))
        self.assertFalse(self.ps.is_closed(cl, "MK", at))  # still Oct 27 at MK

    def test_scripts_exit_1_with_alert_before_any_post(self):
        """AC 1b/1c end to end: bad or missing closures file -> exit 1, alert sent, nothing built or posted."""
        for body in (f"HS,{D},http://bad,x\n", None):
            path = self.f(body) if body else self.tmp / "gone.csv"
            for mod in (wpt, wot):
                sent, build = [], mock.Mock(side_effect=AssertionError("built despite bad closures"))
                post = mock.Mock(side_effect=AssertionError("posted despite bad closures"))
                env = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET",
                                        "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")}
                with mock.patch.object(mod, "load_env"), mock.patch.object(mod, "enforce"), \
                     mock.patch.dict("os.environ", env), mock.patch.object(requests, "post", post), \
                     mock.patch.object(tweet_v2, "build", build), \
                     mock.patch.object(mod, "post_discord", side_effect=sent.append), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        mod.main(["--date", D, "--content-dir", str(self.tmp), "--history", "h",
                                  "--state-file", str(self.tmp / "s.json"), "--closures", str(path)])
                self.assertEqual(cm.exception.code, 1)
                self.assertEqual(len(sent), 1)
                self.assertIn("closures_error", sent[0])
                build.assert_not_called()
                post.assert_not_called()


class Cards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import pandas  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pandas not installed")
        import cards
        cls.c = cards

    S = {p: [10.0, 20.0] for p in ("MK", "EP", "HS", "AK")}

    def test_headline_and_kicker_drop_today(self):
        S = self.S
        self.assertEqual(self.c.forecast_headline({p: 15.0 for p in self.c.P}, S), "All four parks normal")
        fc = {"MK": 15.0, "EP": 15.0, "HS": 15.0, "AK": 30.0}
        self.assertEqual(self.c.forecast_headline(fc, S), "Animal Kingdom busy")
        _, html, alt = self.c.forecast_html(D, fc, S)
        self.assertIn('<div class="k">Wednesday, Oct 28</div>', html)
        self.assertIn("shared scale, 10–35", html)
        self.assertNotIn("Today", html)
        self.assertNotIn("today", html)
        self.assertLessEqual(len(alt), self.c.ALT_MAX)

    def test_tie_names_both_parks_in_mk_ep_hs_ak_order(self):
        fc = {"MK": 15.0, "EP": 5.0, "HS": 5.0, "AK": 15.0}  # EP and HS both 5 below their box
        self.assertEqual(self.c.forecast_headline(fc, self.S), "EPCOT and Hollywood Studios quiet")
        fc = {"MK": 25.0, "EP": 15.0, "HS": 15.0, "AK": 5.0}  # MK busy, AK quiet, both 5 out: MK first
        self.assertEqual(self.c.forecast_headline(fc, self.S), "Magic Kingdom busy, Animal Kingdom quiet")
        import tweet_copy
        m = self.c.meta(D, {"MK": 15.0, "EP": 5.0, "HS": 5.0, "AK": 15.0}, self.S, {}, "x")
        self.assertTrue(tweet_copy.forecast_text(m).startswith("EPCOT and Hollywood Studios look quiet. Tomorrow, Wed Oct 28,"))

    def test_closed_row_and_exclusions(self):
        """AC 1-2, 8, 10, 11: Closed row with no value; closed park never picked or a tie partner; unknown never Closed."""
        fc = {"MK": 15.0, "EP": 5.0, "HS": 5.0, "AK": None}
        st = {"MK": "open", "EP": "open", "HS": "closed", "AK": "unknown"}
        self.assertEqual(self.c.forecast_headline(fc, self.S, st), "EPCOT quiet")
        _, html, alt = self.c.forecast_html(D, {**fc, "HS": 99.0, "AK": 15.0}, self.S, {**st, "AK": "open"})
        self.assertEqual(html.count(">Closed<"), 1)
        self.assertNotIn("99.0", html)
        self.assertIn("Hollywood Studios closed", alt)
        self.assertNotIn("Hollywood Studios 99", alt)

    def test_one_open_park_line_names_it(self):
        st = {"MK": "closed", "EP": "closed", "HS": "closed", "AK": "open"}
        self.assertEqual(self.c.forecast_headline({"AK": 30.0}, self.S, st), "Animal Kingdom busy")

    def test_reply_card_closed_row(self):
        fc = {"MK": 15.0, "EP": 15.0, "HS": None, "AK": 15.0}
        ob = {"MK": 15.5, "EP": 14.0, "HS": None, "AK": 21.0}
        st = {"HS": "closed"}
        t, html, alt = self.c.reply_html(D, fc, ob, st)
        self.assertEqual(t, "Animal Kingdom 6.0 high. Two parks on target")
        self.assertNotIn("Hollywood Studios", t)
        self.assertEqual(html.count(">Closed<"), 1)
        self.assertIn("Hollywood Studios closed", alt)

    def test_tie_next_to_closed_park_names_both_open_parks(self):
        """A tie between two open parks with a closed park at the same value: name both open parks, MK EP HS AK order."""
        fc = {"MK": 5.0, "EP": 15.0, "HS": 5.0, "AK": 5.0}
        st = {"MK": "open", "EP": "open", "HS": "closed", "AK": "open"}
        self.assertEqual(self.c.forecast_headline(fc, self.S, st), "Magic Kingdom and Animal Kingdom quiet")
        st = {"MK": "closed", "EP": "open", "HS": "open", "AK": "open"}
        fc = {"MK": 25.0, "EP": 25.0, "HS": 15.0, "AK": 25.0}
        self.assertEqual(self.c.forecast_headline(fc, self.S, st), "EPCOT and Animal Kingdom busy")

    def test_fallback_headlines_come_from_one_constants_module(self):
        import headline_copy as hc
        self.assertEqual(hc.ALL_OPEN_NORMAL, "All open parks normal")
        self.assertEqual(hc.ONE_PARK_NORMAL.format(park="EPCOT"), "EPCOT normal")
        self.assertEqual(hc.ALL_OPEN_ON_TARGET, "All open parks within 2 of forecast")
        st = {"MK": "closed", "EP": "open", "HS": "open", "AK": "open"}
        self.assertEqual(self.c.forecast_headline({p: 15.0 for p in self.c.P}, self.S, st), hc.ALL_OPEN_NORMAL)
        self.assertEqual(self.c.forecast_headline({"EP": 15.0}, self.S, {"EP": "open", "MK": "closed",
                         "HS": "closed", "AK": "closed"}), "EPCOT normal")
        fc = {p: 15.0 for p in self.c.P}
        self.assertEqual(self.c.reply_headline(fc, {**fc, "MK": 16.0}, {"HS": "closed"}), hc.ALL_OPEN_ON_TARGET)
        self.assertEqual(self.c.reply_headline(fc, fc), hc.ALL_FOUR_ON_TARGET)

    def test_missing_official_row_is_unknown_never_closed(self):
        """R2: no official row (absent, or only an unofficial row) -> unknown, even with a closure marker set."""
        import park_status as ps
        tmp = Path(tempfile.mkdtemp(prefix="tweetv2-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        f = tmp / "dimparkhours.csv"
        f.write_text("park,date,opening_time,closing_time,is_official,flag\n"
                     "MK,2026-10-28,2026-10-28T09:00:00-04:00,2026-10-28T22:00:00-04:00,True,\n"
                     "EP,2026-10-28,,,False,CLOSED\n"
                     "HS,2026-10-27,2026-10-27T09:00:00-04:00,2026-10-27T21:00:00-04:00,True,\n")
        for cl in (frozenset(), ps.load_closures(ps.DEFAULT_CLOSURES)):  # committed file is header-only
            st = ps.load_statuses(str(f), ["MK", "EP", "HS", "AK"], D, cl)
            self.assertEqual(st, {"MK": "open", "EP": "unknown", "HS": "unknown", "AK": "unknown"})
            self.assertNotIn("closed", st.values())

    def test_park_status_reads_official_rows_only(self):
        import park_status as ps
        tmp = Path(tempfile.mkdtemp(prefix="tweetv2-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        f = tmp / "dimparkhours.csv"
        f.write_text("park,date,opening_time,closing_time,is_official,flag\n"
                     "MK,2026-10-28,2026-10-28T09:00:00-04:00,2026-10-28T22:00:00-04:00,True,\n"
                     "EP,2026-10-28,,,False,\n"
                     "HS,2026-10-28,2026-10-28T09:00:00-04:00,2026-10-28T21:00:00-04:00,True,CLOSED\n"
                     "AK,2026-10-28,2026-10-28T09:00:00-04:00,2026-10-28T17:00:00-04:00,False,CLOSED\n")
        # AC 1d: dimparkhours never marks closure, whatever its columns say; only closures.csv does
        self.assertEqual(ps.load_statuses(str(f), ["MK", "EP", "HS", "AK"], D),
                         {"MK": "open", "EP": "unknown", "HS": "open", "AK": "unknown"})
        self.assertEqual(ps.load_statuses(str(f), ["MK", "EP", "HS", "AK"], D, {("HS", D), ("EP", D)}),
                         {"MK": "open", "EP": "closed", "HS": "closed", "AK": "unknown"})
        self.assertEqual(ps.park_status(str(tmp / "missing.csv"), "MK", D), "unknown")
        from datetime import datetime, timezone
        at = datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)  # 11 PM ET Oct 27 = noon Oct 28 in Tokyo
        self.assertEqual(ps.park_local_date("MK", at), "2026-10-27")
        self.assertEqual(ps.park_local_date("TDL", at), "2026-10-28")

    def test_content_loader_refuses_bad_input(self):
        tmp = Path(tempfile.mkdtemp(prefix="tweetv2-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        p = tmp / "p.json"
        for bad in (content("2026-10-27", [1, 2, 3, 4]), content(D, [1, 2, 3, 4], status="held"),
                    content(D, [1, 2, 3, None])):
            p.write_text(bad)
            with self.assertRaises(SystemExit):
                self.c.load_content(str(p), None, D)
        p.write_text(content(D, [1, 2, 3, 4]))
        fc, ob = self.c.load_content(str(p), None, D)
        self.assertEqual(fc["AK"], 4.0)
        self.assertIsNone(ob["AK"])

    def test_alt_text_limit(self):
        with self.assertRaises(SystemExit):
            self.c.check_alt("x" * 1001)

    def test_cli_requires_one_source(self):
        tmp = Path(tempfile.mkdtemp(prefix="tweetv2-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        r = subprocess.run([sys.executable, str(ROOT / "operations/tweet_v2/cards.py"), D, str(tmp / "o"),
                            "--history", "h"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
