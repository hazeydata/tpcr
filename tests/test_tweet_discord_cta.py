"""TPCR-DISCORD-GROWTH-001 (Fred, Nov 18 2026): one Discord line on the 8:30 AM OBSERVED reply only, for 14 posting
days set in config/tweet_format.json discord_cta. FORECAST text never carries it. Off by default."""
import io
import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operations" / "tweet_v2"))
sys.path.insert(0, str(ROOT / "operations" / "scripts"))

import tweet_copy as tc  # noqa: E402
import tweet_format as tf  # noqa: E402

CODE = "AbCdEf12"
CTA = tc.DISCORD_CTA.format(code=CODE)
LINE = "Free daily forecasts live on our Discord: https://discord.gg/AbCdEf12"
FX = ROOT / "tests" / "fixtures" / "tweet_v2_samples"
DATES = ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06"]
SHIPPED = (3.0, 6.0)


def cfg(code=CODE, first="2026-11-19", last="2026-12-02"):
    return {"discord_cta": {"code": code, "first_post_date": first, "last_post_date": last}}


def parse(c):
    err = io.StringIO()
    return tf.parse_discord_cta(c, err=err), err.getvalue()


def park(ob, fc):
    return {"ob": ob, "fc": fc, "gap": round(ob - fc, 1)}


def worst_case_meta():
    """4 parks all far off (2-weight emojis), 3-digit values, Hollywood Studios (longest name) the named miss with
    'quieter', 'No park close to forecast.' lead, longest date (Wed, Sep 30)."""
    parks = {"Magic Kingdom": park(100.0, 199.9), "EPCOT": park(100.0, 199.9),
             "Hollywood Studios": park(100.0, 200.0), "Animal Kingdom": park(100.0, 199.9)}
    return {"date": "2026-09-30", "headline": "x", "parks": parks, "graded": list(parks)}


class Line(unittest.TestCase):
    def test_exact_bytes(self):
        self.assertEqual(CTA, LINE)
        self.assertEqual(CTA.encode(), LINE.encode())

    def test_weighted_len_url_rule(self):
        self.assertEqual(tc.weighted_len("see https://discord.gg/abcdefghij"), 4 + 23)
        self.assertEqual(tc.weighted_len("see https://discord.gg/" + "a" * 200), 4 + 23)
        self.assertEqual(tc.weighted_len("http://x.co"), 23)
        self.assertEqual(tc.weighted_len("a https://x.co b https://y.co\nc"), 2 * 23 + 7)
        self.assertEqual(tc.weighted_len(LINE), len("Free daily forecasts live on our Discord: ") + 23)
        # existing weighting kept
        self.assertEqual(tc.weighted_len("✅⬇️⬆️⏬⏫"), 10)
        self.assertEqual(tc.weighted_len("中文 ab–"), 8)
        self.assertEqual(tc.weighted_len("https:/no"), 9)  # not a URL

    def test_worst_case_fits(self):
        m = worst_case_meta()
        base = tc.reply_text(m, SHIPPED)
        t = tc.reply_text(m, SHIPPED, cta=CTA)
        self.assertEqual(t, base + "\n\n" + CTA)
        self.assertIn("No park close to forecast. Hollywood Studios 100.0 quieter.", t)
        self.assertEqual(t.count("⏬"), 4)
        self.assertIn("Wed, Sep 30", t)
        n = tc.weighted_len(t)
        print(f"\nworst-case OBSERVED with Discord line, weighted {n}:\n{t}", file=sys.stderr)
        self.assertLessEqual(n, 280)

    def test_overflow_posts_without_line(self):
        m = worst_case_meta()
        base = tc.reply_text(m, SHIPPED)
        long_cta = "x" * (281 - tc.weighted_len(base) - 2)
        self.assertGreater(tc.weighted_len(base + "\n\n" + long_cta), 280)
        self.assertEqual(tc.reply_text(m, SHIPPED, cta=long_cta), base)
        fits = "x" * (280 - tc.weighted_len(base) - 2)
        self.assertEqual(tc.reply_text(m, SHIPPED, cta=fits), base + "\n\n" + fits)

    def test_no_tiers_or_ungraded_still_none(self):
        m = worst_case_meta()
        self.assertIsNone(tc.reply_text(m, None, cta=CTA))
        self.assertIsNone(tc.reply_text({**m, "graded": []}, SHIPPED, cta=CTA))

    def test_forecast_text_takes_no_cta(self):
        import inspect
        self.assertEqual(list(inspect.signature(tc.forecast_text).parameters), ["m"])


class WithCards(unittest.TestCase):
    """Every test_tweet_text reply case: with the line = without + blank line + line, and every one fits."""
    @classmethod
    def setUpClass(cls):
        try:
            import pandas  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pandas not installed")
        import cards
        cls.c = cards

    def metas(self):
        names = ["MK", "EP", "HS", "AK"]
        for combo in itertools.product((-19.9, -4.0, 0.0, 4.0, 19.9, None), repeat=4):
            st = {p: "closed" for p, v in zip(names, combo) if v is None}
            fc = {p: (None if v is None else 80.0) for p, v in zip(names, combo)}
            ob = {p: (None if v is None else 80.0 + v) for p, v in zip(names, combo)}
            yield self.c.reply_meta("2026-10-28", fc, ob, st, "x")
        yield self.c.reply_meta("2026-10-28", {"MK": 20.0, "EP": 20.0, "HS": 20.0, "AK": 30.0},
                                {"MK": 21.5, "EP": 18.0, "HS": 22.0, "AK": 23.3}, None, "x")

    def test_append_only_and_fits(self):
        n = 0
        for m in self.metas():
            for tiers in ((2.0, 5.0), SHIPPED):
                base = tc.reply_text(m, tiers)
                if base is None:
                    self.assertIsNone(tc.reply_text(m, tiers, cta=CTA))
                    continue
                n += 1
                t = tc.reply_text(m, tiers, cta=CTA)
                self.assertEqual(t, base + "\n\n" + CTA)
                self.assertTrue(t.endswith("Lower WTI = shorter waits.\n\n" + LINE))
                self.assertLessEqual(tc.weighted_len(t), 280, t)
        self.assertGreater(n, 1000)

    def test_forecast_never_has_discord(self):
        S = {p: [10.0, 20.0] for p in ("MK", "EP", "HS", "AK")}
        for combo in itertools.product((5.0, 15.0, 25.0), repeat=4):
            t = tc.forecast_text(self.c.meta("2026-10-28", dict(zip(S, combo)), S, None, "x"))
            self.assertNotIn("discord", t.lower())


def have_deps():
    try:
        import pandas, pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_deps(), "pandas/pyarrow not installed (run with the TPCR venv)")
class Samples(unittest.TestCase):
    """The tpcr-meta#12 reply samples (Sep 2-6), rendered from the real fixture data by cards.py."""
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix="tweet-discord-"))
        for d in DATES:
            r = subprocess.run([sys.executable, str(ROOT / "operations" / "tweet_v2" / "cards.py"), d, str(cls.out),
                                "--csv", str(FX / "forecast_vs_actual_2026-09-02_06.csv"),
                                "--history", str(FX / "history_wdw4_2022-01-01_2026-09-05.parquet"),
                                "--closures", str(ROOT / "data" / "closures.csv")],
                               capture_output=True, text=True, timeout=120, cwd=ROOT)
            assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, True)

    def test_samples(self):
        for d in DATES:
            m = json.loads((self.out / f"R-{d}.meta.json").read_text())
            base = tc.reply_text(m, SHIPPED)
            self.assertIsNotNone(base, d)
            t = tc.reply_text(m, SHIPPED, cta=CTA)
            self.assertEqual(t, base + "\n\n" + CTA, d)
            self.assertLessEqual(tc.weighted_len(t), 280, d)
            f = tc.forecast_text(json.loads((self.out / f"F-{d}.meta.json").read_text()))
            self.assertNotIn("discord", (f or "").lower(), d)


class Config(unittest.TestCase):
    def test_committed_config_is_off(self):
        c = json.loads((ROOT / "config" / "tweet_format.json").read_text())
        self.assertEqual(c["discord_cta"], {"code": None, "first_post_date": None, "last_post_date": None})
        self.assertIsNone(tf.DISCORD_CTA)
        self.assertIsNone(tf.discord_cta_line(date(2026, 11, 20)))

    def test_all_null_or_missing_is_off_silently(self):
        for c in ({"discord_cta": {"code": None, "first_post_date": None, "last_post_date": None}}, {}, {"discord_cta": None}):
            self.assertEqual(parse(c), (None, ""), c)

    def test_partial_is_off_with_one_error(self):
        for c in (cfg(first=None, last=None), cfg(code=None), cfg(last=None), {"discord_cta": {"code": CODE}}):
            cta, err = parse(c)
            self.assertIsNone(cta, c)
            self.assertEqual(err.count("\n"), 1, err)
            self.assertTrue(err.startswith("ERROR tweet_format: discord_cta partially set"), err)

    def test_bad_values_rejected(self):
        cases = {"code": [cfg(code="a"), cfg(code="x" * 33), cfg(code="ab cd"), cfg(code="ab/cd"), cfg(code=123),
                          cfg(code="https://discord.gg/x")],
                 "dates": [cfg(first="2026-11-31"), cfg(first="11/19/2026"), cfg(first="20261119"), cfg(last=20261202)],
                 "after": [cfg(first="2026-12-02", last="2026-11-19")],
                 "max 14": [cfg(last="2026-12-03"), cfg(first="2026-01-01", last="2026-12-31")]}
        for word, cs in cases.items():
            for c in cs:
                cta, err = parse(c)
                self.assertIsNone(cta, c)
                self.assertEqual(err.count("\n"), 1, err)
                self.assertIn(word, err, (c, err))
        self.assertIn("not an object", parse({"discord_cta": "AbCd"})[1])

    def test_valid(self):
        self.assertEqual(parse(cfg()), ({"code": CODE, "first": date(2026, 11, 19), "last": date(2026, 12, 2)}, ""))
        self.assertIsNotNone(parse(cfg(first="2026-11-19", last="2026-11-19"))[0])
        self.assertIsNotNone(parse(cfg(code="ab-CD-09"))[0])

    def test_window_boundaries(self):
        cta, _ = parse(cfg())
        line = lambda d: tf.discord_cta_line(d, cta)  # noqa: E731
        self.assertIsNone(line(date(2026, 11, 18)))
        self.assertEqual(line(date(2026, 11, 19)), LINE)
        self.assertEqual(line(date(2026, 11, 25)), LINE)
        self.assertEqual(line(date(2026, 12, 2)), LINE)
        self.assertIsNone(line(date(2026, 12, 3)))
        on = sum(line(date.fromordinal(date(2026, 11, 1).toordinal() + i)) is not None for i in range(60))
        self.assertEqual(on, 14)

    def test_datetime_is_taken_in_toronto(self):
        cta, _ = parse(cfg())
        # 2026-11-19 03:00 UTC = Nov 18 22:00 ET: still the day before the window
        self.assertIsNone(tf.discord_cta_line(datetime(2026, 11, 19, 3, tzinfo=timezone.utc), cta))
        self.assertEqual(tf.discord_cta_line(datetime(2026, 11, 19, 13, 30, tzinfo=timezone.utc), cta), LINE)

    def test_module_import_logs_bad_config(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            c = json.loads((ROOT / "config" / "tweet_format.json").read_text())
            c.update(cfg(last="2027-01-31"))
            (tmp / "tf.json").write_text(json.dumps(c))
            code = "import tweet_format as t; print(t.DISCORD_CTA, t.REPLY_TIERS, t.CONFIG_ERROR)"
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                               cwd=ROOT / "operations" / "scripts", env={"PATH": "/usr/bin:/bin",
                                                                         "TPCR_TWEET_FORMAT_CONFIG": str(tmp / "tf.json")})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "None (3.0, 6.0) None")
            self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
            self.assertIn("ERROR tweet_format: discord_cta window", r.stderr)
        finally:
            shutil.rmtree(tmp, True)


class PostText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("requests_oauthlib", mock.MagicMock())
        import tweet_v2
        cls.v2 = tweet_v2

    def arts(self):
        f = {"date": "2026-11-19", "headline": "x",
             "labels": {"Magic Kingdom": "Normal", "EPCOT": "Normal", "Hollywood Studios": "Busier than normal",
                        "Animal Kingdom": "Normal"}}
        return {"F": {"meta": f}, "R": {"meta": worst_case_meta()}}

    def test_observed_passes_the_line_in_window(self):
        cta, _ = parse(cfg())
        with mock.patch.object(tf, "DISCORD_CTA", cta), mock.patch.object(tf, "REPLY_TIERS", SHIPPED):
            base = tc.reply_text(worst_case_meta(), SHIPPED)
            self.assertEqual(self.v2.post_text("observed", self.arts(), today=date(2026, 11, 19)), base + "\n\n" + LINE)
            self.assertEqual(self.v2.post_text("observed", self.arts(), today=date(2026, 12, 3)), base)
            with mock.patch.object(self.v2, "now", return_value=datetime(2026, 12, 2, 13, 30, tzinfo=timezone.utc)):
                self.assertEqual(self.v2.post_text("observed", self.arts()), base + "\n\n" + LINE)
            with mock.patch.object(self.v2, "now", return_value=datetime(2026, 12, 3, 13, 30, tzinfo=timezone.utc)):
                self.assertEqual(self.v2.post_text("observed", self.arts()), base)

    def test_forecast_never(self):
        cta, _ = parse(cfg())
        with mock.patch.object(tf, "DISCORD_CTA", cta):
            for d in (date(2026, 11, 19), date(2026, 11, 25), None):
                t = self.v2.post_text("predicted", self.arts(), today=d)
                self.assertNotIn("discord", t.lower())
                self.assertEqual(t, tc.forecast_text(self.arts()["F"]["meta"]))

    def test_off_by_default(self):
        with mock.patch.object(tf, "REPLY_TIERS", SHIPPED):
            self.assertEqual(self.v2.post_text("observed", self.arts(), today=date(2026, 11, 25)),
                             tc.reply_text(worst_case_meta(), SHIPPED))


if __name__ == "__main__":
    unittest.main()
