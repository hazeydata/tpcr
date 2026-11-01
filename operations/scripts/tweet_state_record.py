"""One-shot, idempotent: record an already-posted tweet in tweet_state.json (rev7i, TPCR-TWEET-LOCAL).

Uses the jobs' own save_state (wti_observed_tweet.save_state / wti_predicted_tweet.save_state), so the entry has
exactly the format the job writes. posted_at comes from the tweet id's snowflake timestamp, so it is deterministic.
Posts nothing: every HTTP call is blocked before the jobs' modules are used.

  python3 operations/scripts/tweet_state_record.py --kind observed --date 2026-10-31 \\
      --tweet-id 2116886013188379069 [--reply-to 2116258879692571134] [--state-file PATH]

Re-running with the same id changes nothing (exit 0). A different id already recorded for that day = exit 1, untouched.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tweet_v2  # noqa: E402

TWITTER_EPOCH_MS = 1288834974657


def snowflake_posted_at(tweet_id):
    ms = (int(tweet_id) >> 22) + TWITTER_EPOCH_MS
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kind", choices=("observed", "predicted"), default="observed")
    ap.add_argument("--date", required=True, help="reference day D")
    ap.add_argument("--tweet-id", required=True)
    ap.add_argument("--reply-to", help="observed only: the forecast tweet replied to (default: predicted[D] from state)")
    ap.add_argument("--state-file", help="tweet_state.json (default: the jobs' STATE_FILE)")
    args = ap.parse_args(argv)
    date.fromisoformat(args.date)
    if not args.tweet_id.isdigit():
        print(f"ERROR: tweet id must be digits: {args.tweet_id!r}", file=sys.stderr)
        return 2
    tweet_v2.block_http_for_dry_run()  # nothing here may post

    if args.kind == "observed":
        import wti_observed_tweet as job
    else:
        import wti_predicted_tweet as job
    if args.state_file:
        job.STATE_FILE = Path(args.state_file)
    state_file = job.STATE_FILE

    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    cur = state.get(args.kind, {}).get(args.date)
    if cur and cur.get("tweet_id") == args.tweet_id:
        print(f"unchanged: {args.kind}[{args.date}] already has tweet_id={args.tweet_id} in {state_file}")
        return 0
    if cur and cur.get("tweet_id"):
        print(f"ERROR: {args.kind}[{args.date}] already has a different tweet_id={cur['tweet_id']}; not changed",
              file=sys.stderr)
        return 1

    posted_at = snowflake_posted_at(args.tweet_id)
    if args.kind == "observed":
        reply_to = args.reply_to or (cur or {}).get("in_reply_to") or \
            tweet_v2.lookup_predicted_tweet(state_file, args.date)[0]
        job.save_state(args.tweet_id, args.date, reply_to, posted_at=posted_at)
        job.tweet_format.record(state_file, args.tweet_id, job.tweet_format.NEW, "observed", args.date, posted_at)
    else:
        job.save_state(args.tweet_id, args.date, posted_at=posted_at)
    entry = json.loads(state_file.read_text())[args.kind][args.date]
    print(f"recorded: {args.kind}[{args.date}] = {json.dumps(entry)} in {state_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
