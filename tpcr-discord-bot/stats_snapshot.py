#!/usr/bin/env python3
"""Append a daily snapshot of Discord guild member/presence counts to a CSV.

Stdlib only. The bot token is never printed or logged.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

API_URL = "https://discord.com/api/v10/guilds/{guild_id}?with_counts=true"
USER_AGENT = "tpcr-stats-snapshot (stdlib)"
TIMEZONE = "America/New_York"
FIELDNAMES = ["date", "approximate_member_count", "approximate_presence_count"]
DEFAULT_CSV = Path(__file__).parent / "data" / "stats.csv"
TOKEN_KEY = "DISCORD_BOT_TOKEN"


def _parse_env_file(path):
    """Parse KEY=VALUE lines; ignore blanks/comments, 'export ' prefix, quotes."""
    values = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def load_token(env_path=None):
    """Return the bot token from the env file, else os.environ; exit 1 if absent."""
    if env_path is None:
        env_path = os.path.expanduser("~/.env")
    token = _parse_env_file(env_path).get(TOKEN_KEY)
    if not token:
        token = os.environ.get(TOKEN_KEY)
    if not token:
        print(f"{TOKEN_KEY} not found in env file or environment", file=sys.stderr)
        sys.exit(1)
    return token


def fetch_counts(guild_id, token, opener=None):
    """Return (status, approximate_member_count, approximate_presence_count)."""
    if opener is None:
        opener = urllib.request.urlopen
    req = urllib.request.Request(
        API_URL.format(guild_id=guild_id),
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with opener(req, timeout=15) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None, None
    if status != 200:
        return status, None, None
    data = json.loads(body)
    return status, data.get("approximate_member_count"), data.get("approximate_presence_count")


def today_eastern(now=None):
    """Return today's date (YYYY-MM-DD) in America/New_York."""
    tz = ZoneInfo(TIMEZONE)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    return now.strftime("%Y-%m-%d")


def write_row(csv_path, row):
    """Upsert one row per date, preserving order, written atomically."""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if csv_path.exists() and csv_path.stat().st_size > 0:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("date") != row["date"]]
    rows.append({k: row[k] for k in FIELDNAMES})
    fd, tmp_name = tempfile.mkstemp(dir=csv_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_name, csv_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def main(argv=None, opener=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild-id", default=None)
    parser.add_argument("--csv", default=None)
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args(argv)

    guild_id = args.guild_id or os.environ.get("TPCR_DISCORD_GUILD_ID")
    if not guild_id:
        print("No guild id: pass --guild-id or set TPCR_DISCORD_GUILD_ID", file=sys.stderr)
        sys.exit(2)

    token = load_token(args.env_file) if args.env_file else load_token()

    try:
        status, members, presence = fetch_counts(guild_id, token, opener=opener)
    except Exception as exc:  # never echo the exception: it may embed request details
        print(f"Discord API request failed ({type(exc).__name__})", file=sys.stderr)
        sys.exit(1)
    if status != 200:
        print(f"Discord API returned HTTP {status}", file=sys.stderr)
        sys.exit(1)

    csv_path = Path(args.csv) if args.csv else DEFAULT_CSV
    row = {
        "date": today_eastern(now),
        "approximate_member_count": members,
        "approximate_presence_count": presence,
    }
    write_row(csv_path, row)
    print(f"Wrote {row['date']}: members={members} presence={presence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
