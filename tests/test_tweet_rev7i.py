"""rev7i (TPCR-TWEET-LOCAL): tweet id parsing from the real X API v2 shape, posted-without-id handling,
HazeyDataFred links, and the one-shot tweet_state_record.py."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operations" / "scripts"))
sys.path.insert(0, str(ROOT / "operations" / "tweet_v2"))

sys.modules.setdefault("requests_oauthlib", type(sys)("requests_oauthlib")).OAuth1 = lambda *a: "auth"
import requests  # noqa: E402

import tweet_v2  # noqa: E402
import wti_observed_tweet as wot  # noqa: E402
import wti_predicted_tweet as wpt  # noqa: E402

TID = "2116886013188379069"
REPLY_TO = "2116258879692571134"
V2_BODY = {"data": {"id": TID, "text": "Walt Disney World crowds, Oct 31 ...", "edit_history_tweet_ids": [TID]}}


def real_response(status, body):
    """A real requests.Response, as requests.post returns it."""
    r = requests.models.Response()
    r.status_code = status
    r._content = json.dumps(body).encode() if body is not None else b""
    r.headers["content-type"] = "application/json"
    return r


class ParseId(unittest.TestCase):
    def test_v2_shape(self):
        self.assertEqual(tweet_v2.parse_tweet_id(V2_BODY), TID)
        self.assertEqual(tweet_v2.tweet_id_from_response(real_response(201, V2_BODY)), (True, TID))

    def test_tweepy_like_response_data(self):
        class Resp:  # tweepy.Response(data=..., includes, errors, meta): data is the v2 "data" dict
            data = V2_BODY["data"]
        self.assertEqual(tweet_v2.parse_tweet_id(Resp()), TID)

    def test_numeric_and_v1(self):
        self.assertEqual(tweet_v2.parse_tweet_id({"data": {"id": int(TID)}}), TID)
        self.assertEqual(tweet_v2.parse_tweet_id({"id_str": TID}), TID)

    def test_missing_or_blank(self):
        for body in ({}, {"data": {}}, {"data": {"id": ""}}, {"data": {"id": "  "}}, None, "x", []):
            self.assertIsNone(tweet_v2.parse_tweet_id(body), body)

    def test_non_2xx(self):
        self.assertEqual(tweet_v2.tweet_id_from_response(real_response(403, {"detail": "no"})), (False, None))

    def test_2xx_unparseable_body(self):
        r = real_response(201, None)
        self.assertEqual(tweet_v2.tweet_id_from_response(r), (True, None))


class PostTweet(unittest.TestCase):
    def run_post(self, mod, resp, **kw):
        with mock.patch.object(mod, "get_twitter_auth", return_value=None), \
                mock.patch.object(mod.requests, "post", return_value=resp) as post:
            out = mod.post_tweet("hello", **kw)
        return out, post

    def test_observed_posts_and_parses(self):
        out, post = self.run_post(wot, real_response(201, V2_BODY), reply_to=REPLY_TO, media_id="M1")
        self.assertEqual(out, TID)
        self.assertEqual(post.call_args.kwargs["json"]["reply"], {"in_reply_to_tweet_id": REPLY_TO})

    def test_predicted_posts_and_parses(self):
        self.assertEqual(self.run_post(wpt, real_response(201, V2_BODY), media_id="M1")[0], TID)

    def test_failure_is_none(self):
        for mod in (wot, wpt):
            self.assertIsNone(self.run_post(mod, real_response(429, {"title": "Too Many Requests"}))[0])

    def test_2xx_without_id_is_id_missing(self):
        for mod in (wot, wpt):
            out = self.run_post(mod, real_response(201, {"data": {"text": "x"}}))[0]
            self.assertIs(out, tweet_v2.ID_MISSING)
            self.assertIsNotNone(out)


class StateAndIdempotency(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.state = Path(self.d.name) / "tweet_state.json"
        self.state.write_text(json.dumps({"predicted": {"2026-10-31": {"tweet_id": REPLY_TO, "format": "new"}}}))

    def tearDown(self):
        self.d.cleanup()

    def test_posted_without_id_saves_state_and_warns(self):
        with mock.patch.object(wot, "STATE_FILE", self.state), \
                mock.patch.object(wot, "post_discord") as disc:
            self.assertIsNone(wot.posted_without_id("2026-10-31", REPLY_TO))
        e = json.loads(self.state.read_text())["observed"]["2026-10-31"]
        self.assertEqual((e["tweet_id"], e["in_reply_to"], e["id_missing"]), (None, REPLY_TO, True))
        self.assertTrue(tweet_v2.already_posted(e))
        self.assertIn("id missing", disc.call_args[0][0])
        self.assertNotIn("tweet failed", disc.call_args[0][0])

    def test_already_posted(self):
        self.assertFalse(tweet_v2.already_posted(None))
        self.assertFalse(tweet_v2.already_posted({}))
        self.assertTrue(tweet_v2.already_posted({"tweet_id": TID}))

    def record(self, *extra, tweet_id=TID):
        """Run tweet_state_record.main in-process; restore requests afterwards (the script blocks HTTP)."""
        import tweet_state_record
        saved = {n: getattr(requests, n) for n in ("get", "post", "put", "delete", "patch", "request")}
        saved_sr = requests.Session.request
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch.object(wot, "STATE_FILE", wot.STATE_FILE):
                rc = tweet_state_record.main(["--kind", "observed", "--date", "2026-10-31", "--tweet-id", tweet_id,
                                              "--state-file", str(self.state), *extra])
                self.blocked = False
                try:
                    requests.post("https://api.twitter.com/2/tweets", json={})
                except tweet_v2.DryRunWrite:
                    self.blocked = True
        finally:
            for n, f in saved.items():
                setattr(requests, n, f)
            requests.Session.request = saved_sr
        return type("R", (), {"returncode": rc, "stdout": out.getvalue(), "stderr": err.getvalue()})

    def test_record_script_writes_job_format_and_is_idempotent(self):
        with mock.patch.object(wot, "STATE_FILE", Path(self.d.name) / "job.json"), \
                mock.patch.object(wot.time, "strftime", return_value="2026-11-01T13:32:10Z"):
            (Path(self.d.name) / "job.json").write_text(self.state.read_text())
            wot.save_state(TID, "2026-10-31", REPLY_TO)
        expected = json.loads((Path(self.d.name) / "job.json").read_text())
        r = self.record()
        self.assertEqual(r.returncode, 0, r.stderr)
        first = self.state.read_bytes()
        self.assertEqual(json.loads(first), expected)  # same save_state code and format as the job
        e = json.loads(first)["observed"]["2026-10-31"]
        self.assertEqual(e, {"tweet_id": TID, "in_reply_to": REPLY_TO, "posted_at": "2026-11-01T13:32:10Z"})
        fmt_store = Path(self.d.name) / "tweet_formats.json"
        store1 = fmt_store.read_bytes()
        r2 = self.record()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("unchanged", r2.stdout)
        self.assertEqual(self.state.read_bytes(), first)
        self.assertEqual(fmt_store.read_bytes(), store1)

    def test_record_script_fills_id_missing_entry(self):
        with mock.patch.object(wot, "STATE_FILE", self.state), mock.patch.object(wot, "post_discord"):
            wot.posted_without_id("2026-10-31", REPLY_TO)
        self.assertEqual(self.record().returncode, 0)
        e = json.loads(self.state.read_text())["observed"]["2026-10-31"]
        self.assertEqual(e["tweet_id"], TID)
        self.assertNotIn("id_missing", e)

    def test_record_script_refuses_different_id(self):
        self.assertEqual(self.record().returncode, 0)
        before = self.state.read_bytes()
        self.assertEqual(self.record(tweet_id="1").returncode, 1)
        self.assertEqual(self.state.read_bytes(), before)

    def test_record_script_never_posts(self):
        self.assertEqual(self.record().returncode, 0)
        self.assertTrue(self.blocked)  # every HTTP call raises while the script runs

    def test_reply_to_comes_from_state(self):
        self.record()
        self.assertEqual(json.loads(self.state.read_text())["observed"]["2026-10-31"]["in_reply_to"], REPLY_TO)


class Links(unittest.TestCase):
    def test_status_url(self):
        self.assertEqual(tweet_v2.status_url(TID), f"https://x.com/HazeyDataFred/status/{TID}")

    def test_no_disneystatswhiz_in_tweet_path(self):
        for f in ("wti_observed_tweet.py", "wti_predicted_tweet.py", "tweet_v2.py", "tweet_state_record.py"):
            self.assertNotIn("DisneyStatsWhiz", (ROOT / "operations/scripts" / f).read_text(), f)


if __name__ == "__main__":
    unittest.main()
