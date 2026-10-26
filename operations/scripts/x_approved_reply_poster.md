# x_approved_reply_poster.py

Posts an X reply as @HazeyDataFred **only** when the exact text has Fred's verbatim, hash-bound approval.
Spec: X-APPROVED-REPLY-POSTER-001. Dry run is the default. Live `--apply` on the Mini needs Fred's go via chief.

## Usage
    python3 operations/scripts/x_approved_reply_poster.py              # dry run: one read-only GET per entry, 0 POSTs
    python3 operations/scripts/x_approved_reply_poster.py --offline    # dry run, no HTTP at all (skips the mention check)
    python3 operations/scripts/x_approved_reply_poster.py --apply      # posts; at most 5 per run, >=30 s apart
Options: `--queue-dir` (default `operations/x_reply_queue/`), `--ledger` (default `operations/x_reply_ledger.jsonl`),
`--max N` (capped at 5). `--apply` with `--offline` is refused. Exit 1 if any `api_error`.

Secrets come only from the process env: `TWITTER_CONSUMER_KEY`, `TWITTER_CONSUMER_SECRET`, `TWITTER_ACCESS_TOKEN`,
`TWITTER_ACCESS_TOKEN_SECRET` (OAuth 1.0a, same as `wti_observed_tweet.py`). Unlike that script it does **not** read `~/.env`;
the caller (cron/launchd) must export them. `requests` and `requests_oauthlib` are imported only when a real client is built.

## Queue (written only by the approval flow)
One file per entry: `operations/x_reply_queue/<entry_id>.json`
`{entry_id, reply_to_id, text, text_sha256, image_path?, alt_text?, approval:{quote, approved_at, approved_by:"Fred", text_sha256}, status}`.
The poster opens these files read-only and never writes them. `image_path` may be relative to the queue dir.

## Ledger and status (OQ-2: status lives in the ledger)
`operations/x_reply_ledger.jsonl`, append-only, fsync'd per line, flock'd per run. Line:
`{entry_id, text_sha256, outcome, post_url?, http_status?, detail?, payload?, manual_url?, at}`.
Effective status = latest status-bearing line for (entry_id, text_sha256), else the queue's `status`:
`posted`/`skipped_duplicate` -> posted, `manual_not_mentioned` -> manual, `api_error` -> failed, except `api_error` with http_status 403 on the POST -> manual (AC 6a). `dry_run` and `rejected_*` don't change it.
Only entries whose effective status is `approved` are processed. Re-queueing a failed/manual entry needs new text (new hash) or a human
edit to the ledger. A torn last ledger line (crash mid-write) is ignored on read.

## Per-entry flow
1. Ledger already has `posted` for (entry_id, text_sha256) -> `skipped_duplicate`, 0 calls.
2. sha256 of the stored UTF-8 text (no trimming) must equal `text_sha256` and `approval.text_sha256`, else `rejected_hash_mismatch`.
3. Validation (quote non-empty, approved_by Fred, approved_at ISO-8601 with offset, digits-only reply_to_id, <=280 weighted chars,
   alt_text <=1000 and required with an image, image file exists), else `rejected_invalid`. 0 calls.
4. One GET of the target (skipped with `--offline`). Passes only if it mentions @HazeyDataFred or quotes a @HazeyDataFred post.
   Fail -> `manual_not_mentioned`, prints `https://x.com/i/status/<reply_to_id>` + text for Fred (chief relays it). GET error -> `api_error`, run stops.
5. Dry run -> `dry_run` with the exact payload.
6. `--apply`: GET the account's recent replies to the target; an identical text means a previous run crashed after posting, so it records `posted` without re-posting.
   Otherwise: image -> media upload -> `media/metadata/create` (alt text) -> `POST /2/tweets {text, reply:{in_reply_to_tweet_id}, media?}`.
   A 403 on the POST -> `api_error` (http_status 403), entry becomes manual and the manual URL + text are printed; no probing retries (AC 6a). Any other 4xx/5xx -> `api_error` (status only, no retry, entry becomes failed). 429 stops the run. Media failure means no POST.

Tests: `tests/test_x_approved_reply_poster.py` (stdlib unittest, mocked client): `unshare -rn python3 tests/test_x_approved_reply_poster.py`.
