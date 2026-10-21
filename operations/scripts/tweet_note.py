import json
import os
from datetime import date, timedelta
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[2] / "config" / "tweet_note.json"


def variant(notes):
    v = os.environ.get("TPCR_TWEET_NOTE_VARIANT", "standard")
    if v not in notes:
        print(f"WARNING: unknown TPCR_TWEET_NOTE_VARIANT {v!r}, using standard")
        return "standard"
    return v


def read_status(today):
    d = os.environ.get("TPCR_SCORECARD_DIR")
    p = Path(d) / "status.json" if d else None
    try:
        s = json.loads(p.read_text())
        fresh = date.fromisoformat(s["as_of"]) >= today - timedelta(days=2)
    except (AttributeError, OSError, ValueError, KeyError, TypeError):
        s, fresh = {}, False
    if not (fresh and s.get("ok")):
        print("status_unavailable")
        return None
    return s


def note_lines(parks, today=None):
    status = read_status(today or date.today())
    if status is None:
        return []
    notes = json.loads(CONFIG.read_text())
    texts = notes[variant(notes)]
    names = {p.get("park_code"): p["park_name"] for p in parks}
    seen, lines = set(), []
    for s in status.get("systematic_parks", []):
        if s["park"] in names and s["park"] not in seen and s["direction"] in texts:
            seen.add(s["park"])
            lines.append(texts[s["direction"]].format(park_name=names[s["park"]]))
    return lines
