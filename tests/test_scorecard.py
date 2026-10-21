import ast
import hashlib
import importlib
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SC = ROOT / "operations" / "scorecard"
sys.path.insert(0, str(SC))
sys.path.insert(0, str(ROOT / "operations" / "scripts"))

import backtest
import drift_alert
import scorecard as sc
import tweet_note
import weekly

sys.modules.setdefault("requests_oauthlib", type(sys)("requests_oauthlib")).OAuth1 = object
import wti_predicted_tweet as wpt

AS_OF = date(2026, 10, 12)


def days(n, end=AS_OF):
    return [end - timedelta(days=i) for i in range(n)]


def wti_rows(park, errs, end=AS_OF, fc=30.0, ev="2026-10-13"):
    return [{"park_code": park, "park_date": d.isoformat(), "forecast_wti": fc, "actual_wti": fc - e, "wti_error": e,
             "wti_abs_error": abs(e), "evaluation_date": ev} for d, e in zip(days(len(errs), end), errs)]


def slot_rows(entity, errs, end=AS_OF, ev="2026-10-13"):
    return [{"entity_code": entity, "park_date": d.isoformat(), "time_slot": f"{h:02d}:00", "forecast_wait": 50.0,
             "actual_wait": 50.0 - e, "signed_error": e, "evaluation_date": ev}
            for d in days(len(errs) // 4, end) for h, e in zip(range(9, 13), errs)]


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.acc, self.out = self.tmp / "acc", self.tmp / "out"
        self.acc.mkdir()
        p = mock.patch.dict(os.environ)
        p.start()
        for k in [k for k in os.environ if k.startswith("TPCR_")]:
            os.environ.pop(k, None)
        os.environ.update({"TPCR_ACCURACY_DIR": str(self.acc), "TPCR_SCORECARD_DIR": str(self.out)})
        self.addCleanup(p.stop)

    def write(self, wti, slot):
        pd.DataFrame(wti).to_parquet(self.acc / "wti_accuracy.parquet", index=False)
        pd.DataFrame(slot).to_parquet(self.acc / "slot_accuracy.parquet", index=False)

    def standard(self):
        self.write(wti_rows("AK", [11.0] * 90) + wti_rows("MK", [4.0, -4.0] * 45) + wti_rows("TDS", [-12.6] * 87),
                   slot_rows("AK86", [47.0] * 120) + slot_rows("AK01", [30.0] * 8) + slot_rows("MK10", [1.0, -1.0] * 60))

    def run_sc(self, *args):
        with redirect_stdout(io.StringIO()) as buf:
            code = sc.main(["--as-of", AS_OF.isoformat(), *args])
        return code, buf.getvalue()

    def card(self):
        return pd.read_parquet(self.out / f"scorecard_{AS_OF}.parquet")


class PhaseA(Env):
    def test_ac1_missing_column_exits_1(self):
        rows = wti_rows("AK", [1.0] * 30)
        [r.pop("wti_error") for r in rows]
        self.write(rows, slot_rows("AK86", [1.0] * 40))
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(self.run_sc()[0], 1)
        self.assertIn("wti_accuracy: missing required column(s): wti_error", err.getvalue())
        self.assertNotIn("KeyError", err.getvalue())
        self.assertFalse(self.out.exists())

    def test_ac2_dedup_keeps_latest(self):
        self.write(wti_rows("AK", [99.0] * 30, ev="2026-10-01") + wti_rows("AK", [2.0] * 30, ev="2026-10-13"),
                   slot_rows("AK86", [99.0] * 40, ev="2026-10-01") + slot_rows("AK86", [3.0] * 40, ev="2026-10-13"))
        self.run_sc()
        c = self.card().set_index(["key", "window_days"])
        self.assertEqual(c.loc[("AK", 30), "mean_bias"], 2.0)
        self.assertEqual(c.loc[("AK", 30), "n"], 30)
        self.assertEqual(c.loc[("AK86", 7), "mean_bias"], 3.0)

    def test_ac3_window_metrics(self):
        self.write(wti_rows("AK", [10.0, 12.0, -1.0, 14.0] * 30), slot_rows("AK86", [47.2] * 400))
        self.run_sc()
        c = self.card().set_index(["key", "window_days"])
        ak = c.loc[("AK", 90)]
        e = pd.Series([10.0, 12.0, -1.0, 14.0] * 30)[:90]
        self.assertEqual(ak["n"], 90)
        self.assertEqual(c.loc[("AK", 30), "n"], 30)
        self.assertAlmostEqual(ak["mean_bias"], e.mean())
        self.assertAlmostEqual(ak["median_bias"], e.median())
        self.assertAlmostEqual(ak["mae"], e.abs().mean())
        self.assertAlmostEqual(ak["mae_over_abs_bias"], e.abs().mean() / abs(e.mean()))
        self.assertAlmostEqual(c.loc[("AK86", 90), "mean_bias"], 47.2)
        self.assertEqual(set(c.reset_index()["window_days"]), {7, 30, 90})

    def test_ac4_deterministic(self):
        self.standard()
        self.run_sc()
        self.run_sc()
        for ext in (".parquet", ".md"):
            a = (self.out / f"scorecard_{AS_OF}{ext}").read_bytes()
            self.assertEqual(a, (self.out / f"scorecard_{AS_OF}_r2{ext}").read_bytes())


class PhaseB(Env):
    def test_ac5_drift_and_shared_thresholds(self):
        self.write(wti_rows("AK", [6.0, 4.0, 12.0, -2.0] * 23) + wti_rows("TDS", [6.0, 4.0, 12.0, -2.0] * 23),
                   slot_rows("AK86", [16.0, 20.0, 30.0, -6.0] * 30) + slot_rows("TDS12", [16.0, 20.0, 30.0, -6.0] * 30))
        self.run_sc()
        c = self.card().set_index(["key", "window_days"])
        for k in ("AK", "TDS", "AK86", "TDS12"):
            self.assertEqual(c.loc[(k, 30), "flag"], "drift", k)
        t = sc.thresholds()
        self.assertEqual((t["park"], t["entity"], t["min_n"]), (5, 15, 20))
        with mock.patch.dict(os.environ, {"TPCR_DRIFT_PARK_BIAS_MIN": "6"}):
            self.assertEqual(sc.flag({"n": 30, "window_days": 30, "mean_bias": 5.5, "mae": 9, "level": "park"}, sc.thresholds()), "none")
        self.assertNotIn("TDS", Path(sc.__file__).read_text())

    def test_ac6_systematic(self):
        self.standard()
        self.run_sc()
        c = self.card().set_index(["key", "window_days"])
        self.assertEqual(c.loc[("AK", 30), "flag"], "systematic")
        self.assertEqual(c.loc[("AK86", 30), "flag"], "systematic")
        self.assertEqual(c.loc[("MK", 30), "flag"], "none")
        s = json.loads((self.out / "status.json").read_text())
        self.assertEqual([(p["park"], p["direction"], p["since"]) for p in s["systematic_parks"]],
                         [("AK", "high", "2026-10-12"), ("TDS", "low", "2026-10-12")])

    def test_ac7_low_n_never_drift(self):
        self.standard()
        self.run_sc()
        c = self.card().set_index(["key", "window_days"])
        self.assertEqual(c.loc[("AK01", 30), "flag"], "low_n")
        s = json.loads((self.out / "status.json").read_text())
        self.assertNotIn("AK01", [f["key"] for f in s["flags"]])

    def test_ac8_backtest_first_flag_dates(self):
        cross = date(2026, 6, 1)
        errs = [0.5] * 60 + [20.0] * 130
        self.write(wti_rows("AK", list(reversed(errs)), end=date(2026, 10, 1)),
                   slot_rows("AK86", list(reversed([1.0, -1.0] * 120 + [50.0] * 520)), end=date(2026, 10, 1)))
        acc = self.acc
        err = sc.errors(sc.load(acc / "wti_accuracy.parquet", "wti_accuracy"), sc.load(acc / "slot_accuracy.parquet", "slot_accuracy"))
        first = backtest.backtest(err, date(2026, 5, 20), date(2026, 7, 1))
        ak = min(d for (l, k, _), d in first.items() if k == "AK")
        self.assertLessEqual((ak - cross).days, 14)
        self.assertIn(("entity", "AK86", "systematic"), first)
        with redirect_stdout(io.StringIO()) as buf:
            backtest.main(["--start", "2026-06-01", "--end", "2026-06-20"])
        self.assertRegex(buf.getvalue(), r"park\tAK\t\w+\t2026-06-\d\d")


class PhaseC(Env):
    def test_ac9_stale_and_missing(self):
        self.write(wti_rows("AK", [1.0] * 30, end=AS_OF - timedelta(days=3)), slot_rows("AK86", [1.0] * 40))
        self.assertEqual(self.run_sc()[0], 75)
        s = json.loads((self.out / "status.json").read_text())
        self.assertEqual((s["ok"], s["reason"]), (False, "inputs_stale"))
        self.assertEqual(list(self.out.glob("scorecard_*")), [])
        (self.acc / "slot_accuracy.parquet").unlink()
        self.assertEqual(self.run_sc()[0], 75)

    def test_ac10_atomic_status(self):
        self.standard()
        with mock.patch("os.replace", wraps=os.replace) as rep:
            self.run_sc()
        rep.assert_called_once()
        self.assertEqual(Path(rep.call_args[0][1]).name, "status.json")
        self.assertFalse((self.out / "status.json.tmp").exists())
        s = json.loads((self.out / "status.json").read_text())
        self.assertTrue((self.out / s["scorecard"]).exists())

    def test_ac11_chained_after_eval_success(self):
        sh = str(SC / "run_after_accuracy.sh")
        env = {**os.environ, "TPCR_AS_OF": AS_OF.isoformat()}
        self.assertEqual(subprocess.run([sh, "false"], env=env).returncode, 1)
        self.assertFalse((self.out / "status.json").exists())
        r = subprocess.run([sh, "true"], env=env, capture_output=True)
        self.assertEqual(r.returncode, 75)
        self.assertTrue((self.out / "status.json").exists())


TWEETED = {"2026-10-01": (33, 15), "2026-10-02": (36, 21), "2026-10-03": (35, 25), "2026-10-04": (32, 25),
           "2026-10-05": (32, 24), "2026-10-06": (32, 20), "2026-10-07": (31, 13)}


class PhaseD(Env):
    def week(self, parks=("MK", "EP", "HS", "AK"), skip=None):
        rows = []
        for p in parks:
            for d, (f, a) in TWEETED.items():
                if (p, d) != skip:
                    rows.append({"park_code": p, "park_date": d, "forecast_wti": float(f), "actual_wti": float(a),
                                 "wti_error": float(f - a), "evaluation_date": "2026-10-08"})
        rows.append({**rows[0], "forecast_wti": 99.0, "wti_error": 99.0, "evaluation_date": "2026-10-01"})
        self.write(rows, slot_rows("AK86", [1.0] * 40))
        with redirect_stdout(io.StringIO()):
            weekly.main(["--start", "2026-10-01", "--end", "2026-10-07"])
        return json.loads((self.out / "weekly_2026-10-01_2026-10-07.json").read_text())

    def test_ac12_reproduces_tweeted_pairs(self):
        w = self.week()
        ak = w["parks"]["AK"]
        self.assertEqual({d["date"]: (d["forecast"], d["actual"]) for d in ak["days"]}, {k: (float(f), float(a)) for k, (f, a) in TWEETED.items()})
        self.assertEqual(ak["n_days"], 7)
        self.assertEqual(ak["biggest"]["date"], "2026-10-01")
        self.assertEqual(ak["closest"]["date"], "2026-10-04")
        self.assertAlmostEqual(ak["avg_abs_miss"], 12.57, places=2)
        self.assertTrue(w["complete"])

    def test_ac13_incomplete_week(self):
        w = self.week(skip=("EP", "2026-10-03"))
        self.assertFalse(w["complete"])
        self.assertEqual(w["missing_dates"], {"EP": ["2026-10-03"]})

    def test_ac14_grade_park_table(self):
        cases = [(20 - 19, "hit"), (28 - 17, "miss"), (31 - 13, "miss"), (7, "none"),
                 (5, "hit"), (5.01, "none"), (10, "none"), (10.01, "miss")]
        for e, g in cases:
            self.assertEqual(sc.grade_park(e), g, e)
            self.assertEqual(sc.grade_park(-e), g, -e)
        ak = self.week()["parks"]["AK"]
        self.assertEqual((ak["hits"], ak["misses"], ak["no_mark"]), (0, 4, 3))

    def test_ac14a_mae_goal(self):
        self.write(wti_rows("AK", [8.0, -8.0] * 15) + wti_rows("MK", [8.01, -8.01] * 15), slot_rows("AK86", [1.0] * 40))
        self.run_sc()
        c = self.card()
        p = c[(c["level"] == "park") & (c["window_days"] == 30)].set_index("key")
        self.assertIs(bool(p.loc["AK", "goal_met"]), True)
        self.assertIs(bool(p.loc["MK", "goal_met"]), False)
        self.assertTrue((p["mae_goal_min"] == 8).all())
        e = c[c["level"] == "entity"]
        self.assertTrue(e["goal_met"].isna().all() and e["mae_goal_min"].isna().all())

    def test_ac14b_no_ride_grades(self):
        self.standard()
        self.run_sc()
        self.assertFalse({"grade", "hits", "misses", "no_mark"} & set(self.card().columns))
        w = self.week()
        self.assertEqual(set(w["parks"]), {"MK", "EP", "HS", "AK"})
        with self.assertRaises(sc.NotCalibrated):
            sc.grade_ride(15.0, 105.0, 0.3)
        for f in ("scorecard.py", "weekly.py", "backtest.py"):
            self.assertNotIn("grade_ride(", (SC / f).read_text().replace("def grade_ride(", ""))


SAFETY_RE = re.compile(r"\b(ssh|scp|requests|tweepy|smtplib|socket|urllib|http)\b")
DELETE_RE = re.compile(r"unlink|remove|rmtree")


class PhaseE(Env):
    def test_ac15_writes_only_under_scorecard_dir(self):
        self.standard()
        snap = lambda: {p: p.stat().st_mtime_ns for p in self.tmp.rglob("*") if self.out not in p.parents and p != self.out}
        before = snap()
        writes = []
        real = {"open": open, "write_text": Path.write_text, "write_bytes": Path.write_bytes, "replace": os.replace, "rename": os.rename}

        def spy_open(f, mode="r", *a, **k):
            if any(c in mode for c in "wax+"):
                writes.append(Path(f))
            return real["open"](f, mode, *a, **k)

        def spy_path(name):
            def f(self_, *a, **k):
                writes.append(Path(self_))
                return real[name](self_, *a, **k)
            return f

        def spy_move(name):
            def f(src, dst, *a, **k):
                writes.extend([Path(src), Path(dst)])
                return real[name](src, dst, *a, **k)
            return f

        with mock.patch("builtins.open", spy_open), mock.patch.object(Path, "write_text", spy_path("write_text")), \
                mock.patch.object(Path, "write_bytes", spy_path("write_bytes")), mock.patch("os.replace", spy_move("replace")), \
                mock.patch("os.rename", spy_move("rename")):
            self.run_sc()
        self.assertTrue(writes)
        self.assertEqual([w for w in writes if self.out not in w.parents], [])
        self.assertEqual(before, snap())
        for f in SC.glob("*.py"):
            self.assertIsNone(SAFETY_RE.search(f.read_text()), f)

    def test_ac16_dry_run_writes_nothing(self):
        self.standard()
        code, out = self.run_sc("--dry-run")
        self.assertEqual(code, 3)
        self.assertIn("# TPCR accuracy scorecard 2026-10-12", out)
        self.assertIn('"reason": "systematic"', out)
        self.assertFalse(self.out.exists())


GOLDEN = """Subject: TPCR drift: 2 flag(s) as of 2026-10-12
To: fred@hazeydata.ai

AK86 entity systematic value=47.0 threshold=15.0 window=30
AK park systematic value=11.0 threshold=5.0 window=30

scorecard: scorecard_2026-10-12.parquet
"""


class PhaseF(Env):
    def status(self, **kw):
        s = {"as_of": "2026-10-12", "ok": True, "scorecard": "scorecard_2026-10-12.parquet", "systematic_parks": [],
             "flags": [{"key": "AK", "level": "park", "reason": "systematic", "value": 11.0, "threshold": 5.0, "window_days": 30},
                       {"key": "AK86", "level": "entity", "reason": "systematic", "value": 47.0, "threshold": 15.0, "window_days": 30}]}
        s.update(kw)
        p = self.tmp / "status.json"
        p.write_text(json.dumps(s))
        return p

    def alert(self, p, *args):
        with redirect_stdout(io.StringIO()) as buf:
            code = drift_alert.main(["--status", str(p), *args])
        return code, buf.getvalue()

    def test_ac17_only_when_flagged(self):
        self.assertEqual(self.alert(self.status(flags=[]))[1], "no_alert\n")
        self.assertFalse((self.out / "alerts").exists())
        stale = [{"key": "inputs", "level": "input", "reason": "inputs_stale", "value": "2026-10-08", "threshold": "2026-10-10", "window_days": 0}]
        self.alert(self.status(ok=False, reason="inputs_stale", flags=stale), "--dry-run")
        self.assertTrue((self.out / "alerts" / "alert_2026-10-12.eml.txt").exists())

    def test_ac18_deterministic_content(self):
        tds = {"key": "TDS", "level": "park", "reason": "drift", "value": 6.0, "threshold": 5.0, "window_days": 30}
        flags = [tds] + json.loads(self.status().read_text())["flags"]
        self.assertEqual([f["key"] for f in drift_alert.alert_flags({"flags": flags})], ["AK86", "AK", "TDS"])
        p = self.status()
        self.alert(p, "--dry-run")
        self.alert(p, "--dry-run")
        a = (self.out / "alerts" / "alert_2026-10-12.eml.txt").read_text()
        self.assertEqual(a, GOLDEN)
        self.assertEqual(a, (self.out / "alerts" / "alert_2026-10-12_r2.eml.txt").read_text())

    def test_ac19_no_send_without_approval(self):
        with mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as ssl:
            code, out = self.alert(self.status())
        self.assertEqual(code, 0)
        self.assertIn("awaiting_approval", out)
        self.assertEqual(smtp.call_count + ssl.call_count, 0)

    def test_ac20_dry_run_no_mail_connection(self):
        with mock.patch("socket.socket") as sock:
            self.alert(self.status(), "--dry-run")
        sock.assert_not_called()
        self.assertIsNone(SAFETY_RE.search((SC / "drift_alert.py").read_text()))
        self.assertEqual((self.out / "alerts" / "alert_2026-10-12.eml.txt").read_text(), GOLDEN)

    def test_ac21_recipient_default_and_no_secrets(self):
        self.assertEqual(drift_alert.recipient(), "fred@hazeydata.ai")
        with mock.patch.dict(os.environ, {"TPCR_DRIFT_ALERT_TO": "override-recipient"}):
            self.assertEqual(drift_alert.recipient(), "override-recipient")
        src = (SC / "drift_alert.py").read_text()
        self.assertNotRegex(src, r"(?i)password|secret|token")


def systematic(*parks, as_of="2026-10-12", ok=True):
    return {"as_of": as_of, "ok": ok, "systematic_parks": [{"park": p, "direction": d, "window_days": 30, "mean_bias": 1.0, "since": as_of} for p, d in parks]}


PARKS = [{"park_code": "AK", "park_name": "Animal Kingdom"}, {"park_code": "HS", "park_name": "Hollywood Studios"},
         {"park_code": "MK", "park_name": "Magic Kingdom"}, {"park_code": "EP", "park_name": "EPCOT"}]

FORECAST = {"target_date": "2026-10-13", "parks": [
    {"park_code": "MK", "park_name": "Magic Kingdom", "wti": 38.2}, {"park_code": "EP", "park_name": "EPCOT", "wti": 12.4},
    {"park_code": "HS", "park_name": "Hollywood Studios", "wti": 27.0}, {"park_code": "AK", "park_name": "Animal Kingdom", "wti": 19.6}]}
MAIN_FORECAST_TEXT = ("Walt Disney World Crowd Forecast \u2014 2026-10-13\n\n\U0001f7e2 EPCOT: 12 min avg wait\n"
                      "\U0001f7e1 Animal Kingdom: 20 min avg wait\n\U0001f7e0 Hollywood Studios: 27 min avg wait\n"
                      "\U0001f534 Magic Kingdom: 38 min avg wait\n\nLower WTI = shorter waits. Plan your day accordingly!\n"
                      "#WDW #DisneyWorld #WaitTimes")


class PhaseG(Env):
    def notes(self, status, today=AS_OF, variant=None):
        self.out.mkdir(exist_ok=True)
        (self.out / "status.json").write_text(json.dumps(status) if status else "")
        env = {"TPCR_TWEET_NOTE_VARIANT": variant} if variant else {}
        with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as buf:
            lines = tweet_note.note_lines(PARKS, today)
        return lines, buf.getvalue()

    def test_ac22_retention_no_prune(self):
        for f in list(SC.glob("*.py")) + [ROOT / "operations" / "scripts" / "tweet_note.py"]:
            self.assertIsNone(DELETE_RE.search(f.read_text()), f)
        self.standard()
        self.run_sc()
        first = (self.out / f"scorecard_{AS_OF}.md").read_bytes()
        self.run_sc()
        self.assertEqual((self.out / f"scorecard_{AS_OF}.md").read_bytes(), first)
        self.assertTrue((self.out / f"scorecard_{AS_OF}_r2.parquet").exists())

    def test_ac23_note_per_flagged_park(self):
        self.assertEqual(self.notes(systematic())[0], [])
        self.assertEqual(self.notes(systematic(("AK", "high")))[0],
                         ["Note: our Animal Kingdom forecasts have been running high lately. We're working on it."])
        lines, _ = self.notes(systematic(("AK", "high"), ("AK", "high"), ("TDS", "low")))
        self.assertEqual(len(lines), 1)
        for st in (None, systematic(("AK", "high"), as_of="2026-10-09"), systematic(("AK", "high"), ok=False)):
            self.assertEqual(self.notes(st), ([], "status_unavailable\n"))

    def test_ac24_variant_config_and_length(self):
        lines, log = self.notes(systematic(("AK", "high")), variant="bogus")
        self.assertIn("WARNING", log)
        self.assertIn("We're working on it.", lines[0])
        self.assertNotIn("working on it", Path(tweet_note.__file__).read_text())
        worst = systematic(*[(p["park_code"], "high") for p in PARKS])
        lines = self.notes(worst, variant="campy")[0]
        text = tweet_note.cap(lines)
        self.assertGreater(len("\n".join(lines)), 280)
        self.assertLessEqual(len(text), 280)
        self.assertEqual(text, "\n".join(lines[:len(text.split("\n"))]))
        self.assertEqual(tweet_note.cap(lines), text)
        self.assertEqual(tweet_note.cap(["x" * 140, "y" * 139]), "x" * 140 + "\n" + "y" * 139)
        self.assertEqual(tweet_note.cap(["x" * 140, "y" * 140, "z"]), "x" * 140)
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(tweet_note.cap(["x" * 281, "y"]))
        self.assertIn("note_too_long", err.getvalue())

    def run_forecast(self, status):
        self.out.mkdir(exist_ok=True)
        (self.out / "status.json").write_text(json.dumps(status))
        state = Path(tempfile.mkdtemp(dir=self.tmp)) / "tweet_state.json"
        post = mock.Mock(return_value="111")
        creds = {k: "x" for k in ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")}
        with mock.patch.multiple(wpt, STATE_FILE=state, load_env=mock.DEFAULT, enforce=mock.DEFAULT, post_discord=mock.DEFAULT,
                                 render_video_on_wilma=mock.Mock(return_value=None), load_content=mock.Mock(return_value=FORECAST),
                                 post_tweet=post), \
                mock.patch.dict(os.environ, creds), mock.patch.object(tweet_note, "date", type("D", (date,), {"today": classmethod(lambda c: AS_OF)})), \
                redirect_stdout(io.StringIO()):
            wpt.main()
        return post, state

    def test_ac23_forecast_unchanged_and_reply_drafted(self):
        self.assertEqual(wpt.format_tweet(FORECAST), MAIN_FORECAST_TEXT)
        post, state = self.run_forecast(systematic(("AK", "high")))
        post.assert_called_once_with(MAIN_FORECAST_TEXT, media_id=None)
        draft = json.loads(state.read_text())["note_drafts"]["2026-10-13"]
        text = "Note: our Animal Kingdom forecasts have been running high lately. We're working on it."
        self.assertEqual(draft, {"text": text, "in_reply_to": "111", "status": "draft",
                                 "sha256": hashlib.sha256((text + "111").encode()).hexdigest()})
        _, state = self.run_forecast(systematic())
        self.assertNotIn("note_drafts", json.loads(state.read_text()))

    def test_ac24_approve_posts_once(self):
        post, state = self.run_forecast(systematic(("AK", "high")))
        reply = mock.Mock(return_value="222")
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(tweet_note.approve(state, "2026-10-13", reply), "222")
            self.assertIsNone(tweet_note.approve(state, "2026-10-13", reply))
            self.assertIsNone(tweet_note.approve(state, "2026-10-14", reply))
        reply.assert_called_once_with(self.draft(state)["text"], reply_to="111")
        self.assertEqual(self.draft(state)["status"], "posted")
        state = self.fresh()
        with mock.patch("os.replace", wraps=os.replace) as rep, mock.patch("sys.stderr", new=io.StringIO()):
            tweet_note.approve(state, "2026-10-13", reply)
        self.assertEqual(rep.call_args_list[-1].args, (Path(f"{state}.tmp"), state))
        self.assertFalse(Path(f"{state}.tmp").exists())

    def draft(self, state, day="2026-10-13"):
        return json.loads(Path(state).read_text())["note_drafts"][day]

    def fresh(self, text="n", reply_to="111"):
        state = Path(tempfile.mkdtemp(dir=self.tmp)) / "tweet_state.json"
        tweet_note.save_draft(state, "2026-10-13", reply_to, text)
        return state

    def refused(self, state, post):
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(tweet_note.approve(state, "2026-10-13", post))
        return err.getvalue()

    def test_ac24_save_state_during_approve_posts_once(self):
        state = self.fresh()
        started = threading.Event()

        def slow_post(text, reply_to=None):
            started.set()
            time.sleep(0.3)
            return "222"

        post = mock.Mock(side_effect=slow_post)
        with mock.patch.object(wpt, "STATE_FILE", state), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", new=io.StringIO()):
            t = threading.Thread(target=tweet_note.approve, args=(state, "2026-10-13", post))
            t.start()
            started.wait()
            wpt.save_state("111", "2026-10-13")
            t.join()
            self.assertEqual(json.loads(state.read_text())["predicted"]["2026-10-13"]["tweet_id"], "111")
            self.assertEqual(self.draft(state)["status"], "posted")
            tweet_note.approve(state, "2026-10-13", post)
            with mock.patch("os.replace", wraps=os.replace) as rep:
                wpt.save_state("111", "2026-10-13")
            rep.assert_called_once_with(Path(f"{state}.tmp"), state)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.draft(state)["status"], "posted")

    def test_ac24_lock_on_separate_lock_file(self):
        state = self.fresh()
        real = tweet_note.fcntl.flock
        calls = []

        def spy(f, op):
            fd = f if isinstance(f, int) else f.fileno()
            path = f.name if hasattr(f, "name") else os.readlink(f"/proc/self/fd/{fd}")
            if op == tweet_note.fcntl.LOCK_EX:
                calls.append((str(path), os.fstat(fd).st_ino))
            return real(f, op)

        with mock.patch.object(tweet_note.fcntl, "flock", spy), redirect_stdout(io.StringIO()):
            tweet_note.approve(state, "2026-10-13", mock.Mock(return_value="222"))
        lock = Path(str(state) + ".lock")
        self.assertTrue(calls)
        for path, ino in calls:
            self.assertEqual(path, str(lock))
            self.assertEqual(ino, lock.stat().st_ino)

    def test_ac24_concurrent_approve_posts_once(self):
        state = self.fresh()
        barrier = threading.Barrier(4)
        post = mock.Mock(return_value="222")

        def go():
            barrier.wait()
            tweet_note.approve(state, "2026-10-13", post)

        real_load = tweet_note._load
        slow = lambda f: (real_load(f), time.sleep(0.2))[0]
        with mock.patch("sys.stderr", new=io.StringIO()), mock.patch.object(tweet_note, "_load", slow):
            ts = [threading.Thread(target=go) for _ in range(4)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.draft(state)["status"], "posted")

    def test_ac24_tampered_or_missing_reply_refused(self):
        post = mock.Mock(return_value="222")
        state = self.fresh()
        st = json.loads(state.read_text())
        st["note_drafts"]["2026-10-13"]["text"] = "tampered"
        state.write_text(json.dumps(st))
        self.assertIn("integrity", self.refused(state, post))
        for bad in (None, "", "drop"):
            state = self.fresh()
            st = json.loads(state.read_text())
            d = st["note_drafts"]["2026-10-13"]
            d.pop("in_reply_to") if bad == "drop" else d.update(in_reply_to=bad)
            state.write_text(json.dumps(st))
            self.assertIn("standalone", self.refused(state, post))
        for bad in (None, ""):
            with mock.patch("sys.stderr", new=io.StringIO()):
                self.assertIsNone(tweet_note.save_draft(self.tmp / "x.json", "2026-10-13", bad, "n"))
        self.assertFalse((self.tmp / "x.json").exists())
        post.assert_not_called()

    def test_ac24_crash_or_unknown_needs_human(self):
        state = self.fresh()
        with self.assertRaises(RuntimeError):
            tweet_note.approve(state, "2026-10-13", mock.Mock(side_effect=RuntimeError("crash")))
        self.assertEqual(self.draft(state)["status"], "posting")
        post = mock.Mock(return_value="222")
        self.assertIn("check X manually", self.refused(state, post))
        state = self.fresh()
        none = mock.Mock(return_value=None)
        self.assertIn("check X manually", self.refused(state, none))
        self.assertEqual(self.draft(state)["status"], "unknown")
        self.assertIn("check X manually", self.refused(state, post))
        none.assert_called_once()
        post.assert_not_called()

    def test_ac25_final_wording(self):
        for v, tail in (("standard", "We're working on it."), ("campy", "The bot team is working on it.")):
            for d in ("high", "low"):
                line = self.notes(systematic(("HS", d)), variant=v)[0][0]
                self.assertEqual(line, f"Note: our Hollywood Studios forecasts have been running {d} lately. {tail}")
                self.assertIsNone(re.search(r"\d|WTI|accura", line))


class WindowOverride(Env):
    def setUp(self):
        super().setUp()
        rows = [{"level": "park", "key": "MK", "park_date": d, "error": 1.0} for d in days(120)]
        rows.append({"level": "park", "key": "MK", "park_date": AS_OF + timedelta(days=1), "error": 1.0})
        self.err = pd.DataFrame(rows)

    def dates(self, w):
        s = sc.window_stats(self.err, AS_OF, w)
        return int(s["n"].iloc[0])

    def test_window_lengths(self):
        self.assertEqual(self.dates(90), 90)
        self.assertEqual(self.dates(91), 91)

    def test_boundary_days(self):
        def has(day, w):
            return len(sc.window_stats(self.err[self.err["park_date"] == day], AS_OF, w)) == 1
        self.assertTrue(has(AS_OF - timedelta(days=89), 90))
        self.assertFalse(has(AS_OF - timedelta(days=90), 90))
        self.assertTrue(has(AS_OF - timedelta(days=90), 91))
        self.assertFalse(has(AS_OF + timedelta(days=1), 91))

    def test_scorecard_windows(self):
        df = sc.scorecard(self.err, AS_OF, windows=(91,))
        self.assertEqual(list(df["window_days"]), [91])
        self.assertEqual(list(df["n"]), [91])
        df = sc.scorecard(self.err, AS_OF)
        self.assertEqual(list(df["window_days"]), [7, 30, 90])
        self.assertEqual(list(df["n"]), [7, 30, 90])

    def test_windows_is_keyword_only_and_not_exposed(self):
        p = inspect.signature(sc.scorecard).parameters["windows"]
        self.assertEqual(p.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(p.default, sc.WINDOWS)
        self.assertEqual(sc.WINDOWS, (7, 30, 90))
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            sc.main(["--as-of", AS_OF.isoformat(), "--window-days", "91"])


class WindowsTestOnly(Env):
    SRC = SC / "scorecard.py"

    def test_no_windows_kwarg_in_run_or_main(self):
        tree = ast.parse(self.SRC.read_text())
        seen = {}
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("run", "main")):
            for c in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
                if isinstance(c.func, ast.Name) and c.func.id == "scorecard":
                    seen.setdefault(fn.name, []).append(c)
                    self.assertNotIn("windows", [k.arg for k in c.keywords])
                    self.assertTrue(all(k.arg for k in c.keywords))
                    self.assertLessEqual(len(c.args), 2)
        self.assertTrue(seen.get("run"))

    def test_env_vars_do_not_change_windows(self):
        env = {"TPCR_SCORECARD_WINDOWS": "1,2,3", "SCORECARD_WINDOWS": "1,2,3", "TPCR_WINDOWS": "1,2,3"}
        self.addCleanup(importlib.reload, sc)
        with mock.patch.dict(os.environ, env):
            importlib.reload(sc)
            self.assertEqual(sc.WINDOWS, (7, 30, 90))

    def test_run_ignores_env_and_config_files(self):
        self.standard()
        os.environ["TPCR_SCORECARD_WINDOWS"] = "1,2,3"
        opened = []
        real_open, real_read, real_popen = open, Path.read_text, Path.open

        def spy_open(f, *a, **k):
            opened.append(str(f))
            return real_open(f, *a, **k)

        def spy_read(self_, *a, **k):
            opened.append(str(self_))
            return real_read(self_, *a, **k)

        def spy_path_open(self_, *a, **k):
            opened.append(str(self_))
            return real_popen(self_, *a, **k)

        with mock.patch.object(sc, "scorecard", wraps=sc.scorecard) as spy, \
                mock.patch("builtins.open", spy_open), mock.patch.object(Path, "read_text", spy_read), \
                mock.patch.object(Path, "open", spy_path_open), redirect_stdout(io.StringIO()):
            sc.run(AS_OF, self.acc, self.out, False)
        self.assertTrue(spy.called)
        for call in spy.call_args_list:
            self.assertEqual(call.kwargs.get("windows", (7, 30, 90)), (7, 30, 90))
        self.assertEqual(set(self.card()["window_days"]), {7, 30, 90})
        self.assertEqual([p for p in opened if "config" in p.lower() and p.endswith(".json")], [])

    def test_source_has_no_config_reference(self):
        src = self.SRC.read_text().lower()
        self.assertNotIn("scorecard.json", src)
        self.assertNotIn("config", src)


if __name__ == "__main__":
    unittest.main()
