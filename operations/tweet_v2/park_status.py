"""TPCR-TWEET-V2-CLOSED-ROW-001: park_status(park, date) -> "open" | "closed" | "unknown".

Closure source (R3, Fred via chief; OQ-5 closed, option b): ONLY the hand-filled data/closures.csv,
header `park,date,source_url,note`, committed header-only. dimparkhours is never used for closure (AC 1d).
  closed  = closures.csv has a row for that park and the park's local date, with an https source_url (AC 1b)
  open    = otherwise, an official dimparkhours row (is_official == 'True') with both opening_time and closing_time
  unknown = otherwise: no official row, or dimparkhours can't be read. A missing row is never closed.
closures.csv is strict (AC 1b/1c): a missing file, wrong header, unknown park code, malformed date or a
source_url that isn't https raises ClosuresError; the tweet scripts turn that into exit 1 + alert before any post.
An empty (header-only) file gives 0 closed parks.
"""
import csv
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
import logging
from zoneinfo import ZoneInfo

log = logging.getLogger("tpcr.park_status")

# Same mapping as tpcr-discord-bot bot.py _PARK_TZ
PARK_TZ = {
    "MK": "America/New_York", "EP": "America/New_York", "HS": "America/New_York", "AK": "America/New_York",
    "UF": "America/New_York",
    "DL": "America/Los_Angeles", "CA": "America/Los_Angeles", "UH": "America/Los_Angeles",
    "TDL": "Asia/Tokyo", "TDS": "Asia/Tokyo",
}
CLOSURES_HEADER = ["park", "date", "source_url", "note"]
DEFAULT_CLOSURES = Path(__file__).resolve().parents[2] / "data" / "closures.csv"


class ClosuresError(ValueError):
    """closures.csv is missing or has a bad row. Never ignored: the run fails before any post."""


def load_closures(path=DEFAULT_CLOSURES):
    """Validated set of (park, park-local date) closures from closures.csv. Raises ClosuresError."""
    try:
        with open(path, newline="") as f:
            r = csv.reader(f)
            header = next(r, None)
            rows = list(r)
    except FileNotFoundError:
        raise ClosuresError(f"closures file missing: {path}")
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        raise ClosuresError(f"closures file unreadable: {path}: {e}")
    if header != CLOSURES_HEADER:
        raise ClosuresError(f"closures header must be {','.join(CLOSURES_HEADER)}, got {header}")
    out = set()
    for n, row in enumerate(rows, start=2):
        if not any(c.strip() for c in row):
            continue
        if len(row) != len(CLOSURES_HEADER):
            raise ClosuresError(f"closures line {n}: expected 4 columns, got {len(row)}")
        park, day, url, _note = (c.strip() for c in row)
        if park not in PARK_TZ:
            raise ClosuresError(f"closures line {n}: unknown park {park!r}")
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError:
            raise ClosuresError(f"closures line {n}: bad date {day!r} (want YYYY-MM-DD)")
        u = urlparse(url)
        if u.scheme != "https" or not u.netloc or " " in url:
            raise ClosuresError(f"closures line {n}: source_url must be a valid https URL")
        out.add((park, day))
    return out


def is_closed(closures, park, at):
    """Closed at instant `at` (tz-aware): matched on the park's local date (Tokyo parks run ahead of ET)."""
    return (park, park_local_date(park, at)) in closures


def park_local_date(park, at):
    """The park's local calendar date at instant `at` (tz-aware datetime)."""
    return at.astimezone(ZoneInfo(PARK_TZ.get(park, "America/New_York"))).date().isoformat()


def load_statuses(path, parks, day, closures=frozenset()):
    """{park: status} for one park-local date string `day`. `closures` comes from load_closures().
    path=None (no hours file given): non-closed parks count as open. One warning if the hours file is unreadable."""
    rows = None
    if path:
        try:
            with open(path, newline="") as f:
                rows = [r for r in csv.DictReader(f) if r.get("date") == day and r.get("park") in parks
                        and r.get("is_official") == "True"]
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            log.warning("dimparkhours unreadable (%s): every park not in closures.csv is unknown", e)
            rows = []
            path = "unreadable"
    out = {}
    for p in parks:
        if (p, day) in closures:
            out[p] = "closed"
        elif rows is None:
            out[p] = "open"
        elif any(r.get("opening_time", "").strip() and r.get("closing_time", "").strip() for r in rows if r["park"] == p):
            out[p] = "open"
        else:
            out[p] = "unknown"
    return out


def park_status(path, park, day, closures=frozenset()):
    return load_statuses(path, [park], day, closures)[park]


if __name__ == "__main__":  # park_status.py HOURS_CSV DATE [PARK...] -> JSON (closures from data/closures.csv)
    import json
    import sys
    a = sys.argv[1:]
    print(json.dumps(load_statuses(a[0], a[2:] or ["MK", "EP", "HS", "AK"], a[1], load_closures())))
