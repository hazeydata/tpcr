"""Tests for the constraint-free live_waits insert-if-absent path (temp dirs only, no network)."""

import csv
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb
import pandas as pd
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
for sub in ("src", "scripts"):
    if str(ROOT / sub) not in sys.path:
        sys.path.insert(0, str(ROOT / sub))

import get_wait_times_from_queue_times as qt  # noqa: E402
import live_duckdb_writes as w  # noqa: E402

LOGGER = logging.getLogger("test_live_waits_insert")
TZ = ZoneInfo("America/New_York")

LEGACY_DDL = """
    CREATE TABLE live_waits (
        entity_code VARCHAR, observed_at TIMESTAMP WITH TIME ZONE,
        wait_time_type VARCHAR, wait_time_minutes INTEGER, park_date DATE,
        inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE data_freshness (
        source VARCHAR, last_updated TIMESTAMP, row_count INTEGER, notes VARCHAR);
"""
PK_DDL = """
    CREATE TABLE live_waits (
        entity_code VARCHAR NOT NULL, observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        wait_time_type VARCHAR DEFAULT 'POSTED', wait_time_minutes INTEGER NOT NULL,
        park_date DATE NOT NULL, inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (entity_code, observed_at, wait_time_type));
    CREATE TABLE data_freshness (
        source VARCHAR PRIMARY KEY, last_updated TIMESTAMP NOT NULL,
        row_count INTEGER, notes VARCHAR);
"""

T1 = "2024-01-15T10:30:00-05:00"
T2 = "2024-01-15T10:35:00-05:00"
CSV_HEADER = ["entity_code", "observed_at", "wait_time_type", "wait_time_minutes", "park_date"]


def _park_date(series, tz):
    return pd.to_datetime(series, utc=True).dt.tz_convert(tz).dt.date


def _df(rows):
    return pd.DataFrame(
        rows, columns=["entity_code", "observed_at", "wait_time_type", "wait_time_minutes"]
    )


def _write_csv(path, rows):
    """Write rows to a CSV; None becomes an empty field (read back as NULL)."""
    with open(path, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(CSV_HEADER)
        for r in rows:
            wr.writerow(["" if v is None else v for v in r])
    return str(path)


class _RecordingConn:
    """Proxy for a DuckDB connection: records SQL and can fail on a chosen statement."""

    def __init__(self, con, fail_on=None):
        self._con = con
        self.fail_on = fail_on
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("injected failure")
        if params is None:
            return self._con.execute(sql)
        return self._con.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._con, name)


class _Base:
    ddl = ""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.db = self.base / "tpcr_live.duckdb"
        con = duckdb.connect(str(self.db))
        con.execute(self.ddl)
        con.close()
        p = mock.patch.object(qt, "derive_park_date", _park_date)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    # -- helpers -------------------------------------------------------------
    def write(self, df):
        return qt._write_to_live_duckdb(df, TZ, self.base, LOGGER, max_retries=1)

    def query(self, sql):
        con = duckdb.connect(str(self.db))
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def snapshot(self):
        return (
            self.query(
                "SELECT entity_code, epoch(observed_at), wait_time_type, wait_time_minutes "
                "FROM live_waits ORDER BY ALL"
            ),
            self.query("SELECT source, row_count, notes FROM data_freshness ORDER BY ALL"),
        )

    def counts(self):
        n = self.query("SELECT COUNT(*) FROM live_waits")[0][0]
        d = self.query(
            "SELECT COUNT(*) FROM (SELECT 1 FROM live_waits "
            "GROUP BY entity_code, observed_at, wait_time_type HAVING COUNT(*) > 1)"
        )[0][0]
        f = self.query("SELECT COUNT(*) FROM data_freshness WHERE source='scraper'")[0][0]
        return n, d, f

    def insert_csv(self, rows, name="batch.csv"):
        """Run insert_new_live_waits on a CSV; returns the dropped count."""
        path = _write_csv(self.base / name, rows)
        con = duckdb.connect(str(self.db))
        try:
            return w.insert_new_live_waits(con, w.CSV_WAITS_SOURCE_SQL, [path], logger=LOGGER)
        finally:
            con.close()

    def seed_state(self):
        """One existing live_waits row and a stale scraper freshness row."""
        con = duckdb.connect(str(self.db))
        con.execute(
            "INSERT INTO live_waits (entity_code, observed_at, wait_time_type, wait_time_minutes, park_date) "
            f"VALUES ('MK01', '{T1}'::TIMESTAMPTZ, 'POSTED', 10, '2024-01-15')"
        )
        con.execute(
            "INSERT INTO data_freshness VALUES ('scraper', TIMESTAMP '2020-01-01 00:00:00', 999, 'old')"
        )
        con.close()

    def sample(self):
        return _df([
            ("MK01", T1, "POSTED", 30),
            ("MK01", T2, "POSTED", 35),
            ("MK02", T1, "POSTED", 20),
        ])

    # -- behaviour -----------------------------------------------------------
    def test_insert_then_idempotent(self):
        self.assertTrue(self.write(self.sample()))
        self.assertEqual(self.counts(), (3, 0, 1))
        self.assertTrue(self.write(self.sample()))
        self.assertEqual(self.counts(), (3, 0, 1))

    def test_duplicate_keys_in_input(self):
        df = pd.concat([self.sample(), self.sample().iloc[:2]], ignore_index=True)
        self.assertTrue(self.write(df))
        self.assertEqual(self.counts(), (3, 0, 1))

    def test_second_write_keeps_first_value(self):
        self.assertTrue(self.write(_df([("MK01", T1, "POSTED", 30)])))
        self.assertTrue(self.write(_df([("MK01", T1, "POSTED", 55)])))
        self.assertEqual(
            self.query("SELECT wait_time_minutes FROM live_waits"), [(30,)]
        )

    def test_existing_key_keeps_first_value_via_helper(self):
        self.seed_state()
        dropped = self.insert_csv([("MK01", T1, "POSTED", 99, "2024-01-15")])
        self.assertEqual(dropped, 0)
        self.assertEqual(self.query("SELECT wait_time_minutes FROM live_waits"), [(10,)])

    def test_duplicate_keys_in_batch_keep_highest_wait(self):
        self.insert_csv([
            ("MK01", T1, "POSTED", 30, "2024-01-15"),
            ("MK01", T1, "POSTED", 50, "2024-01-15"),
        ])
        self.assertEqual(self.query("SELECT wait_time_minutes FROM live_waits"), [(50,)])

    def test_existence_check_uses_full_key(self):
        """A row sharing only part of the key with an existing row is still new."""
        self.seed_state()  # (MK01, T1, POSTED)
        self.insert_csv([
            ("MK01", T1, "ACTUAL", 11, "2024-01-15"),   # differs only by wait_time_type
            ("MK01", T2, "POSTED", 12, "2024-01-15"),   # same entity, different observed_at
            ("MK02", T1, "POSTED", 13, "2024-01-15"),   # same observed_at, different entity
        ])
        rows = self.query(
            "SELECT entity_code, wait_time_type, wait_time_minutes FROM live_waits ORDER BY ALL"
        )
        self.assertEqual(
            rows,
            [("MK01", "ACTUAL", 11), ("MK01", "POSTED", 10), ("MK01", "POSTED", 12),
             ("MK02", "POSTED", 13)],
        )

    # -- dropped key rows ----------------------------------------------------
    def test_null_and_empty_keys_dropped(self):
        rows = [
            ("MK01", T1, "POSTED", 30, "2024-01-15"),
            ("MK02", T2, "POSTED", 20, "2024-01-15"),
            (None, T1, "POSTED", 5, "2024-01-15"),
            ("MK03", None, "POSTED", 6, "2024-01-15"),
            ("MK04", T1, None, 7, "2024-01-15"),
        ]
        with self.assertLogs(LOGGER, level="WARNING") as cm:
            dropped = self.insert_csv(rows)
        self.assertEqual(dropped, 3)
        self.assertTrue(any("dropped 3" in m for m in cm.output))
        self.assertEqual(self.counts()[0], 2)
        # rerun: still 3 dropped, nothing duplicated
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertEqual(self.insert_csv(rows), 3)
        self.assertEqual(self.counts()[:2], (2, 0))

    def test_empty_string_keys_dropped(self):
        src = """
            SELECT * FROM (VALUES
                ('MK01', '2024-01-15T10:30:00-05:00', 'POSTED', 30, '2024-01-15'),
                ('',     '2024-01-15T10:30:00-05:00', 'POSTED', 31, '2024-01-15'),
                ('MK02', '',                          'POSTED', 32, '2024-01-15'),
                ('MK03', '2024-01-15T10:30:00-05:00', '',       33, '2024-01-15'),
                (NULL,   NULL,                        NULL,     34, '2024-01-15')
            ) AS t(entity_code, observed_at, wait_time_type, wait_time_minutes, park_date)
        """
        con = duckdb.connect(str(self.db))
        try:
            with self.assertLogs(LOGGER, level="WARNING"):
                dropped = w.insert_new_live_waits(con, src, logger=LOGGER)
            self.assertEqual(dropped, 4)
            with self.assertLogs(LOGGER, level="WARNING"):
                self.assertEqual(w.insert_new_live_waits(con, src, logger=LOGGER), 4)
            self.assertEqual(
                con.execute("SELECT entity_code, wait_time_minutes FROM live_waits").fetchall(),
                [("MK01", 30)],
            )
        finally:
            con.close()

    def test_no_warning_when_nothing_dropped(self):
        with self.assertNoLogs(LOGGER, level="WARNING"):
            self.assertEqual(self.insert_csv([("MK01", T1, "POSTED", 30, "2024-01-15")]), 0)

    def test_scraper_path_drops_bad_rows_and_logs(self):
        df = _df([
            ("MK01", T1, "POSTED", 30),
            (None, T1, "POSTED", 25),
            ("MK02", T2, "POSTED", 20),
        ])
        with self.assertLogs(LOGGER, level="WARNING") as cm:
            self.assertTrue(self.write(df))
        self.assertTrue(any("dropped 1" in m for m in cm.output))
        self.assertEqual(self.counts(), (2, 0, 1))
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertTrue(self.write(df))
        self.assertEqual(self.counts(), (2, 0, 1))

    def test_scraper_path_counts_every_dropped_row(self):
        """Bad observed_at is dropped in Python, the rest by the helper; all 5 are logged."""
        def lenient_park_date(series, tz):
            return pd.to_datetime(series, utc=True, errors="coerce").dt.tz_convert(tz).dt.date

        df = _df([
            ("MK01", T1, "POSTED", 30),
            ("MK02", "not-a-date", "POSTED", 31),
            (None, T1, "POSTED", 32),
            ("", T1, "POSTED", 33),
            ("MK03", T1, None, 34),
            ("MK04", T1, "", 35),
        ])
        with mock.patch.object(qt, "derive_park_date", lenient_park_date):
            with self.assertLogs(LOGGER, level="WARNING") as cm:
                self.assertTrue(self.write(df))
        self.assertTrue(any("dropped 5" in m for m in cm.output))
        self.assertEqual(self.counts()[0], 1)

    # -- atomicity -----------------------------------------------------------
    def _batch_csv(self):
        return _write_csv(
            self.base / "batch.csv",
            [("MK01", T2, "POSTED", 40, "2024-01-15"), ("MK02", T1, "POSTED", 41, "2024-01-15")],
        )

    def test_rollback_when_freshness_insert_fails(self):
        """Failure after the freshness DELETE must undo the DELETE and the live_waits insert."""
        self.seed_state()
        before = self.snapshot()
        con = duckdb.connect(str(self.db))
        proxy = _RecordingConn(con, fail_on="INSERT INTO data_freshness")
        try:
            with self.assertRaises(RuntimeError):
                w.write_scraper_batch(proxy, self._batch_csv())
            self.assertIn("ROLLBACK", proxy.statements)
        finally:
            con.close()
        self.assertEqual(self.snapshot(), before)

    def test_rollback_when_live_waits_insert_fails(self):
        self.seed_state()
        before = self.snapshot()
        con = duckdb.connect(str(self.db))
        proxy = _RecordingConn(con, fail_on="INSERT INTO live_waits")
        try:
            with self.assertRaises(RuntimeError):
                w.write_scraper_batch(proxy, self._batch_csv())
        finally:
            con.close()
        self.assertEqual(self.snapshot(), before)

    def test_scraper_batch_is_wrapped_in_transaction(self):
        self.seed_state()
        con = duckdb.connect(str(self.db))
        proxy = _RecordingConn(con)
        try:
            w.write_scraper_batch(proxy, self._batch_csv())
        finally:
            con.close()
        self.assertEqual(proxy.statements[0], "BEGIN")
        self.assertEqual(proxy.statements[-1], "COMMIT")
        # committed data is visible to a fresh connection
        self.assertEqual(self.counts()[0], 3)
        self.assertEqual(
            self.query("SELECT row_count, notes FROM data_freshness WHERE source='scraper'"),
            [(3, "queue-times")],
        )


class LegacyNoPkTest(_Base, unittest.TestCase):
    ddl = LEGACY_DDL


class PkSchemaTest(_Base, unittest.TestCase):
    ddl = PK_DDL


if __name__ == "__main__":
    unittest.main()
