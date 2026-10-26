"""TPCR-TWEET-V2-CLOSED-ROW-001 (C1): park_status(park, date) -> "open" | "closed" | "unknown".

Source: dimparkhours.csv (the file the Discord bot reads as DIMPARKHOURS_PATH,
<data>/dimension_tables/dimparkhours.csv). Only rows with is_official == 'True' count.
  open    = an official row with both opening_time and closing_time set
  closed  = an official row carrying the explicit closure marker (CLOSED_MARKER)
  unknown = no official row, or the file can't be read. Unknown is never shown as closed.

OQ-1 (still open): the current writer, src/get_park_hours_from_s3.py STEP 4, sets is_official=True only when
opening_time is non-blank and not the 1999-01-01 placeholder, so the file as written today has NO field that
marks a closure. CLOSED_MARKER is therefore None by default and real runs can only return open/unknown.
Set it (column, value) once Spec/chief name the marker; the fixtures pass one explicitly.
"""
import csv
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("tpcr.park_status")

# Same mapping as tpcr-discord-bot bot.py _PARK_TZ
PARK_TZ = {
    "MK": "America/New_York", "EP": "America/New_York", "HS": "America/New_York", "AK": "America/New_York",
    "UF": "America/New_York",
    "DL": "America/Los_Angeles", "CA": "America/Los_Angeles", "UH": "America/Los_Angeles",
    "TDL": "Asia/Tokyo", "TDS": "Asia/Tokyo",
}
CLOSED_MARKER = None  # OQ-1: (column, value) once known


def park_local_date(park, at):
    """The park's local calendar date at instant `at` (tz-aware datetime)."""
    return at.astimezone(ZoneInfo(PARK_TZ.get(park, "America/New_York"))).date().isoformat()


def load_statuses(path, parks, day, closed_marker=CLOSED_MARKER):
    """{park: status} for one park-local date string `day`. One warning if the file is unreadable."""
    try:
        with open(path, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("date") == day and r.get("park") in parks
                    and r.get("is_official") == "True"]
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        log.warning("dimparkhours unreadable (%s): every park unknown", e)
        return {p: "unknown" for p in parks}
    out = {}
    for p in parks:
        mine = [r for r in rows if r["park"] == p]
        if closed_marker and any(r.get(closed_marker[0]) == closed_marker[1] for r in mine):
            out[p] = "closed"
        elif any(r.get("opening_time", "").strip() and r.get("closing_time", "").strip() for r in mine):
            out[p] = "open"
        else:
            out[p] = "unknown"
    return out


def park_status(path, park, day, closed_marker=CLOSED_MARKER):
    return load_statuses(path, [park], day, closed_marker)[park]


if __name__ == "__main__":  # park_status.py PATH DATE [PARK...] -> JSON
    import json
    import sys
    a = sys.argv[1:]
    print(json.dumps(load_statuses(a[0], a[2:] or ["MK", "EP", "HS", "AK"], a[1])))
