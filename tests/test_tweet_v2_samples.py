"""TPCR-TWEET-RESTYLE-CUTOVER: dry-render every approved tpcr-meta#12 sample and compare against it.

Samples (tpcr-meta PR #12 @ 003735e, cycles/2026-10-24-forecast-tweet-theme/weather5/), copied verbatim into
tests/fixtures/tweet_v2_samples/expected/: forecast cards Sep 2-6 (html/2026-09-0N.html, alt-text.md) and
scorecard replies, design C, Sep 2-6 (reply/html/C-2026-09-0N.html, reply/alt-text.md). Inputs are the same real
data: wti_forecast_vs_actual_last14.csv rows for Sep 2-6 and the WDW historical rows of wti.parquet.

Replies must match byte for byte after Fred's Oct 28 on-target ruling (headline only, OCT28 below). Forecast cards must match after exactly the three edits Fred approved on
Oct 26 (recorded in #12 decisions.md, "Edge-case answers"): no "Today ·" in the kicker, no "today" in the
headline, axis label "shared scale, 10–35". The samples predate those edits. Dry render = cards.py only: no
Chrome, no network, nothing posted."""
import re
import subprocess
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures" / "tweet_v2_samples"
EXP = FX / "expected"
CARDS = ROOT / "operations" / "tweet_v2" / "cards.py"
DATES = ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06"]
HEADLINES = {  # the locked #12 reply headlines (THREAD-SUMMARY.md)
    "2026-09-02": "Every park came in low. Animal Kingdom by 12.2",
    "2026-09-03": "Animal Kingdom came in 16.3 below forecast",
    "2026-09-04": "Animal Kingdom 6.5 low. Two parks on target",
    "2026-09-05": "Three parks on target. Animal Kingdom 6.7 low",
    "2026-09-06": "All four parks within 2 of forecast",
}
# Fred, Oct 28 2026 2:57 PM ET: the card's on-target rule is the reply text's A (3.0, was 2.0). The only change to the
# #12 replies is the headline on the three days where a miss of 2.1-3.0 (or the "within" number) now reads differently.
OCT28 = {
    "2026-09-02": "Every park came in low. Animal Kingdom by 12.2",
    "2026-09-03": "Animal Kingdom 16.3 low. Two parks on target",
    "2026-09-04": "Three parks on target. Animal Kingdom 6.5 low",
    "2026-09-05": "Three parks on target. Animal Kingdom 6.7 low",
    "2026-09-06": "All four parks within 3 of forecast",
}


def oct28(s, d):
    """Apply Fred's Oct 28 on-target ruling to a #12 reply sample (html or alt text): its headline only."""
    assert HEADLINES[d] in s
    return s.replace(HEADLINES[d], OCT28[d])


def oct26(s):
    """Apply Fred's Oct 26 edits to a pre-Oct-26 #12 forecast sample (html or alt text)."""
    s = s.replace("Today · ", "")
    s = re.sub(r"\b(busy|quiet|normal) today\b", r"\1", s)
    return s.replace("normal range, 10–35", "shared scale, 10–35")


def alt_sections(md, prefix=""):
    out, key = {}, None
    for line in md.read_text().splitlines():
        if line.startswith("## "):
            key = line[3:].strip()
        elif key and line.strip():
            out[key] = line.strip()
    return {k[len(prefix):]: v for k, v in out.items() if k.startswith(prefix)}


def have_deps():
    try:
        import pandas, pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_deps(), "pandas/pyarrow not installed (run with the TPCR venv)")
class Samples12(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix="tweetv2-samples-"))
        for d in DATES:
            r = subprocess.run([sys.executable, str(CARDS), d, str(cls.out), "--csv", str(FX / "forecast_vs_actual_2026-09-02_06.csv"),
                                "--history", str(FX / "history_wdw4_2022-01-01_2026-09-05.parquet"),
                                "--closures", str(ROOT / "data" / "closures.csv")],
                               capture_output=True, text=True, timeout=120, cwd=ROOT)
            assert r.returncode == 0, r.stderr
        cls.falt = alt_sections(EXP / "forecast-alt-text.md")
        cls.ralt = alt_sections(EXP / "reply-alt-text.md", "C-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, True)

    def got(self, name):
        return (self.out / name).read_text()


def _forecast(d):
    def t(self):
        self.assertEqual(self.got(f"F-{d}.html"), oct26((EXP / f"F-{d}.html").read_text()))
        self.assertEqual(self.got(f"F-{d}.alt.txt").strip(), oct26(self.falt[d]))
        self.assertNotIn("oday", self.got(f"F-{d}.html"))
    return t


def _reply(d):
    def t(self):
        self.assertEqual(self.got(f"R-{d}.html"), oct28((EXP / f"R-{d}.html").read_text(), d))
        self.assertEqual(self.got(f"R-{d}.alt.txt").strip(), oct28(self.ralt[d], d))
        self.assertIn(f'class="h">{OCT28[d]}<', self.got(f"R-{d}.html"))
    return t


for _d in DATES:
    setattr(Samples12, f"test_forecast_{_d.replace('-', '_')}_matches_12", _forecast(_d))
    setattr(Samples12, f"test_reply_C_{_d.replace('-', '_')}_matches_12", _reply(_d))


class Oct26Edits(unittest.TestCase):
    def test_only_the_three_approved_edits_are_applied(self):
        s = (EXP / "F-2026-09-05.html").read_text()
        self.assertIn("Today · ", s); self.assertIn("busy today", s); self.assertIn("normal range, 10–35", s)
        self.assertLess(len(s) - len(oct26(s)), 20)


if __name__ == "__main__":
    unittest.main()
