import fcntl
import hashlib
import json
import os
import sys
from contextlib import contextmanager
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
    if not lines:
        print(f"ERROR: note_too_long: no note line fits in {limit} characters", file=sys.stderr)
        return None
    return "\n".join(lines)


def digest(text, in_reply_to):
    return hashlib.sha256((text + in_reply_to).encode()).hexdigest()


@contextmanager
def locked(state_file):
    with open(f"{state_file}.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load(state_file):
    p = Path(state_file)
    return json.loads(p.read_text()) if p.exists() else {}


def _save(state_file, state):
    tmp = Path(f"{state_file}.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, state_file)


def save_draft(state_file, reference_date, in_reply_to, text):
    if not in_reply_to or not text:
        print(f"ERROR: note draft for {reference_date} needs text and in_reply_to", file=sys.stderr)
        return None
    with locked(state_file):
        state = _load(state_file)
        drafts = state.setdefault("note_drafts", {})
        if reference_date in drafts:
            return drafts[reference_date]
        drafts[reference_date] = {"text": text, "in_reply_to": in_reply_to, "sha256": digest(text, in_reply_to), "status": "draft"}
        _save(state_file, state)
        return drafts[reference_date]


def refusal(d, reference_date):
    if not d:
        return f"no note draft for {reference_date}"
    if d.get("status") in ("posting", "unknown"):
        return f"note draft for {reference_date} is {d.get('status')!r}: check X manually for a reply under tweet {d.get('in_reply_to')} before doing anything"
    if d.get("status") != "draft":
        return f"note draft for {reference_date} is {d.get('status')!r}, not 'draft'"
    if not isinstance(d.get("in_reply_to"), str) or not d["in_reply_to"]:
        return f"note draft for {reference_date} has no in_reply_to; refusing to post standalone"
    if not isinstance(d.get("text"), str) or d.get("sha256") != digest(d["text"], d["in_reply_to"]):
        return f"note draft for {reference_date} failed its integrity check"
    return None


def approve(state_file, reference_date, post):
    with locked(state_file):
        state = _load(state_file)
        d = state.get("note_drafts", {}).get(reference_date)
        why = refusal(d, reference_date)
        if why:
            print(f"REFUSED: {why}", file=sys.stderr)
            return None
        d["status"] = "posting"
        _save(state_file, state)
        tweet_id = post(d["text"], reply_to=d["in_reply_to"])
        d["status"], d["tweet_id"] = ("posted", tweet_id) if tweet_id else ("unknown", None)
        _save(state_file, state)
        if not tweet_id:
            print(f"REFUSED: {refusal(d, reference_date)}", file=sys.stderr)
        return tweet_id


def main(argv):
    if len(argv) != 2 or argv[0] != "approve":
        print("usage: tweet_note.py approve YYYY-MM-DD")
        return 2
    import wti_predicted_tweet as w
    w.load_env()
    return 0 if approve(w.STATE_FILE, argv[1], w.post_tweet) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
