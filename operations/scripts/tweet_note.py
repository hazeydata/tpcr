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


def cap(lines, limit=280):
    lines = list(lines)
    while lines and len("\n".join(lines)) > limit:
        lines.pop()
    return "\n".join(lines)


def _load(state_file):
    p = Path(state_file)
    return json.loads(p.read_text()) if p.exists() else {}


def save_draft(state_file, reference_date, in_reply_to, text):
    state = _load(state_file)
    drafts = state.setdefault("note_drafts", {})
    if reference_date in drafts:
        return drafts[reference_date]
    drafts[reference_date] = {"text": text, "in_reply_to": in_reply_to, "status": "draft"}
    Path(state_file).write_text(json.dumps(state, indent=2))
    return drafts[reference_date]


def approve(state_file, reference_date, post):
    state = _load(state_file)
    d = state.get("note_drafts", {}).get(reference_date)
    if not d or d["status"] != "draft":
        print(f"no pending note draft for {reference_date}")
        return None
    d["status"] = "posting"
    Path(state_file).write_text(json.dumps(state, indent=2))
    tweet_id = post(d["text"], reply_to=d["in_reply_to"])
    d["status"], d["tweet_id"] = ("posted", tweet_id) if tweet_id else ("draft", None)
    Path(state_file).write_text(json.dumps(state, indent=2))
    return tweet_id


def main(argv):
    if len(argv) != 2 or argv[0] != "approve":
        print("usage: tweet_note.py approve YYYY-MM-DD")
        return 2
    import wti_predicted_tweet as w
    w.load_env()
    return 0 if approve(w.STATE_FILE, argv[1], w.post_tweet) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
