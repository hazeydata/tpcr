#!/usr/bin/env python3
"""TPCR-DISCORD-GROWTH-001: offline demo that the Discord CTA reply threads under the OBSERVED post. No network.

Installs the dry-run HTTP block first (any requests call raises), swaps post_tweet for a fake that records its payload,
points the state file at a TemporaryDirectory, then runs the REAL wti_observed_tweet.post_discord_cta_reply and prints
JSON {flag, invite_url, reason, text, in_reply_to, calls: [payloads]}.

  python3 discord_cta_reply_demo.py --observed-id 111 --invite-url https://discord.gg/AbCdEf12 --today 2026-11-20 --flag 1

The config window is built around --today (so the date gate passes); use --window-first/--window-last to test outside it.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tweet_v2"))
try:
    import requests_oauthlib  # noqa: F401
except ImportError:
    sys.modules["requests_oauthlib"] = type(sys)("requests_oauthlib")
    sys.modules["requests_oauthlib"].OAuth1 = lambda *a: "auth"

import tweet_format  # noqa: E402
import tweet_v2  # noqa: E402
import wti_observed_tweet as wot  # noqa: E402

FAKE_CTA_ID = "9000000000000000001"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--observed-id", required=True, help="the OBSERVED tweet id the reply must thread under")
    ap.add_argument("--invite-url", default=None, help="discord_cta.invite_url (default: null)")
    ap.add_argument("--today", default="2026-11-20", help="America/Toronto posting date, YYYY-MM-DD")
    ap.add_argument("--flag", default="1", help="value of TPCR_DISCORD_CTA_REPLY (only '1' is on)")
    ap.add_argument("--window-first", help="window start (default: --today)")
    ap.add_argument("--window-last", help="window end (default: --today + 13 days)")
    args = ap.parse_args(argv)

    today = date.fromisoformat(args.today)
    first = args.window_first or today.isoformat()
    last = args.window_last or (today + timedelta(days=13)).isoformat()
    cta, _err = tweet_format.parse_discord_cta_checked(
        {"discord_cta": {"invite_url": args.invite_url, "first_post_date": first, "last_post_date": last}})
    calls, alerts = [], []

    def fake_post_tweet(text, reply_to=None, media_id=None):
        payload = {"text": text}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}
        if media_id:
            payload["media"] = {"media_ids": [media_id]}
        calls.append(payload)
        return FAKE_CTA_ID

    tweet_v2.block_http_for_dry_run()  # first: from here any real HTTP call raises
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as st:
        st.enter_context(mock.patch.dict(os.environ, {tweet_format.DISCORD_FLAG: args.flag}))
        st.enter_context(mock.patch.object(wot, "STATE_FILE", Path(tmp) / "tweet_state.json"))
        st.enter_context(mock.patch.object(wot, "cta_today", return_value=today))
        st.enter_context(mock.patch.object(wot, "post_tweet", side_effect=fake_post_tweet))
        st.enter_context(mock.patch.object(wot, "post_discord", side_effect=alerts.append))
        st.enter_context(mock.patch.object(tweet_format, "DISCORD_CTA", cta))
        st.enter_context(mock.patch.object(tweet_format, "DISCORD_CTA_ERROR", _err))
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            wot.post_discord_cta_reply(args.observed_id, today.isoformat())
        text, reason = tweet_format.discord_cta_reply(today)
        out = {"flag": args.flag, "invite_url": args.invite_url, "reason": reason, "text": text,
               "in_reply_to": calls[0]["reply"]["in_reply_to_tweet_id"] if calls else None, "calls": calls}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
