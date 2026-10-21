import tempfile
import unittest
from pathlib import Path

import duckdb

from pipeline.config import load_config
from pipeline.steps import s14_content

ALL_PARKS = ["MK", "EP", "HS", "AK", "DL", "CA", "UF", "IA", "EU", "UH", "TDL", "TDS"]
TARGET = "2026-10-22"


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


if __name__ == "__main__":
    unittest.main()
