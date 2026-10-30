#!/usr/bin/env python3
"""WTI Observed Tweet — Posts yesterday's actual WTI as a reply to the prediction at 8:30 AM ET.

Every tweet has two dates:
  - tweet_date: when the tweet is posted (today)
  - reference_date: the date the statistics apply to (yesterday for observed)

The observed tweet replies to the PREDICTED tweet that shares the same
reference_date. For example, the observed tweet for March 30 replies to
the predicted tweet for March 30 (which was posted on March 29 at 4 PM).

Looks up the predicted tweet_id from tweet_state.json["predicted"][reference_date].
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

# --- Configuration ---
SSH_HOST = "wilma@192.168.2.75"
CONTENT_DIR = str(Path.home() / "hazeydata" / "pipeline" / "content")
REMOTION_DIR = str(Path.home() / "hazeydata" / "remotion-tpcr")
DRY_RUN = os.environ.get("TPCR_TWEET_DRY_RUN", "1") != "0"  # default SAFE: dry run unless explicitly 0
DRY_OUT = Path(os.environ.get("TPCR_TWEET_DRY_OUT", str(Path.home() / "hazeydata" / "reports" / "wti_daily" / "dryrun")))
STATE_FILE = Path.home() / "hazeydata" / "reports" / "wti_daily" / "tweet_state.json"
DISCORD_CHANNEL_ID = "1479351574177513576"
DISCORD_API = "https://discord.com/api/v10"
TWITTER_API = "https://api.twitter.com/2/tweets"
TWITTER_UPLOAD_API = "https://upload.twitter.com/1.1/media/upload.json"


def load_env():
    """Load tokens from ~/.env."""
    env_file = Path.home() / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip().strip('"')


def get_twitter_auth():
    """Build OAuth1 auth for Twitter API v2."""
    return OAuth1(
        os.environ["TWITTER_CONSUMER_KEY"],
        os.environ["TWITTER_CONSUMER_SECRET"],
        os.environ["TWITTER_ACCESS_TOKEN"],
        os.environ["TWITTER_ACCESS_TOKEN_SECRET"],
    )


def ssh_read_file(path):
    """Read a local content file (Mini; formerly SSH to wilma)."""
    p = Path(path)
    return p.read_text().strip() if p.exists() else None


def render_video_on_wilma(target_date, content):
    """Render the Remotion video locally. Returns the mp4 Path, or None on failure."""
    remote_output = f"{REMOTION_DIR}/out/observed-wti-{target_date}.mp4"
    data_path = f"{REMOTION_DIR}/public/daily-wti-data.json"

    parks_data = []
    for p in content.get("parks", []):
        parks_data.append({
            "parkCode": p.get("park_code", ""),
            "parkName": p.get("park_name", ""),
            "parkEmoji": p.get("park_emoji", ""),
            "wtiScore": p.get("wti", 0),
            "wtiEmoji": p.get("wti_emoji", ""),
            "wtiLabel": p.get("wti_label", ""),
            "headlinerRide": p.get("headliner_ride", "Check the app!"),
            "headlinerWait": p.get("headliner_wait", 0),
            "bestRide": p.get("best_ride", "Check the app!"),
            "bestRideWait": p.get("best_ride_wait", 0),
        })

    remotion_data = json.dumps({
        "date": target_date,
        "dateISO": target_date,
        "mode": "observed",
        "parks": parks_data,
    })

    print("Writing observed data to Remotion...")
    Path(data_path).write_text(remotion_data)

    print("Rendering video locally...")
    render_cmd = (
        f"cd {REMOTION_DIR} && "
        f"npx remotion render DailyWTIAll '{remote_output}' --codec h264 --crf 18 --log=error"
    )
    result = subprocess.run(
        ["bash", "-lc", render_cmd],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        print(f"Render failed: {result.stderr}", file=sys.stderr)
        return None

    local_path = Path(remote_output)
    if not local_path.exists():
        print(f"Render produced no file at {local_path}", file=sys.stderr)
        return None

    print(f"Video rendered: {local_path} ({local_path.stat().st_size} bytes)")
    return local_path


def upload_media(video_path):
    if DRY_RUN:
        print('[dry-run] skip upload_media')
        return 'DRYRUN-MEDIA'
    """Upload video to Twitter via v1.1 chunked media upload. Returns media_id or None."""
    auth = get_twitter_auth()
    file_size = video_path.stat().st_size

    resp = requests.post(TWITTER_UPLOAD_API, auth=auth, data={
        "command": "INIT", "total_bytes": file_size,
        "media_type": "video/mp4", "media_category": "tweet_video",
    }, timeout=30)
    if resp.status_code not in (200, 201, 202):
        print(f"Media INIT failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return None
    media_id = resp.json()["media_id_string"]

    with open(video_path, "rb") as f:
        resp = requests.post(TWITTER_UPLOAD_API, auth=auth, data={
            "command": "APPEND", "media_id": media_id, "segment_index": 0,
        }, files={"media": f}, timeout=60)
    if resp.status_code not in (200, 204):
        print(f"Media APPEND failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return None

    resp = requests.post(TWITTER_UPLOAD_API, auth=auth, data={
        "command": "FINALIZE", "media_id": media_id,
    }, timeout=30)
    if resp.status_code not in (200, 201):
        print(f"Media FINALIZE failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return None

    processing = resp.json().get("processing_info")
    elapsed = 0
    while processing and processing.get("state") != "succeeded":
        if processing.get("state") == "failed":
            print(f"Media processing failed: {processing}", file=sys.stderr)
            return None
        if elapsed > 60:
            print(f"Media processing timeout after {elapsed}s (state={processing.get('state')})", file=sys.stderr)
            return None
        wait = processing.get("check_after_secs", 5)
        print(f"Media processing... waiting {wait}s (state={processing.get('state')})")
        time.sleep(wait)
        elapsed += wait
        resp = requests.get(TWITTER_UPLOAD_API, auth=auth, params={
            "command": "STATUS", "media_id": media_id,
        }, timeout=15)
        processing = resp.json().get("processing_info")

    print(f"Media uploaded: {media_id}")
    return media_id


def load_content():
    """Load yesterday's observed content JSON from wilma."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    path = f"{CONTENT_DIR}/observed_{yesterday}.json"
    raw = ssh_read_file(path)
    if raw:
        try:
            data = json.loads(raw)
            if data.get("status") in ("ready", "released"):
                return data
            print(f"Content for {yesterday} is held: {data.get('held_reasons')}")
        except json.JSONDecodeError:
            print(f"Invalid JSON in {path}")

    return None


def find_predicted_tweet_id(reference_date):
    """Look up the predicted tweet ID for a specific reference_date.

    The observed tweet for reference_date X replies to the predicted
    tweet for that same reference_date X.

    State file structure:
      {
        "predicted": {
          "2026-03-31": {"tweet_id": "...", "posted_at": "..."},
          "2026-03-30": {"tweet_id": "...", "posted_at": "..."},
        },
        "last_predicted": {...}  // legacy fallback
      }
    """
    if not STATE_FILE.exists():
        print(f"No state file found at {STATE_FILE}")
        return None

    try:
        state = json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        print("State file is invalid JSON")
        return None

    # Primary lookup: by reference_date in the predicted dict
    predicted = state.get("predicted", {})
    if reference_date in predicted:
        tweet_id = predicted[reference_date].get("tweet_id")
        print(f"Found predicted tweet for ref={reference_date}: {tweet_id}")
        return tweet_id

    # Fallback: check last_predicted if reference_date matches
    last = state.get("last_predicted", {})
    last_ref = last.get("reference_date") or last.get("target_date")
    if last_ref == reference_date:
        tweet_id = last.get("tweet_id")
        print(f"Found predicted tweet via last_predicted fallback: {tweet_id}")
        return tweet_id

    print(f"No predicted tweet found for reference_date={reference_date}")
    return None


def format_tweet(content, is_reply=False):
    """Format the observed WTI content as a tweet."""
    target = content["target_date"]
    parks = content["parks"]
    parks_sorted = sorted(parks, key=lambda p: p["wti"])

    if is_reply:
        lines = [f"How it actually went \u2014 {target}:"]
    else:
        lines = [f"Walt Disney World Actual Wait Times \u2014 {target}"]

    lines.append("")
    for p in parks_sorted:
        wti = p["wti"]
        bar = crowd_bar(wti)
        lines.append(f"{bar} {p['park_name']}: {wti:.0f} min avg wait")

    if not is_reply:
        lines.append("")
        lines.append("#WDW #DisneyWorld #WaitTimes")

    return "\n".join(lines)


def crowd_bar(wti):
    if wti < 15:
        return "\U0001f7e2"
    elif wti < 25:
        return "\U0001f7e1"
    elif wti < 35:
        return "\U0001f7e0"
    else:
        return "\U0001f534"


def post_tweet(text, reply_to=None, media_id=None):
    if DRY_RUN:
        DRY_OUT.mkdir(parents=True, exist_ok=True)
        out = DRY_OUT / f'observed_{date.today().isoformat()}.txt'
        out.write_text(text + (f'\n\n[reply_to={reply_to}] [media={media_id}]\n'))
        print(f'[dry-run] tweet NOT posted; text saved to {out}')
        return None
    auth = get_twitter_auth()
    payload = {"text": text}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to}
    if media_id:
        payload["media"] = {"media_ids": [media_id]}

    resp = requests.post(TWITTER_API, json=payload, auth=auth, timeout=30)
    if resp.status_code in (200, 201):
        data = resp.json()
        tweet_id = data["data"]["id"]
        print(f"Tweet posted: {tweet_id}" + (f" (reply to {reply_to})" if reply_to else ""))
        return tweet_id
    else:
        print(f"Twitter API error {resp.status_code}: {resp.text}", file=sys.stderr)
        return None


def post_discord(message):
    if DRY_RUN:
        print('[dry-run] skip post_discord')
        return None
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        return
    requests.post(
        f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
        json={"content": message},
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        timeout=15,
    )


def main():
    load_env()

    print(f"WTI Observed Tweet \u2014 {date.today().isoformat()}")

    for key in ["TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET",
                "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET"]:
        if not os.environ.get(key):
            print(f"ERROR: {key} not set", file=sys.stderr)
            sys.exit(1)

    # Load content
    content = load_content()
    if not content:
        print("No ready observed content found \u2014 skipping tweet")
        post_discord(":warning: **WTI Observed Tweet** \u2014 No ready content found, tweet skipped")
        return

    # The reference_date is the date the stats apply to (yesterday)
    reference_date = content["target_date"]

    # Idempotency guard (#468): skip if already posted for this reference_date
    if STATE_FILE.exists():
        try:
            existing = json.loads(STATE_FILE.read_text())
            if existing.get("observed", {}).get(reference_date, {}).get("tweet_id"):
                print(f"Already posted observed tweet for {reference_date}, exiting.")
                return
        except (json.JSONDecodeError, KeyError):
            pass

    # Find the predicted tweet for THIS reference_date to reply to
    reply_to = find_predicted_tweet_id(reference_date)
    is_reply = reply_to is not None

    if is_reply:
        print(f"Will reply to predicted tweet {reply_to} (ref={reference_date})")
    else:
        print(f"No predicted tweet found for ref={reference_date} \u2014 posting standalone")

    # Format tweet
    tweet_text = format_tweet(content, is_reply=is_reply)
    print(f"\nTweet ({len(tweet_text)} chars, ref={reference_date}, reply_to={reply_to}):\n{tweet_text}\n")

    # Render and upload video
    media_id = None
    video_path = render_video_on_wilma(reference_date, content)
    if video_path:
        media_id = upload_media(video_path)
        if not media_id:
            print("WARNING: Video upload failed \u2014 posting text-only tweet")
    else:
        print("WARNING: Video render failed \u2014 posting text-only tweet")

    # Post tweet
    tweet_id = post_tweet(tweet_text, reply_to=reply_to, media_id=media_id)
    if DRY_RUN:
        print("Done (dry-run): nothing posted, no state saved.")
        return
    if not tweet_id:
        post_discord(":x: **WTI Observed Tweet** \u2014 Twitter API error, tweet failed")
        sys.exit(1)

    # Discord confirmation — include reference_date and reply target for debugging
    mode = "reply thread" if is_reply else "standalone"
    post_discord(
        f":white_check_mark: **WTI Observed Tweet posted** ({mode}) \u2014 ref={reference_date}\n"
        f"Replied to predicted tweet: {reply_to or 'none (standalone)'}\n"
        f"https://x.com/DisneyStatsWhiz/status/{tweet_id}"
    )
    print("Done.")


if __name__ == "__main__":
    main()
