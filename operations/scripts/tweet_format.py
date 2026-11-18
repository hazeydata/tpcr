"""TPCR-TWEET-RESTYLE-CUTOVER: per-tweet format tags (old = pre-v2 Remotion video, new = v2 card, tpcr-meta#12).

Fred, Oct 27 2026: a parallel transition. Originals posted from the Oct 29 4 PM ET run on are new; a reply uses the
format of the original it answers; the old format retires at 00:00 ET Nov 1 (moved +24h from Oct 28 / Oct 31). Both instants live only in
config/tweet_format.json (FIRST_NEW_RUN, OLD_RETIRES below).

Store: tweet_formats.json next to tweet_state.json, {"<tweet_id>": {"format": "old"|"new", "kind", "reference_date",
"posted_at"}}. Reads never raise: a missing or corrupt store falls back to the original's best-known format
(its tweet_state posted_at vs FIRST_NEW_RUN; unknown = old inside the window, new after it).

CLI (Mini migration, idempotent): python3 tweet_format.py --migrate STATE_FILE [--dry-run]
tags every predicted/observed tweet in tweet_state.json that has no tag, by its posted_at."""
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Runs outside a checkout (the Mini copies in ~/hazeydata/scripts) set TPCR_TWEET_FORMAT_CONFIG.
CONFIG = Path(os.environ.get("TPCR_TWEET_FORMAT_CONFIG") or Path(__file__).resolve().parents[2] / "config" / "tweet_format.json")
OLD, NEW = "old", "new"
ET = ZoneInfo("America/Toronto")
# A missing or corrupt config must not crash the 4 PM / 8:30 AM scripts at import. Defined behavior: CONFIG_ERROR is
# set, an ERROR is logged to stderr, and the window collapses to "already retired" (FIRST_NEW_RUN = OLD_RETIRES =
# datetime.min UTC): every reply is NEW; the 4 PM original run is SKIPPED until a new original exists (D7).
CONFIG_ERROR = None
try:
    _cfg = json.loads(CONFIG.read_text())
    FIRST_NEW_RUN = datetime.fromisoformat(_cfg["first_new_run"])
    OLD_RETIRES = datetime.fromisoformat(_cfg["old_retires"])
    if FIRST_NEW_RUN.tzinfo is None or OLD_RETIRES.tzinfo is None:
        raise ValueError("first_new_run/old_retires need a UTC offset")
    STORE_NAME = str(_cfg.get("store_name") or "tweet_formats.json")
except Exception as _e:  # noqa: BLE001 - fail safe, see above
    CONFIG_ERROR = f"{type(_e).__name__}: {_e}"
    print(f"ERROR tweet_format: config {CONFIG} unusable ({CONFIG_ERROR}); "
          "every reply is new; the 4 PM original run is skipped until a new-format original exists", file=sys.stderr)
    FIRST_NEW_RUN = OLD_RETIRES = datetime.min.replace(tzinfo=timezone.utc)
    STORE_NAME = "tweet_formats.json"


def _tiers(cfg):
    """(A, B) for the new-format reply park lines: |miss| <= A on target, <= B off, > B far off. Else the error."""
    try:
        a, b = cfg["reply_tier_a"], cfg["reply_tier_b"]
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)) or not 0 < a < b:
            return None, f"reply_tier_a/reply_tier_b unset or invalid ({a!r}, {b!r}); need 0 < A < B"
        return (float(a), float(b)), None
    except Exception as e:  # noqa: BLE001 - fail closed
        return None, f"{type(e).__name__}: {e}"


REPLY_TIERS, TIERS_ERROR = _tiers(_cfg) if not CONFIG_ERROR else (None, CONFIG_ERROR)

# TPCR-DISCORD-GROWTH-001 (Fred, Nov 18 2026): one Discord line on the 8:30 AM OBSERVED reply, for at most 14 posting
# days. discord_cta = {code, first_post_date, last_post_date}; dates are America/Toronto posting dates, inclusive.
DISCORD_CODE = re.compile(r"^[A-Za-z0-9-]{2,32}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DISCORD_MAX_SPAN = timedelta(days=13)  # last - first: 14 posts


def _discord_cta(cfg):
    """(cta, error): cta = {"code", "first", "last"} (dates as date) or None. Missing key or all null = (None, None)."""
    raw = cfg.get("discord_cta") if isinstance(cfg, dict) else None
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, f"discord_cta is {type(raw).__name__}, not an object"
    vals = [raw.get(k) for k in ("code", "first_post_date", "last_post_date")]
    if all(v is None for v in vals):
        return None, None
    code, first, last = vals
    if any(v is None for v in vals):
        return None, f"discord_cta partially set ({vals!r}); need code, first_post_date and last_post_date"
    if not isinstance(code, str) or not DISCORD_CODE.match(code):
        return None, f"discord_cta.code {code!r} invalid; need ^[A-Za-z0-9-]{{2,32}}$"
    try:
        if not all(isinstance(d, str) and ISO_DATE.match(d) for d in (first, last)):
            raise ValueError("not YYYY-MM-DD")
        f, l = date.fromisoformat(first), date.fromisoformat(last)
    except ValueError as e:
        return None, f"discord_cta dates ({first!r}, {last!r}) invalid ({e}); need YYYY-MM-DD"
    if f > l:
        return None, f"discord_cta first_post_date {first} is after last_post_date {last}"
    if l - f > DISCORD_MAX_SPAN:
        return None, f"discord_cta window {first}..{last} is {(l - f).days + 1} days; max 14 (last = first + 13)"
    return {"code": code, "first": f, "last": l}, None


def parse_discord_cta(cfg, err=None):
    """The validated discord_cta, or None (line off). Partial/invalid: one ERROR line on stderr. Never raises."""
    try:
        cta, e = _discord_cta(cfg)
    except Exception as x:  # noqa: BLE001 - never crash the import
        cta, e = None, f"{type(x).__name__}: {x}"
    if e:
        print(f"ERROR tweet_format: {e}; Discord line off", file=err or sys.stderr)
    return cta


DISCORD_CTA = parse_discord_cta(_cfg) if not CONFIG_ERROR else None


def discord_cta_line(today, cta=None):
    """The exact Discord line (tweet_copy.DISCORD_CTA) when the America/Toronto date today (a date, or an aware
    datetime converted to ET) is inside the window, else None. cta defaults to DISCORD_CTA. Never raises."""
    try:
        cta = DISCORD_CTA if cta is None else cta
        if not cta:
            return None
        if isinstance(today, datetime):
            today = today.astimezone(ET).date() if today.tzinfo else today.date()
        if not cta["first"] <= today <= cta["last"]:
            return None
        v2 = str(Path(os.environ.get("TPCR_TWEET_V2_DIR") or Path(__file__).resolve().parents[1] / "tweet_v2"))
        if v2 not in sys.path:
            sys.path.insert(0, v2)
        import tweet_copy
        return tweet_copy.DISCORD_CTA.format(code=cta["code"])
    except Exception as e:  # noqa: BLE001 - the CTA must never stop the reply
        print(f"ERROR tweet_format: Discord line lookup failed ({type(e).__name__}: {e}); line off", file=sys.stderr)
        return None
CONFIG_REASON = "config missing/invalid: everything new"


def original_run_blocked(state_file, log=print):
    """None if the 4 PM new-format original run may post; else the reason it must post nothing.
    Only a bad config blocks it: FIRST_NEW_RUN is then unknown, so the run posts only once the tag store or
    tweet_state already records a new-format original (i.e. the first new run has happened). Never raises."""
    if not CONFIG_ERROR:
        return None
    try:
        if any(isinstance(e, dict) and e.get("format") == NEW and e.get("kind") == "predicted"
               for e in load_store(state_file, log).values()):
            return None
        s = json.loads(Path(state_file).read_text())
        if any(isinstance(e, dict) and e.get("format") == NEW for e in (s.get("predicted") or {}).values()):
            return None
    except Exception:  # noqa: BLE001 - unknown history: fail closed
        pass
    return (f"config missing/invalid ({CONFIG_ERROR}) and no new-format original recorded yet; "
            "skipping the new-format original run so nothing posts before the cutover")


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
    """(format, reason) for a reply to original tweet_id at run time now. A naive now is taken as ET
    (America/Toronto). Never raises."""
    try:
        if not isinstance(now, datetime):
            raise TypeError(f"now is {type(now).__name__}, not datetime")
        if now.tzinfo is None:
            now = now.replace(tzinfo=ET)
        tag = load_store(state_file, log).get(str(tweet_id))
        tag = tag.get("format") if isinstance(tag, dict) else None
        posted = _posted_at_from_state(state_file, tweet_id)
        if now >= OLD_RETIRES:
            if tag not in (OLD, NEW) and not CONFIG_ERROR:
                log(f"tweet_format: NOTE untagged original {tweet_id} after the old format retired "
                    f"({OLD_RETIRES.isoformat()}); using new")
                return NEW, "untagged, after retirement"
            if CONFIG_ERROR:
                return NEW, f"{CONFIG_REASON} (original tagged {tag})"
            return NEW, f"after retirement (original tagged {tag})"
        if posted is not None and posted < FIRST_NEW_RUN:
            return OLD, "original posted before the cutover"
        if tag in (OLD, NEW):
            return tag, "tagged"
        if posted is not None:
            return original_format(posted), "untagged, by posted_at"
        return OLD, "untagged, no posted_at: old inside the window"
    except Exception as e:  # noqa: BLE001 - never crash the reply
        try:
            retired = now >= OLD_RETIRES
        except Exception:  # noqa: BLE001 - now itself unusable: old inside the window per the wall clock
            retired = datetime.now(timezone.utc) >= OLD_RETIRES
        log(f"tweet_format: lookup failed ({e}); using {'new' if retired else 'old'}")
        return (NEW if retired else OLD), "lookup failed"


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
