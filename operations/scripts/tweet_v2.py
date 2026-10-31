"""TPCR-TWEET-V2 shared helpers for wti_predicted_tweet.py (4 PM ET) and wti_observed_tweet.py (8:30 AM ET).

Timing rule (Fred, Oct 26 2026: "we never post about today... always posting about tomorrow or yesterday"):
  reference day D -> forecast tweet at 4 PM ET on D-1; observed reply at 8:30 AM ET on D+1,
  in reply to D's forecast tweet (looked up in tweet_state.json["predicted"][D]).

Data sources are the ones the tweet scripts already use: Step 14 content JSONs on the data host
(CONTENT_DIR/predicted_D.json, observed_D.json) and the pipeline's wti.parquet on the data host (normal boxes).
The cards + MP4s are built locally by operations/tweet_v2/build_day.sh.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# TPCR_TWEET_V2_DIR: where build_day.sh lives when the scripts run outside a repo checkout (the Mini runs
# copies in ~/hazeydata/scripts). Default: this repo's operations/tweet_v2.
BUILD_DAY = Path(os.environ.get("TPCR_TWEET_V2_DIR") or ROOT / "operations" / "tweet_v2") / "build_day.sh"
# Data host and paths come from the environment only (no machine-specific defaults in source).
# TPCR_SSH_HOST (ssh target), TPCR_CONTENT_DIR (Step 14 content dir on that host), TPCR_WTI_PARQUET (wti.parquet path there).
def env_required(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is not set; export it (see operations/tweet_v2/README.md) or pass --content-dir/--history")
    return v

TWITTER_METADATA_API = "https://upload.twitter.com/1.1/media/metadata/create.json"
ALT_MAX = 1000
# Closed-row R3: the hand-filled closures file, the only closure source. Runs outside a checkout set TPCR_CLOSURES.
DEFAULT_CLOSURES = ROOT / "data" / "closures.csv"


class DryRunWrite(RuntimeError):
    """Raised if anything tries an HTTP call while --dry-run is on."""


def parse_args(kind, argv=None):
    ap = argparse.ArgumentParser(description=f"TPCR {kind} tweet (v2 card + MP4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build everything, post nothing: no X/Discord HTTP calls, no state writes, no credentials needed")
    ap.add_argument("--date", help="reference day D (default: tomorrow for predicted, yesterday for observed)")
    ap.add_argument("--content-dir", help="read predicted_D.json/observed_D.json from this local dir instead of the data host over ssh")
    ap.add_argument("--history", help="local wti.parquet instead of scp from the data host")
    ap.add_argument("--state-file", help="tweet_state.json to read (default: the live one)")
    ap.add_argument("--park-hours", default=os.environ.get("TPCR_PARK_HOURS"),
                    help="dimparkhours.csv for open/unknown (default $TPCR_PARK_HOURS; unset = non-closed parks open). Never closure")
    ap.add_argument("--closures", default=os.environ.get("TPCR_CLOSURES"),
                    help="hand-filled closures.csv, the only closure source (default $TPCR_CLOSURES, else data/closures.csv "
                         "next to build_day.sh's repo). Missing or bad file = exit 1 + alert before any post")
    ap.add_argument("--out", help="build output dir (default: a temp dir)")
    return ap.parse_args(argv)


def dry_run_requested(args, environ=os.environ):
    return bool(args.dry_run) or environ.get("TPCR_TWEET_DRY_RUN", "0") != "0"


def default_reference_date(kind, today=None):
    today = today or date.today()
    return (today + timedelta(days=1) if kind == "predicted" else today - timedelta(days=1)).isoformat()


def block_http_for_dry_run():
    """Belt and braces: in dry-run, any requests call raises before touching the network."""
    import requests

    def refuse(*a, **k):
        raise DryRunWrite(f"dry-run: blocked HTTP call {a[:1]}")
    for name in ("get", "post", "put", "delete", "patch", "request"):
        setattr(requests, name, refuse)
    requests.Session.request = refuse


def read_content(name, content_dir=None, ssh_read=None):
    """Return the raw JSON text of a Step 14 content file, or None."""
    if content_dir:
        p = Path(content_dir) / name
        return p.read_text() if p.exists() else None
    return ssh_read(f"{env_required('TPCR_CONTENT_DIR')}/{name}")


def fetch_history(work, local=None):
    if local:
        return Path(local)
    dst = Path(work) / "wti.parquet"
    r = subprocess.run(["scp", "-q", f"{env_required('TPCR_SSH_HOST')}:{env_required('TPCR_WTI_PARQUET')}", str(dst)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"scp wti.parquet failed: {r.stderr.strip()}")
    return dst


def build(D, out, history, predicted_json, observed_json=None, runner=subprocess.run, park_hours=None, closures=None):
    """Run build_day.sh. Returns dict of artifact paths (keys F/R -> {mp4, png, alt}) for what was built,
    or {"skipped_all_closed": True} when every park is closed (closed-row R1: no post at all)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    pj = out / f"predicted_{D}.json"
    pj.write_text(predicted_json)
    cmd = ["bash", str(BUILD_DAY), D, str(out), "--history", str(history), "--predicted", str(pj)]
    if observed_json is not None:
        oj = out / f"observed_{D}.json"
        oj.write_text(observed_json)
        cmd += ["--observed", str(oj)]
    else:
        cmd += ["--forecast-only"]
    if park_hours:
        cmd += ["--park-hours", str(park_hours)]
    cmd += ["--closures", str(closures or DEFAULT_CLOSURES)]
    env = dict(os.environ)
    env.setdefault("PY", sys.executable)
    r = runner(cmd, capture_output=True, text=True, timeout=1500, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"build_day.sh failed rc={r.returncode}: {(r.stderr or r.stdout).strip()[-800:]}")
    if (out / "skipped_all_closed").exists():
        return {"skipped_all_closed": True}
    arts = {}
    for k in ("F", "R"):
        mp4 = out / f"{k}-{D}.mp4"
        if mp4.exists():
            alt = (out / f"{k}-{D}.alt.txt").read_text().strip()
            meta = json.loads((out / f"{k}-{D}.meta.json").read_text())
            arts[k] = {"mp4": mp4, "png": out / f"{k}-{D}.png", "alt": alt, "meta": meta}
    return arts


def post_text(kind, arts):
    """New-format tweet text (tweet_copy.py) for the built card, or None = stay silent."""
    sys.path.insert(0, str(BUILD_DAY.parent))
    import tweet_copy
    import tweet_format
    k = "F" if kind == "predicted" else "R"
    if k not in arts:
        return None
    return tweet_copy.forecast_text(arts[k]["meta"]) if k == "F" else tweet_copy.reply_text(arts[k]["meta"], tweet_format.REPLY_TIERS)


def set_alt_text(media_id, alt, auth, post):
    """X v1.1 media metadata: attach alt text to an uploaded media_id. Returns True on success."""
    resp = post(TWITTER_METADATA_API, auth=auth, json={"media_id": media_id, "alt_text": {"text": alt[:ALT_MAX]}}, timeout=30)
    if resp.status_code not in (200, 201, 204):
        print(f"Alt text failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return False
    return True


def lookup_predicted_tweet(state_file, reference_date):
    """D's forecast tweet id from tweet_state.json (predicted[D], then last_predicted if it is for D)."""
    p = Path(state_file)
    if not p.exists():
        return None, f"no state file at {p}"
    try:
        state = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None, "state file is invalid JSON"
    hit = state.get("predicted", {}).get(reference_date, {}).get("tweet_id")
    if hit:
        return hit, f'predicted["{reference_date}"]'
    last = state.get("last_predicted", {})
    if (last.get("reference_date") or last.get("target_date")) == reference_date and last.get("tweet_id"):
        return last["tweet_id"], "last_predicted fallback"
    return None, f"no predicted tweet for {reference_date} in {p}"


def write_manifest(out, data):
    path = Path(out) / "dry_run.json"
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
    print(f"dry-run manifest: {path}")
    return path


def new_workdir(out=None):
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        return Path(out), None
    d = Path(tempfile.mkdtemp(prefix="tpcr-tweetv2-"))
    return d, d


def cleanup(tmp):
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


NO_DATA_ALERT_AFTER = 2  # closed-row R2 / AC 5a: alert once after this many consecutive skipped_no_data days


def no_data_state_path(state_file):
    """Streak state lives next to tweet_state.json, in its own file so the tweet state is never rewritten."""
    return Path(state_file).parent / "no_data_streak.json"


def record_no_data(kind, reference_date, state_file, alert, dry=False):
    """AC 5a: log skipped_no_data and keep a per-kind streak of consecutive reference days.
    Calls alert(msg) exactly once, when the streak reaches NO_DATA_ALERT_AFTER. Dry runs write no state.
    Returns the streak length. The caller then exits 0."""
    print(f"skipped_no_data kind={kind} ref={reference_date}: no ready content; nothing posted")
    p = no_data_state_path(state_file)
    try:
        st = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        st = {}
    cur = st.get(kind, {})
    last = cur.get("last_skipped")
    prev = cur.get("bridged_to") or last  # AC 5b: stale days after the last skip keep the streak alive
    if last == reference_date:
        streak = cur.get("streak", 1)  # same day re-run: no double count
    elif prev and date.fromisoformat(prev) + timedelta(days=1) == date.fromisoformat(reference_date):
        streak = cur.get("streak", 1) + 1
    else:
        streak = 1
    alerted = cur.get("alerted", False) if last == reference_date or streak > 1 else False
    if streak >= NO_DATA_ALERT_AFTER and not alerted:
        alert(f":warning: **WTI {kind} tweet** \u2014 skipped_no_data {streak} days in a row (latest ref={reference_date})")
        alerted = True
    if not dry:
        st[kind] = {"last_skipped": reference_date, "streak": streak, "alerted": alerted}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, indent=2) + "\n")
    return streak


def clear_no_data(kind, state_file, dry=False):
    """A day with data ends the streak."""
    p = no_data_state_path(state_file)
    if dry or not p.exists():
        return
    try:
        st = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        st = {}
    if st.pop(kind, None) is not None:
        p.write_text(json.dumps(st, indent=2) + "\n")


def check_closures(kind, reference_date, path, alert):
    """R3 AC 1b/1c: validate closures.csv before anything is built or posted. A missing file, unknown park,
    bad date or non-https source_url -> alert + False (caller exits 1). Never ignored silently."""
    sys.path.insert(0, str(BUILD_DAY.parent))
    import park_status
    try:
        park_status.load_closures(path or DEFAULT_CLOSURES)
        return True
    except park_status.ClosuresError as e:
        msg = f"closures_error kind={kind} ref={reference_date}: {e}; nothing posted"
        print(msg, file=sys.stderr)
        alert(f":x: **WTI {kind} tweet** \u2014 {msg}"[:1900])
        return False


# AC 16 (closed-row R4): freshness. 36h is final (R5 closed OQ-6).
FORECAST_MAX_AGE_H = 36
ET = "America/New_York"


def now():
    """Run time, tz-aware. Patched in tests."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def forecast_staleness(raw, D, at=None):
    """None if predicted content is fresh, else the reason. Fresh = generated_at within FORECAST_MAX_AGE_H of the run
    and its ET date on or after D - 1. Missing or unparseable generated_at counts as stale (fail safe)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    at = at or now()
    try:
        g = datetime.fromisoformat(str(json.loads(raw)["generated_at"]).replace("Z", "+00:00"))
        if g.tzinfo is None:
            raise ValueError("generated_at has no timezone")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
        return f"generated_at missing or unreadable ({e})"
    age_h = (at - g).total_seconds() / 3600
    if age_h > FORECAST_MAX_AGE_H:
        return f"generated_at {g.isoformat()} is {age_h:.1f}h old (max {FORECAST_MAX_AGE_H}h)"
    if g.astimezone(ZoneInfo(ET)).date() < date.fromisoformat(D) - timedelta(days=1):
        return f"generated_at {g.isoformat()} is before {D} minus 1 day (ET)"
    return None


def parquet_last_date(path):
    import pandas as pd
    h = pd.read_parquet(path, columns=["park_date"])
    return None if h.empty else pd.to_datetime(h.park_date).max().date().isoformat()


def observed_staleness(history, D):
    """None if wti.parquet's last date equals D, else the reason."""
    try:
        last = parquet_last_date(history)
    except Exception as e:  # noqa: BLE001 - unreadable parquet is stale data, not a post
        return f"wti.parquet unreadable ({e})"
    return None if last == D else f"wti.parquet last date {last} != target {D}"


def record_stale(kind, reference_date, why, alert, state_file=None, dry=False):
    """AC 16: skipped_stale -> log + one alert for this run; caller exits 0.
    AC 5b (R5): a stale day neither counts toward the no-data streak nor resets it. If it directly follows the
    streak's last day (a skip or an earlier stale day), it bridges the streak so the next day's skip continues it."""
    msg = f"skipped_stale kind={kind} ref={reference_date}: {why}; nothing posted"
    print(msg)
    alert(f":warning: **WTI {kind} tweet** \u2014 {msg}"[:1900])
    if dry or state_file is None:
        return
    p = no_data_state_path(state_file)
    try:
        st = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return
    cur = st.get(kind)
    if not cur:
        return
    prev = cur.get("bridged_to") or cur.get("last_skipped")
    if prev and date.fromisoformat(prev) + timedelta(days=1) == date.fromisoformat(reference_date):
        cur["bridged_to"] = reference_date
        p.write_text(json.dumps(st, indent=2) + "\n")


def check_park_hours(kind, reference_date, path, alert):
    """AC 2a: unreadable hours file -> every park unknown (silent on the card, never Closed) plus one
    park_hours_unreadable alert per run. Returns True if readable (or not configured)."""
    if not path:
        return True
    try:
        with open(path, newline="") as f:
            f.read(1)
        return True
    except (OSError, UnicodeDecodeError) as e:
        msg = f"park_hours_unreadable kind={kind} ref={reference_date}: {path}: {e}; every park unknown"
        print(msg, file=sys.stderr)
        alert(f":warning: **WTI {kind} tweet** \u2014 {msg}"[:1900])
        return False
