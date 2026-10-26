# x_approved_reply_poster.py

Posts an X reply as @HazeyDataFred **only** when the exact text has Fred's verbatim, hash-bound approval.
Spec: X-APPROVED-REPLY-POSTER-001. Dry run is the default. Live `--apply` on the Mini needs Fred's go via chief.

## Usage
    python3 operations/scripts/x_approved_reply_poster.py              # dry run: one read-only GET per entry, 0 POSTs
    python3 operations/scripts/x_approved_reply_poster.py --offline    # dry run, no HTTP at all (skips the mention check)
    python3 operations/scripts/x_approved_reply_poster.py --apply      # posts; at most 5 per run, >=30 s apart
Options: `--queue-dir` (default `operations/x_reply_queue/`), `--ledger` (default `operations/x_reply_ledger.jsonl`),
`--max N` (capped at 5), `--verify-key PATH` (default: env `X_REPLY_VERIFY_KEY`; **required**, see AC 3c below).
`--apply` with `--offline` is refused. Exit 1 if any `api_error` or `refused_unsigned`, or if the run is refused outright
(queue dir mode, missing/bad verify key).

Secrets come only from the process env: `TWITTER_CONSUMER_KEY`, `TWITTER_CONSUMER_SECRET`, `TWITTER_ACCESS_TOKEN`,
`TWITTER_ACCESS_TOKEN_SECRET` (OAuth 1.0a, same as `wti_observed_tweet.py`). Unlike that script it does **not** read `~/.env`;
the caller (cron/launchd) must export them. `requests` and `requests_oauthlib` are imported only when a real client is built.
`cryptography` (Ed25519, in `requirements.txt`) is needed for the signature check.

## Queue (written only by the approval flow)
One file per entry: `operations/x_reply_queue/<entry_id>.json`
`{entry_id, reply_to_id, text, text_sha256, image_path?, alt_text?, approval:{quote, approved_at, approved_by:"Fred", text_sha256, signature}, status}`.
The poster opens these files read-only and never writes them. `image_path` may be relative to the queue dir.
AC 3a: the queue dir must be writable only by the approval-flow user (e.g. mode 0700). If it is group- or world-writable the
poster refuses to run at all (exits before touching the ledger or X), in dry run too.

## Signed approvals (spec R5 / AC 3c)
Every approval carries `approval.signature`: base64 Ed25519 over the canonical JSON (sorted keys, no spaces, UTF-8) of
`{v:"x-approved-reply-v1", entry_id, reply_to_id, text_sha256, quote, approved_by, approved_at, alt_text_sha256, image_sha256}`
(`alt_text_sha256` / `image_sha256` are null when absent; `image_sha256` hashes the image file bytes). So the signature binds
the approved text hash to its target post, Fred's quote/name/time, the alt text and the image.
- The **approval flow** signs with `operations/scripts/x_reply_sign_approval.py sign --key PRIV.pem ENTRY.json`. The private key
  path is given at runtime only (never env). The helper refuses a key that is group/world-accessible (must be 0600), inside the
  queue dir, or inside a git work tree, and refuses to sign an entry whose hashes don't match its text.
- The **poster** holds only the PUBLIC key (`--verify-key`). It refuses a private key, a non-Ed25519 key, or a key file inside the
  queue dir (whoever writes the queue must not be able to swap the verifier). With no usable key it refuses the whole run before
  the ledger is opened. The poster code has no signing capability.
- Unsigned, malformed or bad signatures -> ledger `refused_unsigned`, 0 calls, the run moves on to the next entry, exit 1.
  This applies in dry run and `--offline` too, so a dry run already proves the entries are signed.

### Key placement (Fred-gated setup step; nothing is installed by this PR)
1. On the approval-flow host, as the approval-flow user: `x_reply_sign_approval.py keygen --out ~/.config/x-reply-approval/ed25519.pem --pub ed25519_pub.pem`
   (refuses to overwrite; writes the private key 0600). Keep the private key there: not in the repo, not in the queue dir, not in
   any env, not on the poster host.
2. Copy only `ed25519_pub.pem` to the poster host, outside the repo and the queue dir, owned by a user the approval flow can't write
   as, mode 0444; point `X_REPLY_VERIFY_KEY` (or `--verify-key`) at it in the poster's cron/launchd job.
3. Rotation: generate a new pair, replace the public key, re-sign pending entries. A leaked private key means: delete it, rotate.
Until Fred does this, the poster refuses to run (no key), dry run included.

## Ledger and status (OQ-2: status lives in the ledger)
`operations/x_reply_ledger.jsonl`, append-only, fsync'd per line, flock'd per run. Line:
`outcome` is one of `dry_run`, `posted`, `skipped_duplicate`, `manual_not_mentioned`, `rejected_hash_mismatch`, `rejected_invalid`,
`refused_unsigned`, `api_error`. Fields:
`{entry_id, text_sha256, outcome, post_url?, http_status?, step?, detail?, payload?, manual_url?, queue_commit?, at}`.
`step` is the X call that failed (`GET tweet`, `GET me`, `GET recent replies`, `media upload`, `media metadata`, `POST tweet`).
AC 3b: every live (`--apply`) line — `posted`, recovered `posted`, and POST-stage `api_error` — carries `queue_commit`: the SHA of the last
local git commit touching that queue file (`git log -1 -- <file>`, no network), with `+dirty` if the file differs from it, or `uncommitted`.
Effective status = latest status-bearing line for (entry_id, text_sha256), else the queue's `status`:
`posted`/`skipped_duplicate` -> posted, `manual_not_mentioned` -> manual, `api_error` -> failed, except `api_error` with http_status 403 and `step` `POST tweet` -> manual (AC 6a). A 403 from any other step (mention GET, recent-replies GET, media) -> failed. `dry_run`, `rejected_*` and `refused_unsigned` don't change it (a re-signed entry is picked up next run).
Only entries whose effective status is `approved` are processed. Re-queueing a failed/manual entry needs new text (new hash) or a human
edit to the ledger. A torn last ledger line (crash mid-write) is ignored on read.

## Per-entry flow
Before any entry: queue dir mode check (AC 3a), then the verify key (AC 3c); either failing refuses the run before the ledger is opened.
1. Ledger already has `posted` for (entry_id, text_sha256) -> `skipped_duplicate`, 0 calls. Entries whose effective status isn't `approved` are skipped silently.
2. The approval must be a JSON object, e.g. `"approval": "yes"` is `rejected_invalid` and the run continues with the next entry. 0 calls.
3. sha256 of the stored UTF-8 text (no trimming) must equal `text_sha256` and `approval.text_sha256`, else `rejected_hash_mismatch`. 0 calls.
4. Ed25519 signature check (AC 3c), else `refused_unsigned`. 0 calls.
5. Validation (quote non-empty, approved_by Fred, approved_at ISO-8601 with offset, digits-only reply_to_id, <=280 weighted chars,
   alt_text <=1000 and required with an image, image file exists), else `rejected_invalid`. 0 calls.
6. One GET of the target (skipped with `--offline`). Passes only if it mentions @HazeyDataFred or quotes a @HazeyDataFred post.
   Fail -> `manual_not_mentioned`, prints `https://x.com/i/status/<reply_to_id>` + text for Fred (chief relays it). GET error -> `api_error`, run stops.
7. Dry run -> `dry_run` with the exact payload.
8. `--apply`: GET the account's recent replies to the target; an identical text means a previous run crashed after posting, so it records `posted` without re-posting.
   Otherwise: image -> media upload -> `media/metadata/create` (alt text) -> `POST /2/tweets {text, reply:{in_reply_to_tweet_id}, media?}`.
   A 403 on the POST (only) -> `api_error` (http_status 403), entry becomes manual and the manual URL + text are printed; no probing retries (AC 6a). Any other 4xx/5xx -> `api_error` (status only, no retry, entry becomes failed). 429 stops the run. Media failure means no POST.

Tests: `tests/test_x_approved_reply_poster.py` (stdlib unittest, mocked client, throwaway Ed25519 keys generated in a temp dir): `unshare -rn python3 tests/test_x_approved_reply_poster.py`.
