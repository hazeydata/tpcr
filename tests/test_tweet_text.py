"""New-format tweet text (operations/tweet_v2/tweet_copy.py) built from the real cards.py meta."""
import itertools
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operations" / "tweet_v2"))

import tweet_copy as tc  # noqa: E402

D = "2026-10-28"
S = {p: [10.0, 20.0] for p in ("MK", "EP", "HS", "AK")}
T = (2.0, 5.0)
CIRCLES = re.compile("[\U0001F534\U0001F535\U0001F7E0-\U0001F7EB\u26AA\u26AB\u2B55]")


class TweetText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import pandas  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pandas not installed")
        import cards
        cls.c = cards

    def fc_text(self, fc, status=None):
        return tc.forecast_text(self.c.meta(D, fc, S, status, "x"))

    def rp_text(self, fc, ob, status=None):
        return tc.reply_text(self.c.reply_meta(D, fc, ob, status, "x"), T)

    def test_forecast_all_normal(self):
        self.assertEqual(self.fc_text({p: 15.0 for p in S}),
                         "WDW Crowd FORECAST: Wed, Oct 28\nAll four parks normal.\n#WDW #DisneyWorld #WaitTimes")

    def test_forecast_mixed(self):
        self.assertEqual(self.fc_text({"MK": 15.0, "EP": 15.0, "HS": 15.0, "AK": 25.0}).split("\n")[1],
                         "Animal Kingdom busier than normal. Other parks normal.")
        self.assertEqual(self.fc_text({"MK": 25.0, "EP": 5.0, "HS": 25.0, "AK": 15.0}).split("\n")[1],
                         "Magic Kingdom and Hollywood Studios busier than normal. EPCOT quieter than normal. Animal Kingdom normal.")
        self.assertEqual(self.fc_text({"MK": 25.0, "EP": 25.0, "HS": 25.0, "AK": 5.0}).split("\n")[1],
                         "Magic Kingdom, EPCOT and Hollywood Studios busier than normal. Animal Kingdom quieter than normal.")

    def test_forecast_closed_park(self):
        fc = {"MK": 15.0, "EP": 15.0, "HS": None, "AK": 15.0}
        self.assertEqual(self.fc_text(fc, {"HS": "closed"}).split("\n")[1], "Hollywood Studios closed. Other parks normal.")
        fc = {"MK": 5.0, "EP": None, "HS": None, "AK": 15.0}
        self.assertEqual(self.fc_text(fc, {"EP": "closed", "HS": "closed"}).split("\n")[1],
                         "Magic Kingdom quieter than normal. EPCOT and Hollywood Studios closed. Animal Kingdom normal.")
        self.assertIsNone(self.fc_text({p: None for p in S}, {p: "closed" for p in S}))

    def test_observed_mixed(self):
        fc = {"MK": 20.0, "EP": 20.0, "HS": 20.0, "AK": 30.0}
        ob = {"MK": 21.5, "EP": 18.0, "HS": 22.0, "AK": 23.3}
        self.assertEqual(self.rp_text(fc, ob),
                         "WDW Crowds OBSERVED: Wed, Oct 28\nThree parks close to forecast. Animal Kingdom 6.7 quieter.\n\n"
                         "✅ Magic Kingdom: 21.5 (forecast 20.0)\n✅ EPCOT: 18.0 (forecast 20.0)\n"
                         "✅ Hollywood Studios: 22.0 (forecast 20.0)\n⏬ Animal Kingdom: 23.3 (forecast 30.0)\n\nLower WTI = shorter waits.")

    def test_tier_boundaries(self):
        fc = {p: 20.0 for p in S}
        for d, mark in ((2.0, "✅"), (-2.0, "✅"), (2.1, "⬆️"), (-2.1, "⬇️"), (5.0, "⬆️"), (-5.0, "⬇️"), (5.1, "⏫"), (-5.1, "⏬")):
            self.assertIn(f"{mark} Animal Kingdom:", self.rp_text(fc, {**fc, "AK": 20.0 + d}), d)

    def test_tiers_come_from_the_argument(self):
        fc = {p: 20.0 for p in S}
        m = self.c.reply_meta(D, fc, {**fc, "AK": 23.0}, {}, "x")
        self.assertIn("✅ Animal Kingdom:", tc.reply_text(m, (3.0, 6.0)))
        self.assertIn("⏫ Animal Kingdom:", tc.reply_text(m, (1.0, 2.5)))
        try:
            unset = tc.reply_text(m, None)
        except TypeError as e:
            unset = f"raised {e}"
        self.assertIsNone(unset)

    def test_arrow_direction_and_summary(self):
        fc = {p: 20.0 for p in S}
        t = self.rp_text(fc, {"MK": 30.0, "EP": 15.0, "HS": 24.0, "AK": 17.0})
        self.assertIn("⏫ Magic Kingdom: 30.0 (forecast 20.0)", t)
        self.assertIn("⬇️ EPCOT: 15.0 (forecast 20.0)", t)
        self.assertIn("⬆️ Hollywood Studios: 24.0 (forecast 20.0)", t)
        self.assertIn("⬇️ Animal Kingdom: 17.0 (forecast 20.0)", t)
        self.assertEqual(t.split("\n")[1], "No park close to forecast. Magic Kingdom 10.0 busier.")
        self.assertEqual(self.rp_text(fc, dict(fc)).split("\n")[1], "All four parks close to forecast.")
        self.assertEqual(self.rp_text(fc, {**fc, "EP": 16.0}).split("\n")[1], "Three parks close to forecast. EPCOT 4.0 quieter.")

    def test_observed_closed_park(self):
        fc = {"MK": 20.0, "EP": 20.0, "HS": None, "AK": 20.0}
        ob = {"MK": 20.0, "EP": 20.0, "HS": None, "AK": 20.5}
        t = self.rp_text(fc, ob, {"HS": "closed"})
        self.assertEqual(t.split("\n")[1], "All open parks close to forecast.")
        self.assertIn("\nHollywood Studios: closed\n", t)
        ob["MK"] = 25.0
        self.assertEqual(self.rp_text(fc, ob, {"HS": "closed"}).split("\n")[1],
                         "Two parks close to forecast. Magic Kingdom 5.0 busier.")

    def every_text(self):
        names = list(S)
        for combo in itertools.product((5.0, 15.0, 25.0, None), repeat=4):
            st = {p: "closed" for p, v in zip(names, combo) if v is None}
            t = self.fc_text(dict(zip(names, combo)), st)
            if t is not None:
                yield t
        for combo in itertools.product((-19.9, -4.0, 0.0, 4.0, 19.9, None), repeat=4):
            st = {p: "closed" for p, v in zip(names, combo) if v is None}
            fc = {p: (None if v is None else 80.0) for p, v in zip(names, combo)}
            ob = {p: (None if v is None else 80.0 + v) for p, v in zip(names, combo)}
            t = self.rp_text(fc, ob, st)
            if t is not None:
                yield t

    def test_every_text_fits_and_has_no_circles(self):
        n = 0
        for t in self.every_text():
            n += 1
            self.assertLess(tc.weighted_len(t), 280, t)
            self.assertIsNone(CIRCLES.search(t), t)
        self.assertGreater(n, 400)

    def test_circle_ban_detector_and_weights(self):
        self.assertIsNotNone(CIRCLES.search("🟢 🟡 🔴 🟠"))
        self.assertEqual(tc.weighted_len("✅⬇️⬆️⏬⏫"), 10)
        self.assertEqual(tc.weighted_len("ab–"), 3)
        self.assertIsNone(tc._fit("x" * 281))
        self.assertEqual(tc._fit("x" * 280), "x" * 280)
        self.assertEqual(tc._fit("x" * 278 + "✅"), "x" * 278 + "✅")

    def test_non_latin_weighs_two(self):
        self.assertEqual(tc.weighted_len("中文"), 4)
        self.assertEqual(tc.weighted_len("a−b"), 4)
        self.assertIsNone(tc._fit("中" * 141))
        self.assertEqual(tc._fit("中" * 140), "中" * 140)

    def test_readme_examples_are_the_code_output(self):
        fc = {"MK": 20.0, "EP": 20.0, "HS": 20.0, "AK": 30.0}
        ob = {"MK": 21.5, "EP": 18.0, "HS": 22.0, "AK": 23.3}
        readme = (ROOT / "operations" / "tweet_v2" / "README.md").read_text()
        self.assertIn(self.rp_text(fc, ob), readme)
        self.assertIn(self.fc_text({"MK": 15.0, "EP": 15.0, "HS": 15.0, "AK": 25.0}), readme)


if __name__ == "__main__":
    unittest.main()
