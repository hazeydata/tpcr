import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import duckdb

from pipeline.config import load_config
from pipeline.steps import s14_content

ALL_PARKS = ["MK", "EP", "HS", "AK", "DL", "CA", "UF", "IA", "EU", "UH", "TDL", "TDS"]
TARGET = "2026-10-22"
WDW = ["MK", "EP", "HS", "AK"]
REPO_ROOT = Path(__file__).resolve().parents[2]


class _Log:
    def __init__(self):
        self.messages = []

    def info(self, msg):
        self.messages.append(("info", msg))

    def warning(self, msg):
        self.messages.append(("warning", msg))

    def error(self, msg):
        self.messages.append(("error", msg))


def _write_wti(wti_dir: Path, parks, source="forecast", extra_rows=()):
    wti_dir.mkdir(parents=True, exist_ok=True)
    rows = [(p, TARGET, source, 20.0) for p in parks] + list(extra_rows)
    con = duckdb.connect()
    con.execute("CREATE TABLE t (park_code VARCHAR, park_date VARCHAR, source VARCHAR, wti DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", rows)
    con.execute(f"COPY t TO '{wti_dir / 'wti.parquet'}' (FORMAT PARQUET)")
    con.close()


class S14ContentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.cfg = load_config(output_base=self.tmp_path)

    def test_config_has_twelve_parks(self):
        self.assertEqual(s14_content._active_parks(self.cfg), ALL_PARKS)

    def test_ignore_parks_excluded(self):
        self.cfg.ignore_parks = ["UH"]
        self.assertNotIn("UH", s14_content._active_parks(self.cfg))

    def test_extraction_returns_all_twelve(self):
        _write_wti(self.cfg.wti_dir, ALL_PARKS, extra_rows=[("BB", TARGET, "forecast", 5.0)])
        data = s14_content._extract_wti_data(self.cfg, _Log(), TARGET, "forecast")
        self.assertEqual(sorted(data), sorted(ALL_PARKS))

    def test_extraction_filters_source(self):
        _write_wti(self.cfg.wti_dir, ALL_PARKS, source="historical")
        data = s14_content._extract_wti_data(self.cfg, _Log(), TARGET, "forecast")
        self.assertIsNone(data)

    def test_sql_in_list_quotes(self):
        self.assertEqual(s14_content._sql_in_list(["MK", "O'X"]), "'MK', 'O''X'")

    def test_completeness_passes_with_all_parks(self):
        wti = {p: 20.0 for p in ALL_PARKS}
        self.assertEqual(s14_content._check_completeness(wti, self.cfg), [])

    def test_completeness_flags_missing_non_wdw_park(self):
        wti = {p: 20.0 for p in ALL_PARKS if p != "TDS"}
        failures = s14_content._check_completeness(wti, self.cfg)
        self.assertEqual(len(failures), 1)
        self.assertIn("TDS", failures[0])

    def test_completeness_flags_none_value(self):
        wti = {p: 20.0 for p in ALL_PARKS}
        wti["IA"] = None
        failures = s14_content._check_completeness(wti, self.cfg)
        self.assertIn("IA", failures[0])

    def test_peer_check_is_per_resort(self):
        # Tokyo parks are far above WDW, but only compared with each other.
        wti = {"MK": 20.0, "EP": 21.0, "HS": 19.0, "AK": 20.0,
               "DL": 40.0, "CA": 42.0, "UH": 5.0, "TDL": 60.0, "TDS": 62.0,
               "UF": 30.0, "IA": 31.0, "EU": 29.0}
        self.assertEqual(s14_content._check_peer_outlier(wti), [])

    def test_peer_check_flags_outlier_within_resort(self):
        wti = {"UF": 20.0, "IA": 21.0, "EU": 60.0, "MK": 20.0, "EP": 20.0}
        failures = s14_content._check_peer_outlier(wti)
        self.assertEqual(len(failures), 1)
        self.assertIn("EU", failures[0])
        self.assertIn("UOR", failures[0])

    def test_peer_check_skips_park_without_peers(self):
        # UH has no resort peers; a lone DL has none present either.
        self.assertEqual(s14_content._check_peer_outlier({"UH": 1.0, "DL": 70.0}), [])

    def test_output_has_twelve_parks_in_order(self):
        # Insert in reverse so ordering comes from cfg.parks, not input order.
        wti = {p: 20.0 + i for i, p in enumerate(reversed(ALL_PARKS))}
        out = s14_content._create_content_json("predicted", TARGET, wti, "ready", [], "t", self.cfg)
        self.assertEqual([p["park_code"] for p in out["parks"]], ALL_PARKS)
        self.assertEqual(set(out), {"type", "status", "held_reasons", "warnings", "target_date",
                                    "generated_at", "generated_by", "property", "parks"})
        self.assertEqual(out["warnings"], [])
        self.assertEqual(set(out["parks"][0]), {"park_code", "park_name", "wti"})

    def _gate(self, missing):
        parks = [p for p in ALL_PARKS if p != missing]
        _write_wti(self.cfg.wti_dir, parks)
        wti = {p: 20.0 for p in parks}
        return s14_content._run_quality_gate(
            wti, self.cfg, TARGET, is_predicted=False, run_date=TARGET)

    def test_missing_tokyo_park_is_ready_with_warning(self):
        passed, failures, warnings = self._gate("TDS")
        self.assertTrue(passed)
        self.assertEqual(failures, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("TDS", warnings[0])
        status = "ready" if passed else "held"
        out = s14_content._create_content_json(
            "observed", TARGET, {"MK": 1.0}, status, failures, "t", self.cfg, warnings)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["held_reasons"], [])
        self.assertEqual(out["warnings"], warnings)

    def test_missing_wdw_park_is_held(self):
        passed, failures, warnings = self._gate("EP")
        self.assertFalse(passed)
        self.assertEqual(len(failures), 1)
        self.assertIn("EP", failures[0])
        self.assertEqual(warnings, [])

    def test_park_names_cover_all_and_fallback(self):
        for code in ALL_PARKS:
            self.assertIn(code, s14_content.PARK_NAMES)
        self.cfg.parks = ["MK", "ZZ"]
        out = s14_content._create_content_json(
            "observed", TARGET, {"MK": 1.0, "ZZ": 2.0}, "ready", [], "t", self.cfg)
        self.assertEqual(out["parks"][1]["park_name"], "ZZ")


def _stub_modules():
    """Fake third-party/network modules so tweet scripts import without side effects."""
    stubs = {}
    for name in ("tweepy", "requests", "requests_oauthlib", "tweet_guard"):
        stubs[name] = types.ModuleType(name)
    stubs["requests_oauthlib"].OAuth1 = object
    stubs["tweet_guard"].enforce = lambda *a, **k: None
    stubs["tweet_guard"].preflight_legacy = lambda *a, **k: None
    return stubs


def _import_script(rel_path, mod_name):
    path = REPO_ROOT / rel_path
    with mock.patch.dict(sys.modules, _stub_modules()):
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _content_json(content_type, target_date, status="ready"):
    return {
        "type": content_type,
        "status": status,
        "held_reasons": [],
        "warnings": [],
        "target_date": target_date,
        "generated_at": "2026-10-21T06:00:00",
        "generated_by": "t",
        "property": "all",
        "parks": [{"park_code": p, "park_name": s14_content.PARK_NAMES[p], "wti": 20.0}
                  for p in ALL_PARKS],
    }


class TweetScriptWdwOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wti_pred = _import_script("operations/scripts/wti_predicted_tweet.py", "t_wti_predicted")
        cls.wti_obs = _import_script("operations/scripts/wti_observed_tweet.py", "t_wti_observed")
        cls.post_pred = _import_script("scripts/post_predicted_tweet.py", "t_post_predicted")
        cls.post_obs = _import_script("scripts/post_observed_tweet.py", "t_post_observed")
        cls.post_standalone = _import_script("scripts/post_observed_standalone.py", "t_post_standalone")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _write(self, directory, content_type, target_date, status="ready"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{content_type}_{target_date}.json").write_text(
            json.dumps(_content_json(content_type, target_date, status)))

    def _codes(self, parks):
        return [p["park_code"] for p in parks]

    def test_wdw_park_codes_constants(self):
        for mod in (self.wti_pred, self.wti_obs, self.post_pred, self.post_obs, self.post_standalone):
            self.assertEqual(set(mod.WDW_PARK_CODES), set(WDW))

    def test_filter_helpers(self):
        content = _content_json("predicted", TARGET)
        for mod in (self.wti_pred, self.wti_obs):
            out = mod._wdw_only(content)
            self.assertEqual(self._codes(out["parks"]), WDW)
        self.assertEqual(len(content["parks"]), 12)
        for mod in (self.post_pred, self.post_obs, self.post_standalone):
            self.assertEqual(self._codes(mod._wdw_parks(content)), WDW)

    def _check_wilma_loader(self, mod, content_type, days):
        d = (date.today() + timedelta(days=days)).isoformat()
        content_dir = self.tmp_path / content_type
        self._write(content_dir, content_type, d)

        def fake_read(path):
            p = Path(path)
            return p.read_text() if p.exists() else None

        with mock.patch.object(mod, "CONTENT_DIR", str(content_dir)), \
                mock.patch.object(mod, "ssh_read_file", fake_read):
            data = mod.load_content()
        self.assertIsNotNone(data)
        self.assertEqual(data["status"], "ready")
        self.assertEqual(self._codes(data["parks"]), WDW)
        text = mod.format_tweet(data)
        for code in ALL_PARKS:
            if code in WDW:
                self.assertIn(s14_content.PARK_NAMES[code], text)
            else:
                self.assertNotIn(s14_content.PARK_NAMES[code], text)

    def test_wti_predicted_loader_emits_only_wdw(self):
        self._check_wilma_loader(self.wti_pred, "predicted", 1)

    def test_wti_observed_loader_emits_only_wdw(self):
        self._check_wilma_loader(self.wti_obs, "observed", -1)

    def _check_remotion_data(self, mod, content_type):
        remotion_dir = self.tmp_path / f"remotion_{content_type}"
        remotion_dir.mkdir()
        content_dir = self.tmp_path / f"content_{content_type}"
        self._write(content_dir, content_type, TARGET)
        with mock.patch.object(mod, "CONTENT_DIR", content_dir), \
                mock.patch.object(mod, "REMOTION_DIR", remotion_dir):
            content = mod.load_content_json(TARGET)
            self.assertIsNotNone(content)
            self.assertTrue(mod.update_remotion_data(content))
        emitted = json.loads((remotion_dir / "public" / "daily-wti-data.json").read_text())
        self.assertEqual([p["parkCode"] for p in emitted["parks"]], WDW)

    def test_post_predicted_emits_only_wdw(self):
        self._check_remotion_data(self.post_pred, "predicted")

    def test_post_observed_emits_only_wdw(self):
        self._check_remotion_data(self.post_obs, "observed")

    def test_post_standalone_reports_only_wdw(self):
        mod = self.post_standalone
        content_dir = self.tmp_path / "content"
        video_dir = self.tmp_path / "video"
        video_dir.mkdir()
        self._write(content_dir, "observed", "2026-03-25")
        (video_dir / "observed-wti-2026-03-25.mp4").write_bytes(b"")
        with mock.patch.object(mod, "CONTENT_DIR", content_dir), \
                mock.patch.object(mod, "VIDEO_PATH", video_dir), \
                mock.patch.object(mod, "post_standalone_tweet", return_value="1"), \
                self.assertLogs(mod.logger, level="INFO") as logs:
            mod.main()
        names = [m.split(":", 2)[2].strip().rsplit(":", 1)[0] for m in logs.output
                 if m.endswith(": 20.0")]
        self.assertEqual(names, [s14_content.PARK_NAMES[c] for c in WDW])


class S14RunWarningsVsHoldTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.cfg = load_config(output_base=self.tmp_path)
        now = datetime.now()
        self.tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        self.yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    def _run(self, overrides):
        rows = []
        for d, src in ((self.tomorrow, "forecast"), (self.yesterday, "historical")):
            for p in ALL_PARKS:
                rows.append((p, d, src, overrides.get(p, 20.0)))
        _write_wti(self.cfg.wti_dir, [], extra_rows=rows)
        with mock.patch.object(s14_content, "_post_discord_warning") as discord:
            s14_content.run(self.cfg, _Log())
        out = {}
        for kind, d in (("predicted", self.tomorrow), ("observed", self.yesterday)):
            out[kind] = json.loads((self.tmp_path / "content" / f"{kind}_{d}.json").read_text())
        return out, discord

    def test_non_wdw_failure_is_warning_not_hold(self):
        out, discord = self._run({"TDS": 80.0})
        for kind, j in out.items():
            self.assertEqual(j["status"], "ready", kind)
            self.assertEqual(j["held_reasons"], [], kind)
            self.assertFalse(any("TDS" in h for h in j["held_reasons"]), kind)
            self.assertTrue(any("TDS" in w for w in j["warnings"]), kind)
        discord.assert_not_called()

    def test_wdw_failure_still_holds(self):
        out, discord = self._run({"EP": 80.0})
        for kind, j in out.items():
            self.assertEqual(j["status"], "held", kind)
            self.assertTrue(any("EP" in h for h in j["held_reasons"]), kind)
        discord.assert_called()


if __name__ == "__main__":
    unittest.main()
