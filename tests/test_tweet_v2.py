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

GFIX = Path(__file__).resolve().parent / "fixtures" / "tweet_guard"
PARKS = [("MK", "Magic Kingdom"), ("EP", "EPCOT"), ("HS", "Hollywood Studios"), ("AK", "Animal Kingdom")]
D = "2026-10-28"


def content(d, vals, status="ready"):
    return json.dumps({"status": status, "target_date": d,
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
        self.cdir = self.tmp / "content"
        self.cdir.mkdir()
        (self.cdir / f"predicted_{D}.json").write_text(content(D, [20.1, 21.0, 26.0, 33.3]))
        (self.cdir / f"observed_{D}.json").write_text(content(D, [19.0, 22.5, 25.0, 27.0]))
        self.state = self.tmp / "tweet_state.json"
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": "1850000000000000001"}}}))

    def fake_build(self, d, out, history, pred, obs=None, park_hours=None):
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
        mk = ("flag", "CLOSED")
        self.assertEqual(ps.load_statuses(str(f), ["MK", "EP", "HS", "AK"], D, mk),
                         {"MK": "open", "EP": "unknown", "HS": "closed", "AK": "unknown"})
        self.assertEqual(ps.park_status(str(f), "HS", D), "open")  # no marker known yet (OQ-1): never closed
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
