"""Mocked-HTTP tests for operations/scripts/x_approved_reply_poster.py (stdlib unittest, no network)."""
import hashlib, io, json, os, subprocess, sys, tempfile, unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "operations" / "scripts"))
import x_approved_reply_poster as P  # noqa: E402

FAKE_SECRET = "FAKE-SECRET-VALUE-zzz"


def entry(eid="e1", text="Thanks for the mention!", rid="1000000000000000001", **kw):
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    e = {"entry_id": eid, "reply_to_id": rid, "text": text, "text_sha256": h,
         "approval": {"quote": "yes post that", "approved_at": "2026-10-26T09:30:00-04:00",
                      "approved_by": "Fred", "text_sha256": h}, "status": "approved"}
    e.update(kw)
    return e


MENTIONED = {"data": {"id": "1", "author_id": "9", "entities": {"mentions": [{"username": "HazeyDataFred"}]}}}
NOT_MENTIONED = {"data": {"id": "2", "author_id": "9", "entities": {"mentions": [{"username": "synthetic_other_user"}]},
                          "referenced_tweets": [{"type": "replied_to", "id": "50"}]},
                 "includes": {"tweets": [{"id": "50", "author_id": "77"}], "users": [{"id": "77", "username": "synthetic_other_user"}]}}
QUOTES_FRED = {"data": {"id": "3", "author_id": "9", "referenced_tweets": [{"type": "quoted", "id": "60"}]},
               "includes": {"tweets": [{"id": "60", "author_id": "42"}], "users": [{"id": "42", "username": "HazeyDataFred"}]}}


class Mock:
    def __init__(self, tweet=MENTIONED, replies=(), fail=None):
        self.tweet, self.replies, self.fail = tweet, list(replies), fail or {}
        self.calls = []
        self.n = 100

    WHERE = {"post_reply": "POST tweet"}

    def _call(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise P.ApiError(self.fail[name], self.WHERE.get(name, name))

    def get_tweet(self, tid):
        self._call("get_tweet"); return self.tweet

    def recent_replies(self, tid):
        self._call("recent_replies"); return self.replies

    def upload_media(self, path):
        self._call("upload_media"); return "m1"

    def media_metadata(self, mid, alt):
        self._call("media_metadata")

    def post_reply(self, payload):
        self._call("post_reply"); self.n += 1; self.last_payload = payload; return str(self.n)

    def posts(self):
        return sum(c in ("post_reply", "upload_media", "media_metadata") for c in self.calls)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.q = Path(self.tmp.name) / "q"; self.q.mkdir()
        self.ledger = Path(self.tmp.name) / "ledger.jsonl"
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, e):
        (self.q / f"{e['entry_id']}.json").write_text(json.dumps(e, ensure_ascii=False))

    def go(self, client, apply=True, **kw):
        return P.run(self.q, self.ledger, apply, client, sleep=self.sleeps.append, out=io.StringIO(), **kw)

    def lines(self):
        return [json.loads(l) for l in self.ledger.read_text().splitlines()]


class P1(Base):
    def test_one_char_edit_rejected(self):
        e = entry(); e["text"] = e["text"][:-1] + "?"; self.put(e)
        c = Mock(); r = self.go(c)
        self.assertEqual(r[0].outcome, "rejected_hash_mismatch"); self.assertEqual(c.calls, [])

    def test_approval_hash_mismatch_rejected(self):
        e = entry(); e["approval"]["text_sha256"] = "0" * 64; self.put(e)
        c = Mock(); self.assertEqual(self.go(c)[0].outcome, "rejected_hash_mismatch"); self.assertEqual(c.calls, [])

    def test_trailing_space_not_trimmed(self):
        e = entry(); e["text"] += " "; self.put(e)
        self.assertEqual(self.go(Mock())[0].outcome, "rejected_hash_mismatch")

    def _invalid(self, mutate):
        e = entry(); mutate(e); self.put(e)
        c = Mock(); r = self.go(c)
        self.assertEqual(r[0].outcome, "rejected_invalid", r[0].detail); self.assertEqual(c.calls, [])

    def test_empty_quote(self): self._invalid(lambda e: e["approval"].update(quote="  "))
    def test_not_fred(self): self._invalid(lambda e: e["approval"].update(approved_by="chief"))
    def test_bad_date(self): self._invalid(lambda e: e["approval"].update(approved_at="yesterday"))
    def test_date_without_offset(self): self._invalid(lambda e: e["approval"].update(approved_at="2026-10-26T09:30:00"))
    def test_reply_to_not_digits(self): self._invalid(lambda e: e.update(reply_to_id="21144x"))

    def test_text_over_280_weighted(self):
        t = "\u4e00" * 141  # CJK weighs 2 -> 282
        e = entry(text=t); self.put(e)
        self.assertEqual(self.go(Mock())[0].outcome, "rejected_invalid")
        self.assertEqual(P.weighted_length("a" * 280), 280)
        self.assertEqual(P.weighted_length("see https://example.com/a/very/long/path/that/is/long"), 4 + 23)

    def test_alt_over_1000(self):
        img = self.q / "i.png"; img.write_bytes(b"x")
        self._invalid(lambda e: e.update(image_path="i.png", alt_text="a" * 1001))

    def test_image_without_alt(self):
        img = self.q / "i.png"; img.write_bytes(b"x")
        self._invalid(lambda e: e.update(image_path="i.png"))

    def test_queue_bytes_unchanged(self):
        e = entry(); self.put(e); before = (self.q / "e1.json").read_bytes()
        self.go(Mock()); self.go(Mock(), apply=False)
        self.assertEqual((self.q / "e1.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.q.iterdir()), ["e1.json"])


class P2(Base):
    def test_not_mentioned_goes_manual(self):
        self.put(entry()); c = Mock(tweet=NOT_MENTIONED); r = self.go(c)
        self.assertEqual(r[0].outcome, "manual_not_mentioned"); self.assertEqual(c.calls, ["get_tweet"])
        self.assertEqual(r[0].manual_url, "https://x.com/i/status/1000000000000000001")
        self.assertEqual(P.effective_status(entry(), self.lines()), "manual")
        self.assertEqual(self.go(Mock()), [])  # manual entries are not retried

    def test_quote_of_fred_passes(self):
        self.put(entry()); c = Mock(tweet=QUOTES_FRED); r = self.go(c)
        self.assertEqual(r[0].outcome, "posted"); self.assertIn("post_reply", c.calls)

    def test_reply_to_fred_post_is_not_quote(self):  # OQ-1 R2: thread position doesn't count
        tw = {"data": {"id": "4", "author_id": "9", "referenced_tweets": [{"type": "replied_to", "id": "60"}]},
              "includes": {"tweets": [{"id": "60", "author_id": "42"}], "users": [{"id": "42", "username": "HazeyDataFred"}]}}
        self.put(entry()); c = Mock(tweet=tw); r = self.go(c)
        self.assertEqual(r[0].outcome, "manual_not_mentioned"); self.assertEqual(c.posts(), 0)

    def test_get_failure_stops_run_no_post(self):
        self.put(entry("a")); self.put(entry("b", rid="5"))
        c = Mock(fail={"get_tweet": 503}); r = self.go(c)
        self.assertEqual([o.outcome for o in r], ["api_error"]); self.assertEqual(c.posts(), 0)


class P3(Base):
    def test_dry_run_zero_posts(self):
        img = self.q / "i.png"; img.write_bytes(b"x")
        self.put(entry(image_path="i.png", alt_text="chart")); c = Mock(); r = self.go(c, apply=False)
        self.assertEqual(r[0].outcome, "dry_run"); self.assertEqual(c.calls, ["get_tweet"])
        self.assertEqual(r[0].payload["reply"]["in_reply_to_tweet_id"], "1000000000000000001")

    def test_offline_dry_run_zero_calls(self):
        self.put(entry()); r = self.go(None, apply=False, offline=True)
        self.assertEqual(r[0].outcome, "dry_run")

    def test_post_with_image_order(self):
        img = self.q / "i.png"; img.write_bytes(b"x")
        self.put(entry(image_path="i.png", alt_text="chart")); c = Mock(); r = self.go(c)
        self.assertEqual(c.calls, ["get_tweet", "recent_replies", "upload_media", "media_metadata", "post_reply"])
        self.assertEqual(c.last_payload, {"text": entry()["text"], "reply": {"in_reply_to_tweet_id": "1000000000000000001"},
                                          "media": {"media_ids": ["m1"]}})
        self.assertEqual(r[0].post_url, "https://x.com/HazeyDataFred/status/101")

    def test_duplicate_zero_calls(self):
        self.put(entry()); self.go(Mock())
        c = Mock(); r = self.go(c)
        self.assertEqual(r[0].outcome, "skipped_duplicate"); self.assertEqual(c.calls, [])

    def test_edited_text_after_post_is_not_duplicate(self):
        self.put(entry()); self.go(Mock())
        self.put(entry(text="Different approved text")); c = Mock(); r = self.go(c)
        self.assertEqual(r[0].outcome, "posted")

    def test_crash_recovery(self):
        self.put(entry())
        # simulate: POST happened, ledger write never did, plus a torn trailing line
        self.ledger.write_text('{"entry_id": "e1", "outcome": "pos')
        c = Mock(replies=[{"id": "777", "text": entry()["text"]}]); r = self.go(c)
        self.assertEqual(r[0].outcome, "posted"); self.assertNotIn("post_reply", c.calls)
        self.assertEqual(r[0].post_url, "https://x.com/HazeyDataFred/status/777")
        c2 = Mock(); self.assertEqual(self.go(c2)[0].outcome, "skipped_duplicate"); self.assertEqual(c2.calls, [])

    def test_500_on_post(self):
        self.put(entry("a")); self.put(entry("b", rid="5"))
        c = Mock(fail={"post_reply": 500}); r = self.go(c)
        self.assertEqual([(o.outcome, o.http_status) for o in r], [("api_error", 500), ("api_error", 500)])
        self.assertEqual(c.calls.count("post_reply"), 2)  # no retry, run continues
        self.assertEqual(P.effective_status(entry("a"), self.lines()), "failed")
        c2 = Mock(); self.assertEqual(self.go(c2), []); self.assertEqual(c2.calls, [])

    def test_403_on_post_marks_manual(self):  # AC 6a
        self.put(entry("a")); self.put(entry("b", rid="5"))
        c = Mock(fail={"post_reply": 403}); r = self.go(c)
        self.assertEqual([(o.outcome, o.http_status) for o in r], [("api_error", 403), ("api_error", 403)])
        self.assertEqual(c.calls.count("post_reply"), 2)  # one attempt each, no probing retries
        self.assertEqual(r[0].manual_url, "https://x.com/i/status/1000000000000000001")
        self.assertEqual(P.effective_status(entry("a"), self.lines()), "manual")
        c2 = Mock(); self.assertEqual(self.go(c2), []); self.assertEqual(c2.calls, [])

    def test_429_stops_run(self):
        self.put(entry("a")); self.put(entry("b", rid="5"))
        c = Mock(fail={"post_reply": 429}); r = self.go(c)
        self.assertEqual([o.outcome for o in r], ["api_error"]); self.assertEqual(c.calls.count("post_reply"), 1)

    def test_media_failure_no_post(self):
        img = self.q / "i.png"; img.write_bytes(b"x")
        self.put(entry(image_path="i.png", alt_text="chart")); c = Mock(fail={"media_metadata": 400}); r = self.go(c)
        self.assertEqual(r[0].outcome, "api_error"); self.assertNotIn("post_reply", c.calls)

    def test_cap_and_spacing(self):
        for i in range(7):
            self.put(entry(f"e{i}", text=f"reply {i}", rid=str(10 + i)))
        c = Mock(); r = self.go(c, max_posts=99)
        self.assertEqual(sum(o.outcome == "posted" for o in r), 5)
        self.assertEqual(len(self.sleeps), 4); self.assertTrue(all(s > 29 for s in self.sleeps))

    def test_apply_offline_exclusive(self):
        with self.assertRaises(ValueError):
            self.go(None, apply=True, offline=True)


class P4(Base):
    def test_no_secret_in_ledger_or_output(self):
        os.environ["TWITTER_ACCESS_TOKEN"] = FAKE_SECRET
        try:
            self.put(entry("a")); self.put(entry("b", rid="5"))
            self.go(Mock(fail={"post_reply": 500}))
            self.assertNotIn(FAKE_SECRET, self.ledger.read_text())
            self.assertNotIn("Authorization", self.ledger.read_text())
        finally:
            del os.environ["TWITTER_ACCESS_TOKEN"]

    def test_xclient_reads_env_only(self):
        src = Path(P.__file__).read_text()
        self.assertNotIn("load_env", src); self.assertNotIn("Path.home()", src)


class Review1(Base):
    """Fixes for the T1 review: AC 2 crash, AC 6a/16 403 scope, AC 15 fsync, AC 3a, AC 3b."""

    def test_non_object_approval_rejected_and_run_continues(self):  # AC 2
        bad = entry("a"); bad["approval"] = "yes"; self.put(bad)
        lst = entry("b"); lst["approval"] = ["Fred"]; self.put(lst)
        self.put(entry("c", rid="5"))
        c = Mock(); r = self.go(c)
        self.assertEqual([(o.entry_id, o.outcome) for o in r],
                         [("a", "rejected_invalid"), ("b", "rejected_invalid"), ("c", "posted")])
        self.assertEqual([l["outcome"] for l in self.lines()], ["rejected_invalid", "rejected_invalid", "posted"])

    def test_non_object_queue_file_rejected(self):  # AC 2: top-level JSON that is not an object
        (self.q / "a.json").write_text('"yes"'); self.put(entry("b"))
        r = self.go(Mock())
        self.assertEqual([o.outcome for o in r], ["rejected_invalid", "posted"])

    def test_403_on_media_is_failed_not_manual(self):  # AC 6a: only a 403 on the POST goes manual
        img = self.q / "i.png"; img.write_bytes(b"x")
        self.put(entry(image_path="i.png", alt_text="chart"))
        c = Mock(fail={"upload_media": 403}); r = self.go(c)
        self.assertEqual((r[0].outcome, r[0].http_status, r[0].manual_url), ("api_error", 403, None))
        self.assertNotIn("post_reply", c.calls)
        self.assertEqual(P.effective_status(entry(), self.lines()), "failed")

    def test_403_on_recent_replies_is_failed(self):
        self.put(entry()); r = self.go(Mock(fail={"recent_replies": 403}))
        self.assertEqual(r[0].http_status, 403)
        self.assertEqual(P.effective_status(entry(), self.lines()), "failed")

    def test_403_on_mention_get_is_failed(self):
        self.put(entry()); r = self.go(Mock(fail={"get_tweet": 403}))
        self.assertEqual(r[0].http_status, 403)
        self.assertEqual(P.effective_status(entry(), self.lines()), "failed")

    def test_fsync_called_per_ledger_line(self):  # AC 15: kills the fsync-removal mutant
        self.put(entry("a")); self.put(entry("b", rid="5"))
        with mock.patch.object(P.os, "fsync", wraps=os.fsync) as fs:
            self.go(Mock())
        self.assertEqual(fs.call_count, len(self.lines()))
        self.assertEqual(fs.call_count, 2)

    def _mode_refused(self, mode):
        self.put(entry()); os.chmod(self.q, mode)
        try:
            c = Mock()
            with self.assertRaises(SystemExit):
                self.go(c)
            with self.assertRaises(SystemExit):
                self.go(None, apply=False, offline=True)
            self.assertEqual(c.calls, []); self.assertFalse(self.ledger.exists() and self.ledger.read_text())
        finally:
            os.chmod(self.q, 0o700)

    def test_group_writable_queue_refused(self): self._mode_refused(0o770)  # AC 3a
    def test_world_writable_queue_refused(self): self._mode_refused(0o707)  # AC 3a

    def test_private_queue_allowed(self):  # AC 3a
        os.chmod(self.q, 0o700); self.put(entry())
        self.assertEqual(self.go(Mock())[0].outcome, "posted")

    def _git(self, *a):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        return subprocess.run(["git", *a], cwd=self.q, env=env, check=True, capture_output=True, text=True).stdout.strip()

    def test_ledger_records_queue_commit_sha(self):  # AC 3b
        self._git("init", "-q"); self.put(entry("a")); self.put(entry("b", rid="5"))
        self._git("add", "a.json"); self._git("commit", "-q", "-m", "queue a")
        sha = self._git("rev-parse", "HEAD")
        os.chmod(self.q, 0o700)
        r = self.go(Mock())
        by = {l["entry_id"]: l for l in self.lines()}
        self.assertEqual(by["a"]["outcome"], "posted"); self.assertEqual(by["a"]["queue_commit"], sha)
        self.assertEqual(by["b"]["queue_commit"], "uncommitted")
        self.assertEqual(len(sha), 40)

    def test_queue_commit_flags_dirty_and_on_post_403(self):  # AC 3b
        self._git("init", "-q"); self.put(entry("a"))
        self._git("add", "a.json"); self._git("commit", "-q", "-m", "q")
        sha = self._git("rev-parse", "HEAD")
        (self.q / "a.json").write_text((self.q / "a.json").read_text() + "\n")
        os.chmod(self.q, 0o700)
        self.go(Mock(fail={"post_reply": 403}))
        self.assertEqual(self.lines()[0]["queue_commit"], sha + "+dirty")

    def test_dry_run_has_no_queue_commit(self):
        self.put(entry()); self.go(Mock(), apply=False)
        self.assertNotIn("queue_commit", self.lines()[0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
