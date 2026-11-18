"""TPCR-ACCURACY-001: queue-times Universal Studios Hollywood (park 66) mapping."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("requests")
pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

qt = pytest.importorskip("get_wait_times_from_queue_times")

UH_PAIRS = {
    6051: "UH06", 6050: "UH13", 6055: "UH19", 6048: "UH23", 6047: "UH24",
    6057: "UH28", 6049: "UH29", 11513: "UH46", 14926: "UH75", 6053: "UH16",
    6045: "UH21", 6056: "UH27", 7332: "UH39",
}


@pytest.fixture(scope="module")
def mapping():
    df = qt.load_queue_times_mapping(ROOT / "config")
    assert df is not None
    return df


@pytest.mark.parametrize("ride_id,code", sorted(UH_PAIRS.items()))
def test_uh_rides_map_to_uh_codes(mapping, ride_id, code):
    assert qt.map_ride_to_entity_code(ride_id, "x", "uh", mapping) == code


def test_unmapped_uh_ride_keeps_fallback(mapping):
    assert qt.map_ride_to_entity_code(16543, "x", "uh", mapping) == "USH16543"


def test_no_duplicate_park_ride_pairs(mapping):
    assert not mapping.duplicated(["park_code", "queue_times_id"]).any()


def test_no_uh_entity_code_mapped_twice(mapping):
    # Scoped to UH: other parks have pre-existing multi-ride entities (e.g. AK11).
    uh = mapping[mapping["park_code"] == "uh"]
    assert len(uh) == len(UH_PAIRS)
    assert not uh["entity_code"].duplicated().any()
