"""TPCR-TWEET-RESTYLE-CUTOVER: the OLD (pre-v2) observed reply, kept only for replies to old-format originals
during the overlap window (config/tweet_format.json; retires 00:00 ET Nov 1 2026). Delete after that date.

Visual and text are the pre-#532 wti_observed_tweet.py (main @ e25bd9c0e): the Remotion DailyWTIAll video rendered
on the data host, emoji-bar text. Two changes: the host and Remotion dir come from env (TPCR_SSH_HOST,
TPCR_REMOTION_DIR; no defaults), and the video now carries alt text (alt_text below), because every tweet does."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import tweet_v2

N = {"MK": "Magic Kingdom", "EP": "EPCOT", "HS": "Hollywood Studios", "AK": "Animal Kingdom"}


def crowd_bar(wti):
    return "\U0001f7e2" if wti < 15 else "\U0001f7e1" if wti < 25 else "\U0001f7e0" if wti < 35 else "\U0001f534"


def format_tweet(content):
    """Old reply text (is_reply=True branch of the pre-v2 script)."""
    lines = [f"How it actually went \u2014 {content['target_date']}:", ""]
    for p in sorted(content["parks"], key=lambda p: p["wti"]):
        lines.append(f"{crowd_bar(p['wti'])} {p['park_name']}: {p['wti']:.0f} min avg wait")
    return "\n".join(lines)


def alt_text(content):
    parks = "; ".join(f"{p.get('park_name') or N.get(p.get('park_code'), p.get('park_code'))} {p['wti']:.1f}"
                      + (f" ({p['wti_label']})" if p.get("wti_label") else "")
                      for p in sorted(content["parks"], key=lambda p: p["wti"]))
    return (f"Animated TPCR observed Wait Time Index (WTI) for Walt Disney World, {content['target_date']}: "
            + _labels_phrase(content["parks"]) + f". {parks}.")[:1000]


def _labels_phrase(parks):
    """Opening line promises crowd labels only for the parks that actually have one."""
    n = sum(1 for p in parks if p.get("wti_label"))
    if n and n == len(parks):
        return "each park's observed WTI with its crowd label"
    if n:
        return "each park's observed WTI, with a crowd label where one is available"
    return "each park's observed WTI"


def render_video(target_date, content):
    """Pre-v2 render: write data to Remotion on the data host, render DailyWTIAll, scp the MP4 back."""
    host, rdir = tweet_v2.env_required("TPCR_SSH_HOST"), tweet_v2.env_required("TPCR_REMOTION_DIR")
    remote = f"{rdir}/out/observed-wti-{target_date}.mp4"
    data = json.dumps({"date": target_date, "dateISO": target_date, "mode": "observed", "parks": [{
        "parkCode": p.get("park_code", ""), "parkName": p.get("park_name", ""), "parkEmoji": p.get("park_emoji", ""),
        "wtiScore": p.get("wti", 0), "wtiEmoji": p.get("wti_emoji", ""), "wtiLabel": p.get("wti_label", ""),
        "headlinerRide": p.get("headliner_ride", "Check the app!"), "headlinerWait": p.get("headliner_wait", 0),
        "bestRide": p.get("best_ride", "Check the app!"), "bestRideWait": p.get("best_ride_wait", 0)}
        for p in content.get("parks", [])]})
    w = subprocess.run(["ssh", host, f"cat > '{rdir}/public/daily-wti-data.json'"], input=data,
                       capture_output=True, text=True, timeout=15)
    if w.returncode != 0:
        print(f"Data write failed: {w.stderr}", file=sys.stderr); return None
    r = subprocess.run(["ssh", host, f"cd {rdir} && npx remotion render DailyWTIAll '{remote}' --codec h264 --crf 18 --log=error"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"Render failed: {r.stderr}", file=sys.stderr); return None
    local = Path(tempfile.mkdtemp(prefix="tpcr-v1-")) / f"observed-wti-{target_date}.mp4"
    c = subprocess.run(["scp", f"{host}:{remote}", str(local)], capture_output=True, text=True, timeout=30)
    if c.returncode != 0:
        print(f"SCP failed: {c.stderr}", file=sys.stderr); return None
    return local


def build_reply(target_date, content, dry=False):
    """{"text", "alt", "mp4"} for an old-format reply. A dry run renders nothing (no ssh)."""
    out = {"text": format_tweet(content), "alt": alt_text(content), "mp4": None}
    if not dry:
        try:
            out["mp4"] = render_video(target_date, content)
        except Exception as e:  # noqa: BLE001 - a failed render means no reply (caller alerts), not a crash
            print(f"Old-format render failed: {e}", file=sys.stderr)
    return out
