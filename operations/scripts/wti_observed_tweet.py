#!/usr/bin/env python3
"""WTI Observed Tweet — Posts yesterday's actual WTI as a reply to the prediction at 8:30 AM ET.

Reads observed content JSON from wilma-server and tweet_state.json for the
predicted tweet ID to reply to. If no predicted tweet exists, posts standalone.
Renders Remotion video on wilma, SCPs it locally, uploads to Twitter with text.
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
CONTENT_DIR = "/home/wilma/hazeydata/pipeline/content"
REMOTION_DIR = "/home/wilma/clawd-anthropic/remotion-experiments/remotion-tpcr"
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
    """Read a file from wilma-server via SSH."""
    result = subprocess.run(
        ["ssh", SSH_HOST, f"cat '{path}'"],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def render_video_on_wilma(target_date):
    """Trigger Remotion render on wilma-server and SCP the video back.

    Returns local path to the mp4, or None on failure.
    """
    remote_output = f"{REMOTION_DIR}/out/observed-wti-{target_date}.mp4"

    # Extract observed data and render
    print("Rendering video on wilma-server...")
    render_cmd = (
        f"cd {REMOTION_DIR} && "
        f"/home/wilma/theme-park-crowd-report/.venv/bin/python3 scripts/extract_daily_wti.py --mode observed && "
        f"npx remotion render DailyWTIAll '{remote_output}' --codec h264 --crf 18 --log=error"
    )
    result = subprocess.run(
        ["ssh", SSH_HOST, render_cmd],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"Render failed: {result.stderr}", file=sys.stderr)
        return None

    local_path = Path(tempfile.mkdtemp()) / f"observed-wti-{target_date}.mp4"
    scp_result = subprocess.run(
        ["scp", f"{SSH_HOST}:{remote_output}", str(local_path)],
        capture_output=True, text=True, timeout=30,
    )
    if scp_result.returncode != 0:
        print(f"SCP failed: {scp_result.stderr}", file=sys.stderr)
        return None

    print(f"Video downloaded: {local_path} ({local_path.stat().st_size} bytes)")
    return local_path


def upload_media(video_path):
    """Upload video to Twitter via v1.1 chunked media upload. Returns media_id or None."""
    auth = get_twitter_auth()
    file_size = video_path.stat().st_size

    # INIT
    resp = requests.post(TWITTER_UPLOAD_API, auth=auth, data={
        "command": "INIT",
        "total_bytes": file_size,
        "media_type": "video/mp4",
        "media_category": "tweet_video",
    }, timeout=30)
    if resp.status_code not in (200, 201, 202):
        print(f"Media INIT failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return None
    media_id = resp.json()["media_id_string"]

    # APPEND
    with open(video_path, "rb") as f:
        resp = requests.post(TWITTER_UPLOAD_API, auth=auth, data={
            "command": "APPEND",
            "media_id": media_id,
            "segment_index": 0,
        }, files={"media": f}, timeout=60)
    if resp.status_code not in (200, 204):
        print(f"Media APPEND failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return None

    # FINALIZE
    resp = requests.post(TWITTER_UPLOAD_API, auth=auth, data={
        "command": "FINALIZE",
        "media_id": media_id,
    }, timeout=30)
    if resp.status_code not in (200, 201):
        print(f"Media FINALIZE failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return None

    processing = resp.json().get("processing_info")
    while processing and processing.get("state") == "pending":
        wait = processing.get("check_after_secs", 5)
        print(f"Media processing... waiting {wait}s")
        time.sleep(wait)
        resp = requests.get(TWITTER_UPLOAD_API, auth=auth, params={
            "command": "STATUS",
            "media_id": media_id,
        }, timeout=15)
        processing = resp.json().get("processing_info")

    if processing and processing.get("state") == "failed":
        print(f"Media processing failed: {processing}", file=sys.stderr)
        return None

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


def load_predicted_tweet_id():
    """Load the predicted tweet ID from state file for reply threading."""
    if not STATE_FILE.exists():
        return None
    try:
        state = json.loads(STATE_FILE.read_text())
        return state.get("last_predicted", {}).get("tweet_id")
    except (json.JSONDecodeError, KeyError):
        return None


def format_tweet(content, is_reply=False):
    """Format the observed WTI content as a tweet."""
    target = content["target_date"]
    parks = content["parks"]

    parks_sorted = sorted(parks, key=lambda p: p["wti"])

    if is_reply:
        lines = [f"How it actually went — {target}:"]
    else:
        lines = [f"Walt Disney World Actual Wait Times — {target}"]

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
    """Return an emoji indicator for crowd level."""
    if wti < 15:
        return "\U0001f7e2"  # green
    elif wti < 25:
        return "\U0001f7e1"  # yellow
    elif wti < 35:
        return "\U0001f7e0"  # orange
    else:
        return "\U0001f534"  # red


def post_tweet(text, reply_to=None, media_id=None):
    """Post a tweet via Twitter API v2. Returns tweet_id or None."""
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
    """Post confirmation to Discord #wti-pipeline."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        return
    payload = json.dumps({"content": message})
    requests.post(
        f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
        data=payload,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )


def main():
    load_env()

    print(f"WTI Observed Tweet — {date.today().isoformat()}")

    for key in ["TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET",
                "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET"]:
        if not os.environ.get(key):
            print(f"ERROR: {key} not set", file=sys.stderr)
            sys.exit(1)

    # Load content
    content = load_content()
    if not content:
        print("No ready observed content found — skipping tweet")
        post_discord(":warning: **WTI Observed Tweet** — No ready content found, tweet skipped")
        return

    # Check for predicted tweet to reply to
    reply_to = load_predicted_tweet_id()
    is_reply = reply_to is not None

    # Format tweet
    tweet_text = format_tweet(content, is_reply=is_reply)
    print(f"\nTweet ({len(tweet_text)} chars, reply_to={reply_to}):\n{tweet_text}\n")

    # Render and upload video
    media_id = None
    video_path = render_video_on_wilma(content["target_date"])
    if video_path:
        media_id = upload_media(video_path)
        if not media_id:
            print("WARNING: Video upload failed — posting text-only tweet")
    else:
        print("WARNING: Video render failed — posting text-only tweet")

    # Post tweet
    tweet_id = post_tweet(tweet_text, reply_to=reply_to, media_id=media_id)
    if not tweet_id:
        post_discord(":x: **WTI Observed Tweet** — Twitter API error, tweet failed")
        sys.exit(1)

    # Discord confirmation
    mode = "reply thread" if is_reply else "standalone"
    post_discord(
        f":white_check_mark: **WTI Observed Tweet posted** ({mode}) — {content['target_date']}\n"
        f"https://x.com/DisneyStatsWhiz/status/{tweet_id}"
    )
    print("Done.")


if __name__ == "__main__":
    main()
