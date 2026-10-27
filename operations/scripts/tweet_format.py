"""TPCR-TWEET-RESTYLE-CUTOVER: per-tweet format tags (old = pre-v2 Remotion video, new = v2 card, tpcr-meta#12).

Fred, Oct 27 2026: a parallel transition. Originals posted from the Oct 28 4 PM ET run on are new; a reply uses the
format of the original it answers; the old format retires at 00:00 ET Oct 31. Both instants live only in
config/tweet_format.json (FIRST_NEW_RUN, OLD_RETIRES below).

Store: tweet_formats.json next to tweet_state.json, {"<tweet_id>": {"format": "old"|"new", "kind", "reference_date",
"posted_at"}}. Reads never raise: a missing or corrupt store falls back to the original's best-known format
(its tweet_state posted_at vs FIRST_NEW_RUN; unknown = old inside the window, new after it).

CLI (Mini migration, idempotent): python3 tweet_format.py --migrate STATE_FILE [--dry-run]
tags every predicted/observed tweet in tweet_state.json that has no tag, by its posted_at."""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Runs outside a checkout (the Mini copies in ~/hazeydata/scripts) set TPCR_TWEET_FORMAT_CONFIG.
CONFIG = Path(os.environ.get("TPCR_TWEET_FORMAT_CONFIG") or Path(__file__).resolve().parents[2] / "config" / "tweet_format.json")
_cfg = json.loads(CONFIG.read_text())
FIRST_NEW_RUN = datetime.fromisoformat(_cfg["first_new_run"])
OLD_RETIRES = datetime.fromisoformat(_cfg["old_retires"])
STORE_NAME = _cfg["store_name"]
OLD, NEW = "old", "new"


def store_path(state_file):
    return Path(state_file).with_name(STORE_NAME)


def _parse(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t if t.tzinfo else None
    except (TypeError, ValueError):
        return None


def original_format(posted_at):
    """Format of an original posted at posted_at (aware datetime): new from FIRST_NEW_RUN on, else old."""
    return NEW if posted_at >= FIRST_NEW_RUN else OLD


def load_store(state_file, log=print):
    """{tweet_id: entry}; {} on a missing or corrupt store (logged, never raised)."""
    p = store_path(state_file)
    try:
        d = json.loads(p.read_text())
        if not isinstance(d, dict):
            raise ValueError("not an object")
        return d
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 - fail safe
        log(f"tweet_format: store {p} unreadable ({e}); using best-known formats")
        return {}


def record(state_file, tweet_id, fmt, kind, reference_date, posted_at, log=print):
    """Tag tweet_id. Never raises: a failed write is logged (the reply falls back to posted_at)."""
    p = store_path(state_file)
    try:
        d = load_store(state_file, log)
        d[str(tweet_id)] = {"format": fmt, "kind": kind, "reference_date": reference_date, "posted_at": posted_at}
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tweet_formats.")
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.rename(tmp, p)  # not os.replace: tweet_state writes patch that in tests
        return True
    except Exception as e:  # noqa: BLE001
        log(f"tweet_format: could not record {tweet_id}={fmt} in {p} ({e})")
        return False


def _posted_at_from_state(state_file, tweet_id):
    try:
        s = json.loads(Path(state_file).read_text())
        for kind in ("predicted", "observed"):
            for e in (s.get(kind) or {}).values():
                if isinstance(e, dict) and str(e.get("tweet_id")) == str(tweet_id):
                    return _parse(e.get("posted_at"))
        lp = s.get("last_predicted") or {}
        if str(lp.get("tweet_id")) == str(tweet_id):
            return _parse(lp.get("posted_at"))
    except Exception:  # noqa: BLE001
        pass
    return None


def reply_format(tweet_id, state_file, now, log=print):
    """(format, reason) for a reply to original tweet_id at run time now (aware). Never raises."""
    try:
        tag = load_store(state_file, log).get(str(tweet_id))
        tag = tag.get("format") if isinstance(tag, dict) else None
        posted = _posted_at_from_state(state_file, tweet_id)
        if now >= OLD_RETIRES:
            if tag not in (OLD, NEW):
                log(f"tweet_format: NOTE untagged original {tweet_id} after the old format retired "
                    f"({OLD_RETIRES.isoformat()}); using new")
                return NEW, "untagged, after retirement"
            return NEW, f"after retirement (original tagged {tag})"
        if posted is not None and posted < FIRST_NEW_RUN:
            return OLD, "original posted before the cutover"
        if tag in (OLD, NEW):
            return tag, "tagged"
        if posted is not None:
            return original_format(posted), "untagged, by posted_at"
        return OLD, "untagged, no posted_at: old inside the window"
    except Exception as e:  # noqa: BLE001 - never crash the reply
        log(f"tweet_format: lookup failed ({e}); using old inside the window")
        return (NEW if now >= OLD_RETIRES else OLD), "lookup failed"


def migrate(state_file, dry=False, log=print):
    """Tag every untagged tweet in tweet_state.json by its posted_at (old before FIRST_NEW_RUN). Idempotent."""
    s = json.loads(Path(state_file).read_text())
    d = load_store(state_file, log)
    added = {}
    for kind in ("predicted", "observed"):
        for ref, e in (s.get(kind) or {}).items():
            tid = str((e or {}).get("tweet_id") or "")
            if not tid or tid in d:
                continue
            t = _parse(e.get("posted_at"))
            added[tid] = {"format": original_format(t) if t else OLD, "kind": kind, "reference_date": ref,
                          "posted_at": e.get("posted_at"), "migrated": True}
    log(f"tweet_format: migrate {len(added)} untagged tweet(s) -> {store_path(state_file)}{' (dry run)' if dry else ''}")
    for k, v in added.items():
        log(f"  {k}: {v['format']} ({v['kind']} {v['reference_date']})")
    if not dry and added:
        for k, v in added.items():
            record(state_file, k, v["format"], v["kind"], v["reference_date"], v["posted_at"], log)
    return added


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "--migrate":
        migrate(a[1], dry="--dry-run" in a)
    else:
        sys.exit("usage: tweet_format.py --migrate STATE_FILE [--dry-run]")
