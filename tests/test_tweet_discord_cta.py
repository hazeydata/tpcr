"""TPCR-DISCORD-GROWTH-001 (Fred, Nov 18 2026): the Discord CTA is a SEPARATE text-only reply threaded under the 8:30 AM
OBSERVED post. Off by default (env TPCR_DISCORD_CTA_REPLY=1 AND a valid config window with a real invite). The OBSERVED and
FORECAST texts are byte-identical to origin/main. Mocks only: no network, no X/Discord API, tmp dirs only."""
import contextlib
import functools
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operations" / "tweet_v2"))
sys.path.insert(0, str(ROOT / "operations" / "scripts"))
sys.modules.setdefault("requests_oauthlib", type(sys)("requests_oauthlib")).OAuth1 = lambda *a: "auth"
import requests  # noqa: E402

import tweet_copy as tc  # noqa: E402
import tweet_format as tf  # noqa: E402
import tweet_v2  # noqa: E402
import wti_observed_tweet as wot  # noqa: E402

URL = "https://discord.gg/AbCdEf12"
TEXT = f"Free daily forecasts live on our Discord: {URL}"
FLAG = "TPCR_DISCORD_CTA_REPLY"
ON = {FLAG: "1"}
FIRST, LAST = "2026-11-19", "2026-12-02"
IN_WINDOW = date(2026, 11, 20)
NOW = datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)  # 07:30 America/Toronto
SHIPPED = (3.0, 6.0)
TID, RT, CTA_ID = "2116886013188379069", "2116258879692571134", "2116886013188379999"
D = (date.today() - timedelta(days=1)).isoformat()


def cta(url=URL, first=FIRST, last=LAST):
    c, e = tf.parse_discord_cta_checked({"discord_cta": {"invite_url": url, "first_post_date": first, "last_post_date": last}},
                                        err=io.StringIO())
    assert e is None, e
    return c


def park(ob, fc):
    return {"ob": ob, "fc": fc, "gap": round(ob - fc, 1)}


def metas():
    """Synthetic scorecard metas: every miss tier, closed parks, worst-case length."""
    names = ["Magic Kingdom", "EPCOT", "Hollywood Studios", "Animal Kingdom"]
    for combo in itertools.product((-19.9, -4.0, 0.0, 4.0, 19.9, None), repeat=4):
        parks = {n: park(80.0 + v, 80.0) for n, v in zip(names, combo) if v is not None}
        if parks:
            yield {"date": "2026-09-30", "headline": "x", "parks": parks, "graded": list(parks)}
    worst = {n: park(100.0, 199.9) for n in names}
    yield {"date": "2026-09-30", "headline": "x", "parks": worst, "graded": names}


@functools.lru_cache(maxsize=None)
def origin_main_copy():
    """tweet_copy.py exactly as on origin/main (read-only git show, built in memory, no files), or skip."""
    r = subprocess.run(["git", "show", "origin/main:operations/tweet_v2/tweet_copy.py"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise unittest.SkipTest("origin/main not available")
    m = types.ModuleType("tweet_copy_origin")
    exec(compile(r.stdout, "origin/main:operations/tweet_v2/tweet_copy.py", "exec"), m.__dict__)
    return m


class Texts(unittest.TestCase):
    """OBSERVED/FORECAST text is untouched whether the feature is off or on."""

    def test_reply_and_forecast_signatures_are_origin_main(self):
        import inspect
        self.assertEqual(list(inspect.signature(tc.reply_text).parameters), ["m", "tiers"])
        self.assertEqual(list(inspect.signature(tweet_v2.post_text).parameters), ["kind", "arts"])

    def test_observed_identical_to_origin_main(self):
        old = origin_main_copy()
        n = 0
        for m in metas():
            for tiers in ((2.0, 5.0), SHIPPED):
                a, b = tc.reply_text(m, tiers), old.reply_text(m, tiers)
                self.assertEqual(a, b)
                if a is not None:
                    n += 1
                    self.assertNotIn("discord", a.lower())
                    self.assertEqual(tc.weighted_len(a), old.weighted_len(a))
        self.assertGreater(n, 1000)

    def test_forecast_identical_to_origin_main(self):
        old = origin_main_copy()
        S = {p: [10.0, 20.0] for p in ("MK", "EP", "HS", "AK")}
        try:
            import cards
        except ImportError:
            self.skipTest("cards deps (pandas) not installed")
        for combo in itertools.product((5.0, 15.0, 25.0), repeat=4):
            m = cards.meta("2026-10-28", dict(zip(S, combo)), S, None, "x")
            self.assertEqual(tc.forecast_text(m), old.forecast_text(m))
            self.assertNotIn("discord", tc.forecast_text(m).lower())

    def test_url_rule_is_noop_for_url_free_text(self):
        old = origin_main_copy()
        for t in ("WDW Crowds OBSERVED: Wed, Sep 30\n✅ ⏬ ⬆️ 中文 a–b", "plain", ""):
            self.assertEqual(tc.weighted_len(t), old.weighted_len(t))
        self.assertEqual(tc.weighted_len(TEXT), len("Free daily forecasts live on our Discord: ") + 23)


class Decision(unittest.TestCase):
    def test_text_exact_and_fits(self):
        text, reason = tf.discord_cta_reply(IN_WINDOW, ON, cta())
        self.assertEqual((text, reason), (TEXT, "on"))
        self.assertEqual(text.encode(), TEXT.encode())
        self.assertLessEqual(tc.weighted_len(text), 280)

    def test_flag_gate(self):
        for env in ({}, {FLAG: "0"}, {FLAG: ""}, {FLAG: "true"}, {FLAG: "yes"}, {FLAG: " 1"}):
            text, reason = tf.discord_cta_reply(IN_WINDOW, env, cta())
            self.assertIsNone(text)
            self.assertEqual(reason, "off: TPCR_DISCORD_CTA_REPLY not 1")

    def test_committed_config_is_off(self):
        self.assertEqual(json.loads(tf.CONFIG.read_text())["discord_cta"],
                         {"invite_url": None, "first_post_date": None, "last_post_date": None})
        self.assertIsNone(tf.DISCORD_CTA)
        text, reason = tf.discord_cta_reply(IN_WINDOW, ON)
        self.assertIsNone(text)
        self.assertTrue(reason.startswith("skipped:"), reason)

    def test_missing_invite(self):
        text, reason = tf.discord_cta_reply(IN_WINDOW, ON, cta(url=None))
        self.assertIsNone(text)
        self.assertTrue(reason.startswith("skipped: invite missing"), reason)

    def test_placeholders_distinct_reasons(self):
        bad = ["https://discord.gg/INVITE-X-DAILY", "https://discord.gg/invite-abc", "https://discord.gg/my-placeholder",
               "https://discord.gg/Example1", "https://discord.gg/abxxxcd", "https://discord.gg/x-daily",
               "https://discord.gg/X-Daily"]
        reasons = []
        for u in bad:
            text, reason = tf.discord_cta_reply(IN_WINDOW, ON, cta(url=u))
            self.assertIsNone(text, u)
            self.assertTrue(reason.startswith("skipped: "), reason)
            reasons.append(reason)
        self.assertIn("starts with INVITE", reasons[0])
        self.assertIn("'placeholder'", reasons[2])
        self.assertIn("'example'", reasons[3])
        self.assertIn("'xxx'", reasons[4])
        self.assertIn("x-daily", reasons[5])
        self.assertGreaterEqual(len({re_kind(r) for r in reasons}), 5)

    def test_general_invite_allowed(self):
        text, reason = tf.discord_cta_reply(IN_WINDOW, ON, cta(url="https://discord.gg/2Estr7sbP7"))
        self.assertEqual(reason, "on")
        self.assertEqual(text, "Free daily forecasts live on our Discord: https://discord.gg/2Estr7sbP7")

    def test_window_inclusive_toronto(self):
        c = cta()
        self.assertEqual(tf.discord_cta_reply(date(2026, 11, 19), ON, c)[1], "on")
        self.assertEqual(tf.discord_cta_reply(date(2026, 12, 2), ON, c)[1], "on")
        for d in (date(2026, 11, 18), date(2026, 12, 3)):
            text, reason = tf.discord_cta_reply(d, ON, c)
            self.assertIsNone(text)
            self.assertTrue(reason.startswith("skipped: outside window"), reason)
        # 2026-12-03 03:00Z is still Dec 2 in Toronto (22:00 EST); 05:30Z is Dec 3 00:30 EST
        self.assertEqual(tf.discord_cta_reply(datetime(2026, 12, 3, 3, 0, tzinfo=timezone.utc), ON, c)[1], "on")
        self.assertTrue(tf.discord_cta_reply(datetime(2026, 12, 3, 5, 30, tzinfo=timezone.utc), ON, c)[1].startswith("skipped: outside"))

    def test_config_parse(self):
        def parse(**kv):
            err = io.StringIO()
            return tf.parse_discord_cta({"discord_cta": kv}, err=err), err.getvalue()
        c, e = parse(invite_url=None, first_post_date=None, last_post_date=None)
        self.assertEqual((c, e), (None, ""))  # all null: off, silent
        self.assertEqual(tf.parse_discord_cta({}, err=io.StringIO()), None)
        for kv in ({"invite_url": URL, "first_post_date": FIRST, "last_post_date": None},       # partial
                   {"invite_url": URL, "first_post_date": None, "last_post_date": None},
                   {"invite_url": "http://discord.gg/AbCdEf12", "first_post_date": FIRST, "last_post_date": LAST},
                   {"invite_url": "https://discord.gg/a", "first_post_date": FIRST, "last_post_date": LAST},
                   {"invite_url": "https://discord.gg/ab/cd", "first_post_date": FIRST, "last_post_date": LAST},
                   {"invite_url": URL, "first_post_date": FIRST, "last_post_date": "2026-12-03"},  # 15 days
                   {"invite_url": URL, "first_post_date": LAST, "last_post_date": FIRST},
                   {"invite_url": URL, "first_post_date": "11/19/2026", "last_post_date": LAST},
                   {"invite_url": 7, "first_post_date": FIRST, "last_post_date": LAST}):
            c, e = parse(**kv)
            self.assertIsNone(c, kv)
            self.assertEqual(e.count("ERROR"), 1, (kv, e))
            self.assertEqual(len(e.strip().splitlines()), 1)
        self.assertEqual(tf.parse_discord_cta({"discord_cta": "x"}, err=io.StringIO()), None)
        self.assertEqual(tf.parse_discord_cta(None, err=io.StringIO()), None)

    def test_invalid_config_reason_and_never_raises(self):
        with mock.patch.object(tf, "DISCORD_CTA", None), mock.patch.object(tf, "DISCORD_CTA_ERROR", "bad window"):
            self.assertEqual(tf.discord_cta_reply(IN_WINDOW, ON), (None, "skipped: config invalid (bad window)"))
        for junk in ({"invite_url": URL}, "x", 5, {"invite_url": URL, "first": None, "last": None}):
            text, reason = tf.discord_cta_reply(IN_WINDOW, ON, junk)
            self.assertIsNone(text)
            self.assertTrue(reason.startswith("skipped:"), reason)
        self.assertEqual(tf.discord_cta_reply(None, ON, cta())[0], None)  # bad today
        self.assertEqual(tf.discord_cta_reply(IN_WINDOW, None)[0], None)  # bad environ

    def test_over_limit_skipped(self):
        with mock.patch.object(tc, "DISCORD_CTA", "x" * 281 + " {invite_url}"):
            text, reason = tf.discord_cta_reply(IN_WINDOW, ON, cta())
        self.assertIsNone(text)
        self.assertIn("over 280", reason)


def re_kind(reason):
    for k in ("INVITE", "'placeholder'", "'example'", "'xxx'", "x-daily", "shared untracked"):
        if k in reason:
            return k
    return reason


def meta_for(m=None):
    names = ["Magic Kingdom", "EPCOT", "Hollywood Studios", "Animal Kingdom"]
    parks = {n: park(v, 80.0) for n, v in zip(names, (82.0, 70.0, 95.0, 80.0))}
    return {"date": "2026-09-30", "headline": "x", "parks": parks, "graded": names}


class Main(unittest.TestCase):
    """Drives wti_observed_tweet.main end to end with requests/Discord/state mocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state = self.dir / "tweet_state.json"
        self.state.write_text(json.dumps({"predicted": {D: {"tweet_id": RT, "posted_at": "2026-10-30T20:00:05Z",
                                                            "format": "new"}}, "observed": {}}))

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        return json.loads(self.state.read_text())

    def run_main(self, responses, env=None, config="valid", upload="M1", argv=(), discord_fail_on=None, save_fail=False,
                 dry=False, build_r=True, config_error=None):
        """responses: queue for requests.post to the X API (a dict/Response, or an Exception to raise).
        Returns (rc, posts, disc, out, err, manifest)."""
        responses = list(responses)
        posts, disc = [], []

        def fake_post(url, **kw):
            posts.append(kw)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        def fake_disc(msg):
            disc.append(msg)
            if discord_fail_on and discord_fail_on in msg:
                raise requests.ConnectionError("discord down")

        real_save = wot.tweet_note._save

        def save(path, state):
            if save_fail and "discord_cta_reply" in state:
                raise OSError("disk full")
            return real_save(path, state)

        cfg = {"valid": cta(), "off": None}[config] if isinstance(config, str) else config
        pats = [mock.patch.dict(os.environ, {**{k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET",
                                                                 "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")},
                                             "TPCR_TWEET_DRY_RUN": "0", **(env or {})}),
                mock.patch.object(wot, "load_env"), mock.patch.object(wot, "enforce"),
                mock.patch.object(wot, "guard_status", return_value="test"),
                mock.patch.object(wot, "STATE_FILE", self.state),
                mock.patch.object(wot, "load_content", return_value=('{"generated_at":"x"}', {"parks": []})),
                mock.patch.object(wot, "post_discord", side_effect=fake_disc),
                mock.patch.object(wot, "upload_media", return_value=upload),
                mock.patch.object(wot, "get_twitter_auth", return_value=None),
                mock.patch.object(wot.requests, "post", side_effect=fake_post),
                mock.patch.object(wot.tweet_note, "_save", side_effect=save),
                mock.patch.object(tweet_v2, "now", return_value=NOW),
                mock.patch.object(tweet_v2, "check_closures", return_value=True),
                mock.patch.object(tweet_v2, "check_park_hours"),
                mock.patch.object(tweet_v2, "fetch_history", return_value=None),
                mock.patch.object(tweet_v2, "clear_no_data"),
                mock.patch.object(tweet_v2, "read_content", return_value="{}"),
                mock.patch.object(tweet_v2, "observed_staleness", return_value=None),
                mock.patch.object(tweet_v2, "build",
                                  return_value={"R": {"mp4": self.dir / "r.mp4", "alt": "a", "meta": meta_for()}} if build_r else {}),
                mock.patch.object(tweet_v2, "new_workdir", return_value=(self.dir, None)),
                mock.patch.object(tweet_v2, "cleanup"),
                mock.patch.object(tf, "reply_format", return_value=(tf.NEW, "test")),
                mock.patch.object(tf, "TIERS_ERROR", None), mock.patch.object(tf, "REPLY_TIERS", SHIPPED),
                mock.patch.object(tf, "DISCORD_CTA", cfg), mock.patch.object(tf, "DISCORD_CTA_ERROR", config_error)]
        if dry:
            # block_http_for_dry_run rewrites requests.* for good: patch first so the exit restores them
            pats += [mock.patch.object(requests, n, requests.post) for n in ("get", "post", "put", "delete", "patch", "request")]
            pats += [mock.patch.object(requests.Session, "request", requests.Session.request)]
            argv = ("--dry-run", *argv)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as st:
            for p in pats:
                st.enter_context(p)
            st.enter_context(contextlib.redirect_stdout(out))
            st.enter_context(contextlib.redirect_stderr(err))
            try:
                rc = wot.main(["--date", D, "--state-file", str(self.state), *argv])
                rc = 0 if rc is None else rc
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if dry:
                with self.assertRaises(tweet_v2.DryRunWrite):
                    requests.post("https://api.twitter.com/2/tweets", json={})
        manifest = None
        if (self.dir / "dry_run.json").exists():
            manifest = json.loads((self.dir / "dry_run.json").read_text())
        return rc, posts, disc, out.getvalue(), err.getvalue(), manifest

    def ok(self, i=TID):
        return resp(201, {"data": {"id": i, "text": "posted"}})

    # a. flag off ----------------------------------------------------------------------------------------------------
    def test_flag_off_no_cta_and_observed_text_is_origin_main(self):
        old = origin_main_copy()
        expected = old.reply_text(meta_for(), SHIPPED)
        for env in (None, {FLAG: "0"}):
            self.setUp()
            rc, posts, disc, out, err, _ = self.run_main([self.ok()], env=env)
            self.assertEqual(rc, 0)
            self.assertEqual(len(posts), 1, env)
            self.assertEqual(posts[0]["json"]["text"], expected)
            self.assertEqual(posts[0]["json"]["reply"], {"in_reply_to_tweet_id": RT})
            self.assertIn(f"discord_cta_reply ref={D}: off: TPCR_DISCORD_CTA_REPLY not 1", out)
            self.assertNotIn("discord_cta_reply", self.read())

    def test_flag_on_observed_text_still_origin_main(self):
        expected = origin_main_copy().reply_text(meta_for(), SHIPPED)
        rc, posts, *_ = self.run_main([self.ok(), self.ok(CTA_ID)], env=ON)
        self.assertEqual(rc, 0)
        self.assertEqual(posts[0]["json"]["text"], expected)
        self.assertEqual(posts[0]["json"]["media"], {"media_ids": ["M1"]})
        self.assertNotIn("discord", expected.lower())

    # b. flag on, real invite, in window -----------------------------------------------------------------------------
    def test_on_posts_one_reply_threaded_under_observed(self):
        rc, posts, disc, out, err, _ = self.run_main([self.ok(), self.ok(CTA_ID)], env=ON)
        self.assertEqual(rc, 0)
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[1]["json"], {"text": TEXT, "reply": {"in_reply_to_tweet_id": TID}})  # no media
        self.assertNotIn("media", posts[1]["json"])
        s = self.read()
        self.assertEqual(s["observed"][D]["tweet_id"], TID)
        e = s["discord_cta_reply"][D]
        self.assertEqual((e["tweet_id"], e["in_reply_to"]), (CTA_ID, TID))
        self.assertTrue(e["posted_at"])
        self.assertIn(f"status/{CTA_ID}", disc[-1])
        self.assertEqual(len(disc), 2)
        self.assertEqual(err, "")

    def test_state_prune_over_14_days(self):
        old = (date.today() - timedelta(days=30)).isoformat()
        s = self.read()
        s["discord_cta_reply"] = {old: {"tweet_id": "1"}}
        self.state.write_text(json.dumps(s))
        self.run_main([self.ok(), self.ok(CTA_ID)], env=ON)
        self.assertEqual(list(self.read()["discord_cta_reply"]), [D])

    # c. skipped -----------------------------------------------------------------------------------------------------
    def check_skipped(self, config, expect, env=ON, config_error=None):
        rc, posts, disc, out, err, _ = self.run_main([self.ok()], env=env, config=config, config_error=config_error)
        self.assertEqual(rc, 0)
        self.assertEqual(len(posts), 1)  # the OBSERVED post only
        self.assertIn(f"discord_cta_reply ref={D}: skipped: {expect}", out)
        self.assertNotIn("discord_cta_reply", self.read())
        self.assertEqual(len(disc), 1)

    def test_missing_invite(self):
        self.check_skipped(cta(url=None), "invite missing")

    def test_placeholders(self):
        for u, why in (("https://discord.gg/INVITE-X-DAILY", "placeholder invite"), ("https://discord.gg/placeholder1", "placeholder invite"),
                       ("https://discord.gg/example", "placeholder invite"), ("https://discord.gg/xxxxxx", "placeholder invite"),
                       ("https://discord.gg/x-daily", "placeholder invite")):
            self.setUp()
            self.check_skipped(cta(url=u), why)

    def test_outside_window(self):
        self.check_skipped(cta(first="2026-11-21", last="2026-12-04"), "outside window")
        self.setUp()
        self.check_skipped(cta(first="2026-11-01", last="2026-11-14"), "outside window")

    def test_partial_config_off_with_error(self):
        err = io.StringIO()
        c, e = tf.parse_discord_cta_checked({"discord_cta": {"invite_url": URL, "first_post_date": FIRST, "last_post_date": None}}, err=err)
        self.assertEqual(err.getvalue().count("ERROR"), 1)
        self.check_skipped(None, "config invalid", config_error=e)

    # d. failures ----------------------------------------------------------------------------------------------------
    def test_observed_failure_posts_no_reply(self):
        rc, posts, disc, out, err, _ = self.run_main([FAIL()], env=ON)  # API error
        self.assertEqual(rc, 1)
        self.assertEqual(len(posts), 1)
        self.assertNotIn("discord_cta_reply", out + err)
        rc, posts, disc, out, err, _ = self.run_main([NO_ID()], env=ON)  # accepted but no id
        self.assertEqual((rc, len(posts)), (0, 1))
        self.assertNotIn("discord_cta_reply", out + err)
        self.assertEqual(self.read()["observed"][D]["id_missing"], True)
        self.setUp()
        rc, posts, disc, out, err, _ = self.run_main([], env=ON, upload=None)  # upload failure
        self.assertEqual((rc, posts), (1, []))
        self.assertNotIn("discord_cta_reply", out + err)
        self.assertNotIn("discord_cta_reply", self.read())

    def test_no_reply_on_silent_and_already_posted_paths(self):
        rc, posts, *_rest = self.run_main([], env=ON, build_r=False)  # no card: silent
        self.assertEqual(posts, [])
        self.assertNotIn("discord_cta_reply", "".join(_rest[1:3]))
        s = self.read()
        s["observed"][D] = {"tweet_id": TID, "in_reply_to": RT, "posted_at": "2026-11-19T13:30:00Z"}
        self.state.write_text(json.dumps(s))
        rc, posts, disc, out, err, _ = self.run_main([], env=ON)  # already posted
        self.assertEqual(posts, [])
        self.assertIn("Already posted", out)
        self.assertNotIn("discord_cta_reply", out + err)

    def test_reply_failures_are_isolated(self):
        base_rc, base_posts, *_ = self.run_main([self.ok()])  # feature off
        cases = {
            "api error": dict(responses=[self.ok(), FAIL()]),
            "no id": dict(responses=[self.ok(), NO_ID()]),
            "raised": dict(responses=[self.ok(), requests.ConnectionError("boom")]),
            "state write": dict(responses=[self.ok(), self.ok(CTA_ID)], save_fail=True),
            "discord alert fails": dict(responses=[self.ok(), self.ok(CTA_ID)], discord_fail_on="Discord CTA reply posted"),
            "both": dict(responses=[self.ok(), FAIL()], discord_fail_on="Discord CTA reply"),
        }
        for name, kw in cases.items():
            self.setUp()
            rc, posts, disc, out, err, _ = self.run_main(env=ON, **kw)
            self.assertEqual(rc, base_rc, name)
            self.assertEqual(len(posts), 2, name)  # OBSERVED once, CTA once: nothing retried
            self.assertEqual(posts[0]["json"]["reply"], {"in_reply_to_tweet_id": RT}, name)
            self.assertEqual(self.read()["observed"][D]["tweet_id"], TID, name)
            self.assertIn(f"ERROR discord_cta_reply ref={D}: ", err, name)
            self.assertNotIn("Traceback", err)
            self.assertNotIn("x" * 10, err.replace("xxx", ""))  # no env secrets echoed
            if name != "both":
                self.assertTrue(any(":warning:" in m and "Discord CTA reply" in m for m in disc) or name == "discord alert fails", name)
        self.assertEqual(base_rc, 0)

    def test_failed_reply_not_in_state(self):
        self.run_main([self.ok(), FAIL()], env=ON)
        self.assertNotIn("discord_cta_reply", self.read())

    def test_post_discord_cta_reply_never_raises_or_exits(self):
        for exc in (RuntimeError("x"), KeyError("k"), requests.Timeout("t"), OSError("o")):
            with mock.patch.object(wot, "cta_today", side_effect=exc), mock.patch.object(wot, "post_discord", side_effect=exc), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(wot.post_discord_cta_reply(TID, D))
            self.assertIn("ERROR discord_cta_reply", err.getvalue())

    def test_old_format_path_never_replies(self):
        with mock.patch.object(wot.tweet_v1_legacy, "build_reply",
                               return_value={"text": "t", "alt": "a", "mp4": self.dir / "o.mp4"}):
            with mock.patch.object(wot.tweet_format, "reply_format", return_value=(tf.OLD, "old")):
                self.run_main_old()

    def run_main_old(self):
        called = []
        with mock.patch.object(wot, "post_discord_cta_reply", side_effect=lambda *a: called.append(a)), \
                mock.patch.object(wot, "post_tweet", return_value=TID), mock.patch.object(wot, "upload_media", return_value="M"), \
                mock.patch.object(wot, "post_discord"), mock.patch.object(tf, "record"), \
                mock.patch.object(wot, "STATE_FILE", self.state), mock.patch.object(wot, "load_env"), \
                mock.patch.object(wot, "enforce"), mock.patch.object(wot, "load_content", return_value=("{}", {"parks": []})), \
                mock.patch.object(tweet_v2, "check_closures", return_value=True), mock.patch.object(tweet_v2, "check_park_hours"), \
                mock.patch.object(tweet_v2, "new_workdir", return_value=(self.dir, None)), mock.patch.object(tweet_v2, "cleanup"), \
                mock.patch.dict(os.environ, {"TWITTER_CONSUMER_KEY": "x", "TWITTER_CONSUMER_SECRET": "x",
                                             "TWITTER_ACCESS_TOKEN": "x", "TWITTER_ACCESS_TOKEN_SECRET": "x",
                                             "TPCR_TWEET_DRY_RUN": "0", FLAG: "1"}), \
                contextlib.redirect_stdout(io.StringIO()):
            wot.main(["--date", D, "--state-file", str(self.state)])
        self.assertEqual(called, [])

    # e. dry run -----------------------------------------------------------------------------------------------------
    def test_dry_run_on(self):
        rc, posts, disc, out, err, man = self.run_main([], env=ON, dry=True)
        self.assertEqual((rc, posts, disc), (0, [], []))
        c = man["discord_cta_reply"]
        self.assertEqual((c["would_post"], c["reason"], c["text"]), (True, "on", TEXT))
        self.assertEqual(c["observed_reply_to_tweet_id"], RT)
        self.assertEqual(c["in_reply_to"], wot.CTA_IN_REPLY_TO)
        self.assertIn("DRY RUN discord_cta_reply: would_post=True reason=on", out)
        self.assertIn(TEXT, out)
        self.assertIn(f"in_reply_to={wot.CTA_IN_REPLY_TO}", out)
        self.assertIn(f"thread: forecast {RT} -> OBSERVED -> Discord reply", out)
        self.assertTrue(man["would_post"])
        self.assertEqual(man["tweet_text"], origin_main_copy().reply_text(meta_for(), SHIPPED))
        self.assertNotIn("discord_cta_reply", self.read())  # no state written

    def test_dry_run_off_and_skipped(self):
        for env, config, reason in ((None, "valid", "off: TPCR_DISCORD_CTA_REPLY not 1"),
                                    (ON, cta(url="https://discord.gg/INVITE-X"), "skipped: placeholder")):
            self.setUp()
            rc, posts, _d, out, _e, man = self.run_main([], env=env, config=config, dry=True)
            self.assertEqual((rc, posts), (0, []))
            c = man["discord_cta_reply"]
            self.assertEqual((c["would_post"], c["text"]), (False, None))
            self.assertTrue(c["reason"].startswith(reason), c)
            self.assertIn("DRY RUN discord_cta_reply: would_post=False", out)

    def test_dry_run_observed_would_not_post(self):
        rc, posts, _d, out, _e, man = self.run_main([], env=ON, dry=True, build_r=False)
        self.assertEqual(rc, 1)
        self.assertEqual(posts, [])
        self.assertFalse(man["discord_cta_reply"]["would_post"])
        self.assertIn("OBSERVED would not post", man["discord_cta_reply"]["reason"])


def resp(status, body=None, raw=None):
    r = requests.models.Response()
    r.status_code = status
    r._content = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    return r


NO_ID = lambda: resp(201, raw=b'{"data":{"text":"posted"}}')  # noqa: E731
FAIL = lambda: resp(503, {"title": "Service Unavailable"})  # noqa: E731


class Demo(unittest.TestCase):
    def run_demo(self, *args):
        r = subprocess.run([sys.executable, str(ROOT / "operations" / "scripts" / "discord_cta_reply_demo.py"), *args],
                           capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_threads_under_observed_id(self):
        o = self.run_demo("--observed-id", "12345", "--invite-url", URL, "--today", "2026-11-20", "--flag", "1")
        self.assertEqual((o["reason"], o["text"], o["in_reply_to"]), ("on", TEXT, "12345"))
        self.assertEqual(o["calls"], [{"text": TEXT, "reply": {"in_reply_to_tweet_id": "12345"}}])

    def test_off_and_placeholder_make_no_calls(self):
        o = self.run_demo("--observed-id", "1", "--invite-url", URL, "--flag", "0")
        self.assertEqual((o["calls"], o["in_reply_to"]), ([], None))
        o = self.run_demo("--observed-id", "1", "--invite-url", "https://discord.gg/INVITE-X-DAILY", "--flag", "1")
        self.assertEqual(o["calls"], [])
        self.assertIn("placeholder", o["reason"])


if __name__ == "__main__":
    unittest.main()
