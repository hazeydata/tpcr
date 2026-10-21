"""Tests for the constraint-free live_waits upsert (temp dirs only, no network)."""

import sys
import tempfile
import unittest
import logging
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

LOGGER = logging.getLogger("test_live_waits_upsert")
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


def _park_date(series, tz):
    return pd.to_datetime(series, utc=True).dt.tz_convert(tz).dt.date


def _df(rows):
    return pd.DataFrame(
        rows, columns=["entity_code", "observed_at", "wait_time_type", "wait_time_minutes"]
    )


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

    def write(self, df):
        return qt._write_to_live_duckdb(df, TZ, self.base, LOGGER, max_retries=1)

    def counts(self):
        con = duckdb.connect(str(self.db))
        try:
            n = con.execute("SELECT COUNT(*) FROM live_waits").fetchone()[0]
            d = con.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM live_waits "
                "GROUP BY entity_code, observed_at, wait_time_type HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            f = con.execute(
                "SELECT COUNT(*) FROM data_freshness WHERE source='scraper'"
            ).fetchone()[0]
            return n, d, f
        finally:
            con.close()

    def sample(self):
        return _df([
            ("MK01", "2024-01-15T10:30:00-05:00", "POSTED", 30),
            ("MK01", "2024-01-15T10:35:00-05:00", "POSTED", 35),
            ("MK02", "2024-01-15T10:30:00-05:00", "POSTED", 20),
        ])

    def test_insert_then_idempotent(self):
        self.assertTrue(self.write(self.sample()))
        self.assertEqual(self.counts(), (3, 0, 1))
        self.assertTrue(self.write(self.sample()))
        self.assertEqual(self.counts(), (3, 0, 1))

    def test_duplicate_keys_in_input(self):
        df = pd.concat([self.sample(), self.sample().iloc[:2]], ignore_index=True)
        self.assertTrue(self.write(df))
        self.assertEqual(self.counts(), (3, 0, 1))


class LegacyNoPkTest(_Base, unittest.TestCase):
    ddl = LEGACY_DDL


class PkSchemaTest(_Base, unittest.TestCase):
    ddl = PK_DDL


if __name__ == "__main__":
    unittest.main()
