"""rev7i follow-ups (Kitty t4 on tpcr#543): main()-level tests for both tweet jobs and tweet_state_record.py,
aimed at the surviving mutants m04 m05 m07 m11 m13 m17 m18 m19 m20 m25 m27, plus R14 (backfill older than the prune window)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operations" / "scripts"))
sys.path.insert(0, str(ROOT / "operations" / "tweet_v2"))
sys.modules.setdefault("requests_oauthlib", type(sys)("requests_oauthlib")).OAuth1 = lambda *a: "auth"
import requests  # noqa: E402

import tweet_state_record  # noqa: E402
import tweet_v2  # noqa: E402
import wti_observed_tweet as wot  # noqa: E402
import wti_predicted_tweet as wpt  # noqa: E402

TID, RT = "2116886013188379069", "2116258879692571134"
D = (date.today() - timedelta(days=1)).isoformat()  # inside the 14-day prune window whenever this runs


def resp(status, body=None, raw=None):
    r = requests.models.Response()
    r.status_code = status
    r._content = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    return r


OK = lambda: resp(201, {"data": {"id": TID, "text": "posted"}})  # noqa: E731
NO_ID = lambda: resp(201, raw=b'{"data":{"text":"posted"}}')  # noqa: E731
FAIL = lambda: resp(503, {"title": "Service Unavailable"})  # noqa: E731


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state = self.dir / "tweet_state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, obj):
        self.state.write_text(json.dumps(obj))

    def read(self):
        return json.loads(self.state.read_text())

    def run_main(self, mod, post_resp):
        """Live path of mod.main() with build/content/upload mocked; X and Discord calls are captured, never sent."""
        posts, disc = [], []
        env = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET", "TWITTER_ACCESS_TOKEN",
                                "TWITTER_ACCESS_TOKEN_SECRET")}
        env["TPCR_TWEET_DRY_RUN"] = "0"
        pats = [mock.patch.dict(os.environ, env), mock.patch.object(mod, "load_env"), mock.patch.object(mod, "enforce"),
                mock.patch.object(mod, "STATE_FILE", self.state),
                mock.patch.object(mod, "load_content", return_value=('{"generated_at":"x"}', {"parks": []})),
                mock.patch.object(mod, "post_discord", side_effect=disc.append),
                mock.patch.object(mod, "upload_media", return_value="M1"),
                mock.patch.object(mod, "get_twitter_auth", return_value=None),
                mock.patch.object(mod.requests, "post", side_effect=lambda url, **kw: posts.append(kw) or post_resp),
                mock.patch.object(tweet_v2, "check_closures", return_value=True),
                mock.patch.object(tweet_v2, "check_park_hours"),
                mock.patch.object(tweet_v2, "fetch_history", return_value=None),
                mock.patch.object(tweet_v2, "clear_no_data"),
                mock.patch.object(tweet_v2, "post_text", return_value="WDW synthetic text"),
                mock.patch.object(tweet_v2, "build", return_value={"R": {"mp4": "r.mp4", "alt": "a"},
                                                                   "F": {"mp4": "f.mp4", "alt": "a"}}),
                mock.patch.object(tweet_v2, "new_workdir", return_value=(self.dir, None)),
                mock.patch.object(tweet_v2, "cleanup")]
        if mod is wot:
            pats += [mock.patch.object(tweet_v2, "read_content", return_value="{}"),
                     mock.patch.object(tweet_v2, "observed_staleness", return_value=None),
                     mock.patch.object(wot.tweet_format, "reply_format", return_value=(wot.tweet_format.NEW, "test")),
                     mock.patch.object(wot.tweet_format, "TIERS_ERROR", None)]
        else:
            pats += [mock.patch.object(tweet_v2, "forecast_staleness", return_value=None),
                     mock.patch.object(wpt.tweet_format, "FIRST_NEW_RUN", tweet_v2.now().replace(year=2000)),
                     mock.patch.object(wpt.tweet_format, "original_run_blocked", return_value=None),
                     mock.patch.object(wpt.tweet_note, "note_lines", return_value=[])]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as st:
            for p in pats:
                st.enter_context(p)
            st.enter_context(contextlib.redirect_stdout(out))
            st.enter_context(contextlib.redirect_stderr(err))
            try:
                rc = mod.main(["--date", D, "--state-file", str(self.state)])
                rc = 0 if rc is None else rc
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return rc, posts, disc, out.getvalue(), err.getvalue()


class ObservedMain(Harness):
    def setUp(self):
        super().setUp()
        self.write({"predicted": {D: {"tweet_id": RT, "posted_at": "2026-10-30T20:00:05Z", "format": "new"}},
                    "observed": {}})

    def test_success_saves_and_links(self):
        rc, posts, disc, _, _ = self.run_main(wot, OK())
        self.assertEqual(rc, 0)
        self.assertEqual(posts[0]["json"]["reply"], {"in_reply_to_tweet_id": RT})
        self.assertEqual(self.read()["observed"][D]["tweet_id"], TID)
        self.assertIn(f"https://x.com/HazeyDataFred/status/{TID}", disc[-1])

    def test_posted_without_id_warns_saves_exit0(self):  # m11
        rc, posts, disc, _, err = self.run_main(wot, NO_ID())
        self.assertEqual((rc, len(posts)), (0, 1))
        e = self.read()["observed"][D]
        self.assertEqual((e["tweet_id"], e["in_reply_to"], e["id_missing"]), (None, RT, True))
        self.assertEqual(len(disc), 1)
        self.assertTrue(disc[0].startswith(":warning:") and "id missing" in disc[0], disc)
        self.assertFalse(any(":x:" in m for m in disc))
        self.assertIn("id missing", err)
        self.assertIn('"text":"posted"', err)  # raw response body is logged

    def test_rerun_after_id_missing_does_not_post(self):  # m13
        self.run_main(wot, NO_ID())
        rc, posts, _, out, _ = self.run_main(wot, OK())
        self.assertEqual((rc, posts), (0, []))
        self.assertIn("Already posted observed tweet", out)

    def test_real_failure_alarms_exit1_no_state(self):
        rc, _, disc, _, _ = self.run_main(wot, FAIL())
        self.assertEqual(rc, 1)
        self.assertNotIn(D, self.read()["observed"])
        self.assertTrue(any(":x:" in m for m in disc))


class PredictedMain(Harness):
    def setUp(self):
        super().setUp()
        self.write({"observed": {}})

    def test_success_saves_tags_and_links(self):
        rc, _, disc, _, _ = self.run_main(wpt, OK())
        self.assertEqual(rc, 0)
        self.assertEqual(self.read()["predicted"][D]["tweet_id"], TID)
        self.assertIn(TID, json.loads((self.dir / "tweet_formats.json").read_text()))
        self.assertIn(f"https://x.com/HazeyDataFred/status/{TID}", disc[-1])

    def test_posted_without_id_warns_saves_exit0(self):  # m17 m18 m20
        rc, posts, disc, _, err = self.run_main(wpt, NO_ID())
        self.assertEqual((rc, len(posts)), (0, 1))
        self.assertIn('"text":"posted"', err)  # raw response body is logged
        e = self.read()["predicted"][D]
        self.assertEqual((e["tweet_id"], e["id_missing"]), (None, True))
        self.assertEqual(len(disc), 1)
        self.assertTrue(disc[0].startswith(":warning:") and "id missing" in disc[0], disc)
        self.assertFalse(any(":x:" in m for m in disc))
        fs = self.dir / "tweet_formats.json"
        self.assertFalse(fs.exists() and {"None", "null"} & set(json.loads(fs.read_text())))

    def test_rerun_after_id_missing_does_not_post(self):  # m19
        self.run_main(wpt, NO_ID())
        rc, posts, _, out, _ = self.run_main(wpt, OK())
        self.assertEqual((rc, posts), (0, []))
        self.assertIn("Already posted predicted tweet", out)

    def test_real_failure_alarms_exit1_no_state(self):
        rc, _, disc, _, _ = self.run_main(wpt, FAIL())
        self.assertEqual(rc, 1)
        self.assertNotIn(D, self.read().get("predicted", {}))
        self.assertTrue(any(":x:" in m for m in disc))


class Parser(unittest.TestCase):
    def test_data_id_preferred_over_top_level(self):  # m04
        self.assertEqual(tweet_v2.parse_tweet_id({"data": {"id": TID}, "id": "999"}), TID)

    def test_attr_object_data(self):  # m05
        class Inner:
            id = TID

        class Resp:
            data = Inner()
        self.assertEqual(tweet_v2.parse_tweet_id(Resp()), TID)

    def test_3xx_is_not_posted(self):  # m07
        for s in (300, 302, 399):
            self.assertEqual(tweet_v2.tweet_id_from_response(resp(s, {"data": {"id": TID}})), (False, None))


class RecordMain(Harness):
    def record(self, *argv):
        saved = {n: getattr(requests, n) for n in ("get", "post", "put", "delete", "patch", "request")}
        saved_sr = requests.Session.request
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch.object(wot, "STATE_FILE", wot.STATE_FILE), mock.patch.object(wpt, "STATE_FILE", wpt.STATE_FILE):
                rc = tweet_state_record.main([*argv, "--state-file", str(self.state)])
        finally:
            for n, f in saved.items():
                setattr(requests, n, f)
            requests.Session.request = saved_sr
        return rc, out.getvalue(), err.getvalue()

    def test_non_digit_id_exit2_untouched(self):  # m25
        self.write({"observed": {}})
        before = self.state.read_bytes()
        rc, _, err = self.record("--date", D, "--tweet-id", "12a4")
        self.assertEqual(rc, 2)
        self.assertIn("digits", err)
        self.assertEqual(self.state.read_bytes(), before)

    def test_keeps_existing_in_reply_to(self):  # m27
        self.write({"predicted": {D: {"tweet_id": "555"}},
                    "observed": {D: {"tweet_id": None, "in_reply_to": RT, "id_missing": True, "posted_at": "x"}}})
        rc, _, _ = self.record("--date", D, "--tweet-id", TID)
        self.assertEqual(rc, 0)
        self.assertEqual(self.read()["observed"][D]["in_reply_to"], RT)

    def test_old_date_is_not_pruned(self):  # R14
        old = (date.today() - timedelta(days=30)).isoformat()
        self.write({"predicted": {old: {"tweet_id": RT}}, "observed": {}})
        rc, out, err = self.record("--date", old, "--tweet-id", TID)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read()["observed"][old]["tweet_id"], TID)
        self.assertIn("recorded:", out)
        rc2, out2, _ = self.record("--date", old, "--tweet-id", TID)
        self.assertEqual(rc2, 0)
        self.assertIn("unchanged", out2)

    def test_old_date_predicted_kind(self):  # R14, predicted save_state
        old = (date.today() - timedelta(days=30)).isoformat()
        self.write({})
        rc, _, err = self.record("--kind", "predicted", "--date", old, "--tweet-id", TID)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read()["predicted"][old]["tweet_id"], TID)

    def test_job_save_state_still_prunes_other_old_days(self):
        old = (date.today() - timedelta(days=30)).isoformat()
        self.write({"observed": {old: {"tweet_id": "1"}}})
        rc, _, _ = self.record("--date", D, "--tweet-id", TID, "--reply-to", RT)
        self.assertEqual(rc, 0)
        self.assertEqual(list(self.read()["observed"]), [D])

    def test_predicted_job_save_state_still_prunes_other_old_days(self):
        old = (date.today() - timedelta(days=30)).isoformat()
        self.write({"predicted": {old: {"tweet_id": "1"}}})
        rc, _, _ = self.record("--kind", "predicted", "--date", D, "--tweet-id", TID)
        self.assertEqual(rc, 0)
        self.assertEqual(list(self.read()["predicted"]), [D])

    def test_prune_window_edge_both_jobs(self):  # 13 days old kept, 15 days old pruned
        d13 = (date.today() - timedelta(days=13)).isoformat()
        d15 = (date.today() - timedelta(days=15)).isoformat()
        for kind, extra in (("observed", ["--reply-to", RT]), ("predicted", [])):
            with self.subTest(kind=kind):
                self.write({kind: {d13: {"tweet_id": "13"}, d15: {"tweet_id": "15"}}})
                rc, _, err = self.record("--kind", kind, "--date", D, "--tweet-id", TID, *extra)
                self.assertEqual(rc, 0, err)
                self.assertEqual(sorted(self.read()[kind]), sorted([d13, D]))

    def test_readback_missing_exits1(self):
        for kind in ("observed", "predicted"):
            job = wot if kind == "observed" else wpt
            for name, fake in (("writes nothing", lambda *a, **k: None),
                               ("different id", lambda *a, **k: self.write({kind: {D: {"tweet_id": "999"}}}))):
                with self.subTest(kind=kind, case=name):
                    self.write({"predicted": {D: {"tweet_id": RT}}} if kind == "observed" else {})
                    with mock.patch.object(job, "save_state", side_effect=fake), \
                            mock.patch.object(wot.tweet_format, "record"):
                        rc, out, err = self.record("--kind", kind, "--date", D, "--tweet-id", TID)
                    self.assertEqual(rc, 1, out)
                    self.assertIn("not found after save", err)
                    self.assertNotIn("recorded:", out)


if __name__ == "__main__":
    unittest.main()
