"""Tests for src/live_duckdb_writes.py and every live data_freshness writer.

Each writer is exercised against a table without a PRIMARY KEY and against one
with a PRIMARY KEY (temp DuckDB files only, no network, no S3).
"""

import contextlib
import datetime as dt
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts", ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import live_duckdb_writes as w  # noqa: E402

LOGGER = logging.getLogger("test_live_duckdb_writes")

FRESHNESS_NO_PK = (
    "CREATE TABLE data_freshness (source VARCHAR, last_updated TIMESTAMP, "
    "row_count INTEGER, notes VARCHAR);"
)
FRESHNESS_PK = (
    "CREATE TABLE data_freshness (source VARCHAR PRIMARY KEY, last_updated TIMESTAMP NOT NULL, "
    "row_count INTEGER, notes VARCHAR);"
)
LIVE_WAITS_NO_PK = (
    "CREATE TABLE live_waits (entity_code VARCHAR, observed_at TIMESTAMP WITH TIME ZONE, "
    "wait_time_type VARCHAR, wait_time_minutes INTEGER, park_date DATE, "
    "inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
)
STALE = "TIMESTAMP '2020-01-01 00:00:00'"


def _write_parquet(df, path):
    con = duckdb.connect()
    try:
        con.register("df_view", df)
        con.execute(f"COPY df_view TO '{path}' (FORMAT PARQUET)")
    finally:
        con.close()


def _ddl_wti(pk):
    return (
        "CREATE TABLE wti (park_code VARCHAR, park_date DATE, time_slot VARCHAR, wti DOUBLE, "
        "source VARCHAR, updated_at TIMESTAMP" + (", PRIMARY KEY (park_code, park_date, time_slot)" if pk else "") + ");"
    )


def _ddl_forecasts(pk):
    return (
        "CREATE TABLE forecasts (entity_code VARCHAR, park_date DATE, time_slot VARCHAR, "
        "predicted_actual DOUBLE, prediction_method VARCHAR, updated_at TIMESTAMP"
        + (", PRIMARY KEY (entity_code, park_date, time_slot)" if pk else "") + ");"
    )


def _ddl_entities(pk):
    return (
        "CREATE TABLE entities (entity_code VARCHAR, entity_name VARCHAR, short_name VARCHAR, "
        "park_code VARCHAR, property_code VARCHAR, has_posted BOOLEAN, updated_at TIMESTAMP"
        + (", PRIMARY KEY (entity_code)" if pk else "") + ");"
    )


class _Base:
    """Subclasses set ``pk``; tables are created with or without primary keys."""

    pk = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.db = self.base / "tpcr_live.duckdb"
        con = duckdb.connect(str(self.db))
        con.execute(FRESHNESS_PK if self.pk else FRESHNESS_NO_PK)
        con.close()

    def run_sql(self, sql, params=None):
        con = duckdb.connect(str(self.db))
        try:
            return con.execute(sql, params or []).fetchall()
        finally:
            con.close()

    def freshness(self, source):
        return self.run_sql(
            "SELECT row_count, notes FROM data_freshness WHERE source = ?", [source]
        )

    def seed_stale(self, source):
        self.run_sql(f"INSERT INTO data_freshness VALUES (?, {STALE}, 999, 'old')", [source])

    def assert_fresh_row(self, source, row_count, notes):
        self.assertEqual(self.freshness(source), [(row_count, notes)])
        stale = self.run_sql(
            f"SELECT COUNT(*) FROM data_freshness WHERE source = ? AND last_updated = {STALE}",
            [source],
        )
        self.assertEqual(stale, [(0,)])


class _HelperTests(_Base):
    def test_insert_when_absent(self):
        con = duckdb.connect(str(self.db))
        w.replace_data_freshness(con, "x", "n1", row_count=7)
        con.close()
        self.assert_fresh_row("x", 7, "n1")

    def test_replaces_existing_row_without_duplicating(self):
        self.seed_stale("x")
        self.seed_stale("other")
        con = duckdb.connect(str(self.db))
        w.replace_data_freshness(con, "x", "n2", row_count=3)
        w.replace_data_freshness(con, "x", "n3", row_count=4)
        con.close()
        self.assert_fresh_row("x", 4, "n3")
        self.assertEqual(self.freshness("other"), [(999, "old")])  # other keys untouched

    def test_count_table(self):
        self.run_sql("CREATE TABLE t (a INTEGER)")
        self.run_sql("INSERT INTO t VALUES (1), (2), (3)")
        con = duckdb.connect(str(self.db))
        w.replace_data_freshness(con, "t", "cnt", count_table="t")
        con.close()
        self.assert_fresh_row("t", 3, "cnt")

    def test_last_updated_column(self):
        self.run_sql(LIVE_WAITS_NO_PK)
        self.run_sql(
            "INSERT INTO live_waits (entity_code, inserted_at) VALUES "
            "('A', TIMESTAMP '2024-05-01 01:00:00'), ('B', TIMESTAMP '2024-05-02 03:04:05')"
        )
        con = duckdb.connect(str(self.db))
        w.replace_data_freshness(
            con, "scraper", "init", count_table="live_waits", last_updated_column="inserted_at"
        )
        con.close()
        self.assertEqual(
            self.run_sql("SELECT last_updated, row_count, notes FROM data_freshness"),
            [(dt.datetime(2024, 5, 2, 3, 4, 5), 2, "init")],
        )

    def test_rejects_bad_table_name(self):
        con = duckdb.connect(str(self.db))
        try:
            with self.assertRaises(ValueError):
                w.replace_data_freshness(con, "x", "n", count_table="t; DROP TABLE t")
        finally:
            con.close()

    def test_failure_between_delete_and_insert_rolls_back(self):
        self.seed_stale("x")

        class _Failing:
            def __init__(self, con):
                self.con = con

            def execute(self, sql, params=None):
                if "INSERT INTO data_freshness" in sql:
                    raise RuntimeError("injected")
                return self.con.execute(sql, params) if params is not None else self.con.execute(sql)

        con = duckdb.connect(str(self.db))
        try:
            with self.assertRaises(RuntimeError):
                w.replace_data_freshness(_Failing(con), "x", "n", row_count=1)
        finally:
            con.close()
        self.assertEqual(self.freshness("x"), [(999, "old")])


class _WriterTests(_Base):
    """One call-through per live data_freshness writer."""

    def test_s11_deploy(self):
        from pipeline.steps import s11_deploy

        self.run_sql(_ddl_wti(self.pk))
        self.run_sql(_ddl_forecasts(self.pk))
        self.seed_stale("wti")
        self.seed_stale("forecasts")
        wti_dir, fc_dir = self.base / "wti", self.base / "fc"
        wti_dir.mkdir()
        fc_dir.mkdir()
        d = dt.date(2024, 1, 15)
        _write_parquet(pd.DataFrame({
            "park_code": ["MK", "EP"], "park_date": [d, d], "wti": [1.5, 2.5],
            "source": ["forecast", "forecast"],
        }), wti_dir / "wti.parquet")
        _write_parquet(pd.DataFrame({
            "entity_code": ["MK01", "MK02", "MK03"], "park_date": [d] * 3,
            "time_slot": ["10:00"] * 3, "predicted_actual": [10.0, 20.0, 30.0],
            "prediction_method": ["model"] * 3,
        }), fc_dir / "all_forecasts.parquet")
        cfg = SimpleNamespace(
            shadow=False, wti_dir=wti_dir, forecast_dir=fc_dir, duckdb_path=self.db
        )
        log = mock.MagicMock()
        log.timed = lambda *_a, **_k: contextlib.nullcontext()

        # Test env may lack a parquet engine for pandas; read via DuckDB instead.
        def _read_parquet(path, *_a, **_k):
            c = duckdb.connect()
            try:
                return c.execute("SELECT * FROM read_parquet(?)", [str(path)]).df()
            finally:
                c.close()

        for _ in range(2):  # rerun must not duplicate freshness rows
            with mock.patch.object(s11_deploy.pd, "read_parquet", _read_parquet):
                result = s11_deploy.run(cfg, log)
            self.assertEqual(result["action"], "deployed")
            self.assert_fresh_row("wti", 2, "pipeline")
            self.assert_fresh_row("forecasts", 3, "pipeline")

    def test_entities_refresh(self):
        try:
            import get_entity_table_from_s3 as ge
        except ImportError as e:  # e.g. boto3 missing
            self.skipTest(f"cannot import get_entity_table_from_s3: {e}")
        self.run_sql(_ddl_entities(self.pk))
        self.seed_stale("entities")
        dim = self.base / "dimentity.csv"
        dim.write_text("entity_code,entity_name\nMK01,Ride One\nMK02,Ride Two\n")
        for _ in range(2):
            ge._refresh_entities_duckdb(dim, self.base, LOGGER)
            self.assertEqual(self.run_sql("SELECT COUNT(*) FROM entities"), [(2,)])
            self.assert_fresh_row("entities", 2, "dimension_fetch")

    def test_ask_response_monitor_fix(self):
        try:
            import ask_response_monitor as arm
        except ImportError as e:
            self.skipTest(f"cannot import ask_response_monitor: {e}")
        # attempt_fix also runs INSERT OR IGNORE on forecasts, which needs a PK there.
        self.run_sql(_ddl_forecasts(True))
        self.seed_stale("forecasts")
        d = dt.date(2024, 1, 15)
        parquet = self.base / "all_forecasts.parquet"
        _write_parquet(pd.DataFrame({
            "entity_code": ["MK01", "MK02"], "park_date": [d, d], "time_slot": ["10:00"] * 2,
            "predicted_actual": [10.0, 20.0], "prediction_method": ["model"] * 2,
        }), parquet)
        with mock.patch.object(arm, "DUCKDB_PATH", str(self.db)), \
                mock.patch.object(arm, "ALL_FORECASTS", str(parquet)):
            for _ in range(2):
                res = arm.attempt_fix({"root_cause": "forecasts_table_incomplete"})
                fix = res["fix_applied"]
                self.assertTrue(fix["success"], fix)
                self.assert_fresh_row("forecasts", 2, "auto-fix-reload")

    def test_init_live_duckdb(self):
        import init_live_duckdb as init_mod

        # Pre-create live_waits so init's CREATE TABLE IF NOT EXISTS keeps this schema.
        if self.pk:
            self.run_sql(
                "CREATE TABLE live_waits (entity_code VARCHAR NOT NULL, "
                "observed_at TIMESTAMP WITH TIME ZONE NOT NULL, wait_time_type VARCHAR DEFAULT 'POSTED', "
                "wait_time_minutes INTEGER NOT NULL, park_date DATE NOT NULL, "
                "inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (entity_code, observed_at, wait_time_type))"
            )
        else:
            self.run_sql(LIVE_WAITS_NO_PK)
        for src in ("scraper", "wti", "forecasts", "entities"):
            self.seed_stale(src)
        staging = self.base / "staging" / "queue_times"
        staging.mkdir(parents=True)
        (staging / "a.csv").write_text(
            "entity_code,observed_at,wait_time_type,wait_time_minutes\n"
            "MK01,2024-01-15T10:30:00-05:00,POSTED,30\n"
            "MK01,2024-01-15T10:30:00-05:00,POSTED,30\n"      # duplicate key in batch
            "MK02,2024-01-15T10:35:00-05:00,,20\n"             # NULL type -> POSTED
            ",2024-01-15T10:40:00-05:00,POSTED,15\n"           # NULL entity -> dropped
            "MK03,2024-01-15T10:45:00-05:00,POSTED,0\n"        # not > 0 -> filtered
        )
        for _ in range(2):  # rerun must not duplicate
            self.assertTrue(init_mod.init(self.db, self.base, LOGGER))
            rows = self.run_sql(
                "SELECT entity_code, wait_time_type, wait_time_minutes FROM live_waits ORDER BY ALL"
            )
            self.assertEqual(rows, [("MK01", "POSTED", 30), ("MK02", "POSTED", 20)])
            self.assert_fresh_row("scraper", 2, "init")
            self.assert_fresh_row("wti", 0, "init")
            self.assert_fresh_row("forecasts", 0, "init")
            self.assert_fresh_row("entities", 0, "init")


class HelperNoPkTest(_HelperTests, unittest.TestCase):
    pk = False


class HelperPkTest(_HelperTests, unittest.TestCase):
    pk = True


class WritersNoPkTest(_WriterTests, unittest.TestCase):
    pk = False


class WritersPkTest(_WriterTests, unittest.TestCase):
    pk = True


if __name__ == "__main__":
    unittest.main()
