#!/usr/bin/env python3
"""WTI Observed Tweet — Posts yesterday's actual WTI as a reply to the prediction at 8:30 AM ET.

Timing rule (Fred, Oct 26 2026): for reference day D this runs at 8:30 AM ET on D+1, after D's observed
values are gathered that morning, and replies to D's forecast tweet (posted 4 PM ET on D-1, two days earlier).
It builds the TPCR-TWEET-V2 scorecard reply (design C) card + 7.1s MP4 locally from observed_D.json,
predicted_D.json and wilma's wti.parquet, and uploads the MP4 with alt text.
--dry-run builds everything, computes the reply target, and posts nothing.

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
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

import tweet_format
import tweet_note
import tweet_v1_legacy
import tweet_v2
from tweet_guard import enforce, preflight, status as guard_status

# --- Configuration ---
# Data source from env, no defaults: TPCR_SSH_HOST (ssh target), TPCR_CONTENT_DIR (Step 14 content dir there, or local when TPCR_SSH_HOST is unset).
# tweet_v2.env_required raises '<NAME> is not set' when one is needed (see operations/tweet_v2/README.md).
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
        ["ssh", tweet_v2.env_required("TPCR_SSH_HOST"), f"cat '{path}'"],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def upload_media(video_path, alt_text=None):
    """Upload video to Twitter via v1.1 chunked media upload, then attach alt text. Returns media_id or None."""
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
    # Fail closed: no tweet without alt text (metadata/create runs after FINALIZE, before the tweet).
    if not alt_text or not tweet_v2.set_alt_text(media_id, alt_text, auth, requests.post):
        print("ERROR: alt text not set on media; refusing to tweet", file=sys.stderr)
        return None
    return media_id


WDW_PARK_CODES = ("MK", "EP", "HS", "AK")


def _wdw_only(content):
    """Step 14 emits all parks; these tweets cover Walt Disney World only."""
    content = dict(content)
    content["parks"] = [p for p in content.get("parks", []) if p.get("park_code") in WDW_PARK_CODES]
    return content


def load_content(reference_date, content_dir=None):
    """Load D's observed content JSON (raw text + parsed WDW-only dict)."""
    path = f"observed_{reference_date}.json"
    raw = tweet_v2.read_content(path, content_dir, ssh_read_file)
    if raw:
        try:
            data = json.loads(raw)
            if data.get("status") in ("ready", "released"):
                return raw, _wdw_only(data)
            print(f"Content for {reference_date} is held: {data.get('held_reasons')}")
        except json.JSONDecodeError:
            print(f"Invalid JSON in {path}")
    return None, None


def save_state(tweet_id, reference_date, reply_to):
    """Record the observed tweet under observed[D] so the #468 idempotency check can see it."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tweet_note.locked(STATE_FILE):
        state = {}
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except json.JSONDecodeError:
                pass
        state.setdefault("observed", {})[reference_date] = {
            "tweet_id": tweet_id, "in_reply_to": reply_to,
            "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        cutoff = (date.today() - timedelta(days=14)).isoformat()
        state["observed"] = {k: v for k, v in state["observed"].items() if k >= cutoff}
        tweet_note._save(STATE_FILE, state)


def find_predicted_tweet_id(reference_date, state_file=None):
    """D's forecast tweet id: tweet_state.json["predicted"][D] (then last_predicted if it is for D).

    The observed reply for reference day D (8:30 AM ET on D+1) replies to the forecast tweet for the
    same D, which was posted at 4 PM ET on D-1, two days earlier.
    """
    tweet_id, how = tweet_v2.lookup_predicted_tweet(state_file or STATE_FILE, reference_date)
    print(f"Predicted tweet for ref={reference_date}: {tweet_id} ({how})")
    return tweet_id


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
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        return
    requests.post(
        f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
        json={"content": message},
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        timeout=15,
    )


def reply_old_format(reference_date, content, reply_to, how, fmt_why, dry, args, state_file, guard):
    """Old-format reply (pre-v2 video + text, now with alt text) to an old-format original. Overlap window only."""
    print(f"OLD FORMAT reply for ref={reference_date} to {reply_to} ({fmt_why})")
    work, tmp = tweet_v2.new_workdir(args.out)
    try:
        r = tweet_v1_legacy.build_reply(reference_date, content, dry=dry)
        print(f"\nTweet ({len(r['text'])} chars, ref={reference_date}, reply_to={reply_to}, format=old):\n{r['text']}\n")
        if dry:
            tweet_v2.write_manifest(work, {
                "would_post": True, "mode": "dry-run", "kind": "observed", "format": "old", "format_reason": fmt_why,
                "reference_date": reference_date,
                "posts_at": f"{(date.fromisoformat(reference_date) + timedelta(days=1)).isoformat()} 08:30 America/New_York",
                "reply_to_tweet_id": reply_to, "reply_to_lookup": how, "state_file": str(state_file),
                "tweet_text": r["text"], "media": "old Remotion DailyWTIAll video (rendered on the data host; skipped in dry run)",
                "alt_text": r["alt"], "guard_status": guard,
            })
            return 0
        if not r["mp4"]:
            print("SILENT: old-format video render failed; nothing posted", file=sys.stderr)
            post_discord(f":x: **WTI Observed Tweet** \u2014 ref={reference_date}: old-format video render failed, nothing posted")
            sys.exit(1)
        media_id = upload_media(r["mp4"], r["alt"])
        if not media_id:
            post_discord(f":x: **WTI Observed Tweet** \u2014 ref={reference_date}: video upload failed, nothing posted")
            sys.exit(1)
        tweet_id = post_tweet(r["text"], reply_to=reply_to, media_id=media_id)
        if not tweet_id:
            post_discord(":x: **WTI Observed Tweet** \u2014 Twitter API error, tweet failed")
            sys.exit(1)
        save_state(tweet_id, reference_date, reply_to)
        tweet_format.record(state_file, tweet_id, tweet_format.OLD, "observed", reference_date,
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        post_discord(f":white_check_mark: **WTI Observed Tweet posted** (reply, old format) \u2014 ref={reference_date}\n"
                     f"Replied to predicted tweet: {reply_to}\nhttps://x.com/DisneyStatsWhiz/status/{tweet_id}")
        print("Done.")
    finally:
        tweet_v2.cleanup(tmp)


def main(argv=None):
    global STATE_FILE
    args = tweet_v2.parse_args("observed", argv)
    dry = tweet_v2.dry_run_requested(args)
    if dry:
        tweet_v2.block_http_for_dry_run()
        print("DRY RUN: building only; nothing will be posted and no state will be written")
        guard = guard_status()
    else:
        load_env()
        enforce(preflight)
        guard = "enforced"

    reference_date = args.date or tweet_v2.default_reference_date("observed")
    state_file = STATE_FILE = Path(args.state_file) if args.state_file else STATE_FILE
    print(f"WTI Observed Tweet \u2014 {date.today().isoformat()} (ref={reference_date})")

    if not dry:
        for key in ["TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET",
                    "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET"]:
            if not os.environ.get(key):
                print(f"ERROR: {key} not set", file=sys.stderr)
                sys.exit(1)

    if not tweet_v2.check_closures("observed", reference_date, args.closures, print if dry else post_discord):
        sys.exit(1)  # R3: bad or missing closures.csv fails the run before any post

    raw, content = load_content(reference_date, args.content_dir)
    if not content:
        # AC 5a (closed-row R2): missing data is a skip, not a failure -> exit 0; alert after 2 days in a row
        tweet_v2.record_no_data("observed", reference_date, state_file, post_discord if not dry else print, dry)
        return 0
    alert = print if dry else post_discord
    tweet_v2.check_park_hours("observed", reference_date, args.park_hours, alert)  # AC 2a

    # Idempotency guard (#468): skip if already posted for this reference_date
    if state_file.exists():
        try:
            existing = json.loads(state_file.read_text())
            if existing.get("observed", {}).get(reference_date, {}).get("tweet_id"):
                print(f"Already posted observed tweet for {reference_date}, exiting.")
                return None
        except (json.JSONDecodeError, KeyError):
            pass

    # Reply target: D's forecast tweet (posted 4 PM ET on D-1)
    reply_to, how = tweet_v2.lookup_predicted_tweet(state_file, reference_date)
    print(f"Reply target for ref={reference_date}: {reply_to or 'none, staying silent'} ({how})")

    # TPCR-TWEET-RESTYLE-CUTOVER: a reply uses its original's format (config/tweet_format.json window).
    fmt, fmt_why = tweet_format.NEW, "no reply target"
    if reply_to:
        fmt, fmt_why = tweet_format.reply_format(reply_to, state_file, tweet_v2.now())
    print(f"Reply format for ref={reference_date}: {fmt} ({fmt_why})")
    if reply_to and fmt == tweet_format.OLD:
        return reply_old_format(reference_date, content, reply_to, how, fmt_why, dry, args, state_file, guard)
    if tweet_format.TIERS_ERROR:
        print(f"ERROR skipped_bad_config ref={reference_date}: {tweet_format.TIERS_ERROR}; nothing posted", file=sys.stderr)
        print(f"skipped_bad_config ref={reference_date}: nothing posted")
        try:
            (print if dry else post_discord)(
                f":warning: **WTI Observed Tweet** \u2014 ref={reference_date}: config {tweet_format.CONFIG} reply tiers "
                "(reply_tier_a/reply_tier_b) unset or invalid; the new-format reply was skipped, nothing posted."[:1900])
        except Exception as exc:  # no secrets: exception type only
            print(f"ERROR skipped_bad_config ref={reference_date}: Discord alert failed ({type(exc).__name__}); "
                  "run still skipped, exit 0", file=sys.stderr)
        return 0

    work, tmp = tweet_v2.new_workdir(args.out)
    try:
        arts, build_error = {}, None
        try:
            pred_raw = tweet_v2.read_content(f"predicted_{reference_date}.json", args.content_dir, ssh_read_file)
            if not pred_raw:
                raise RuntimeError(f"predicted_{reference_date}.json not found (needed for the forecast side of the scorecard)")
            history = tweet_v2.fetch_history(work, args.history)
            stale = tweet_v2.observed_staleness(history, reference_date)
            if stale:  # AC 16: the reply posts only when wti.parquet ends on D
                tweet_v2.record_stale("observed", reference_date, stale, alert, state_file, dry)
                return 0
            tweet_v2.clear_no_data("observed", state_file, dry)  # AC 5b: only a fresh data day ends the streak
            arts = tweet_v2.build(reference_date, work, history, pred_raw, raw, park_hours=args.park_hours, closures=args.closures)
            if "R" not in arts and not arts.get("skipped_all_closed"):
                raise RuntimeError("build produced no R (scorecard) MP4")
        except Exception as e:  # noqa: BLE001 - any build failure means no card, so the bot stays silent
            build_error = str(e)
            print(f"WARNING: v2 card build failed \u2014 {build_error}", file=sys.stderr)
        if arts.get("skipped_all_closed"):  # closed-row R1: every park closed -> no reply
            print(f"skipped_all_closed ref={reference_date}: every park closed; nothing posted")
            if dry:
                tweet_v2.write_manifest(work, {"would_post": False, "mode": "dry-run", "kind": "observed",
                                               "reference_date": reference_date, "result": "skipped_all_closed"})
            return 0 if dry else None
        tweet_text = tweet_v2.post_text("observed", arts)
        r = arts.get("R") if tweet_text else None
        print(f"\nTweet ({len(tweet_text or '')} chars, ref={reference_date}, reply_to={reply_to}):\n{tweet_text}\n")

        if dry:
            tweet_v2.write_manifest(work, {
                "would_post": bool(r and reply_to),
                "mode": "dry-run", "kind": "observed", "reference_date": reference_date,
                "posts_at": f"{(date.fromisoformat(reference_date) + timedelta(days=1)).isoformat()} 08:30 America/New_York",
                "reply_to_tweet_id": reply_to, "reply_to_lookup": how, "state_file": str(state_file),
                "tweet_text": tweet_text, "media": str(r["mp4"]) if r else None,
                "alt_text": r["alt"] if r else None, "build_error": build_error, "guard_status": guard,
            })
            return 0 if r else 1

        # Fred, Oct 26 2026: "Stay silent on missing-data days". No scorecard card or no forecast tweet
        # to reply to = no tweet (edge copy v2 E1/E5/E6/E7; default silent, no text-only fallback).
        if not r or not reply_to:
            why = build_error or f"no forecast tweet for {reference_date} ({how})"
            print(f"SILENT: no observed reply for {reference_date} ({why}); nothing posted")
            post_discord(f":mute: **WTI Observed Tweet** \u2014 ref={reference_date}: staying silent ({why})"[:1900])
            return None
        media_id = upload_media(r["mp4"], r["alt"])
        if not media_id:
            print("SILENT: video upload failed; nothing posted", file=sys.stderr)
            post_discord(f":x: **WTI Observed Tweet** \u2014 ref={reference_date}: video upload failed, nothing posted")
            sys.exit(1)

        tweet_id = post_tweet(tweet_text, reply_to=reply_to, media_id=media_id)
        if not tweet_id:
            post_discord(":x: **WTI Observed Tweet** \u2014 Twitter API error, tweet failed")
            sys.exit(1)
        save_state(tweet_id, reference_date, reply_to)
        tweet_format.record(state_file, tweet_id, tweet_format.NEW, "observed", reference_date,
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

        post_discord(
            f":white_check_mark: **WTI Observed Tweet posted** (reply) \u2014 ref={reference_date}"
            "\n"
            f"Replied to predicted tweet: {reply_to}\n"
            f"https://x.com/DisneyStatsWhiz/status/{tweet_id}"
        )
        print("Done.")
    finally:
        tweet_v2.cleanup(tmp)


if __name__ == "__main__":
    sys.exit(main())
