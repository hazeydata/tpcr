# TPCR-BACKFILL-OCT1-9 — rollout note (for chief)

**Status.** Prep approved by Fred via chief, 1:06 AM ET Oct 24 2026. **NOT run.** Running it needs Fred's separate go. chief runs it on the Mini.

`live_waits` in `tpcr_live.duckdb` has no rows for park dates 2026-10-01..09. The POSTED rows exist in `fact_tables/clean/2026-10/{park}_{date}.csv`. `scripts/backfill_live_waits_oct1_9.py` loads them once, using the scraper's own prep and an exact-key anti-join insert. The days are fixed (2026-10-01..09). PRIORITY and ACTUAL rows in the clean files are out of scope. The script never writes CSVs and only reads under `fact_tables/clean`.

## Prerequisites

- Fred's separate go. Do not start without it.
- Run it mid-afternoon ET, outside the protected windows below.
- While `--apply` holds the write connection, the scraper's live write retries, then parks its batches in `state/live_waits_pending/` (replayed later). That retry/park behavior is #524 (TPCR-S11-LOCK-001) code; it is only on the Mini once #524 has passed its own gate and merged (see Deploy). Keep the run short and in the afternoon.
- Use the pipeline venv python. `$OB` is the production output base, `$DB` is `$OB/tpcr_live.duckdb`.
- Deploy: only gated, merged commits; never deploy an unmerged PR branch. This branch contains #524's scraper changes (merge commits `a1c86020a`, `355a7610c`), so: (1) #524 must pass its gate and merge onto `backup/mini-live-b4e6edd` first; (2) this PR then passes its gate and merges there; (3) on the Mini, with `LIVE=backup/mini-live-b4e6edd` and `MERGED_SHA=<full 40-char merge SHA of this PR on $LIVE>`: `git fetch origin "$LIVE" && [ "$(git rev-parse "origin/$LIVE")" = "$MERGED_SHA" ] && [ "$(git rev-parse --abbrev-ref HEAD)" = "$LIVE" ] && [ -z "$(git status --porcelain --untracked-files=no)" ] && git merge-base --is-ancestor HEAD "$MERGED_SHA" && git merge --ff-only "$MERGED_SHA" && [ "$(git rev-parse HEAD)" = "$MERGED_SHA" ]`. If any check fails, stop and report. If #524 is not merged and gated, do not deploy (the script needs `src/live_waits_common.py`, which ships with it).
- **Backup.** Copy `$DB` while no writer has it open (or DuckDB `EXPORT DATABASE` to a dated directory): `cp "$DB" "$OB/tpcr_live.duckdb.bak-$(date +%Y%m%d)"`. Check that sizes match (`ls -l`) and that a read-only `SELECT count(*) FROM live_waits` on the backup equals the source. If the scraper wrote during the copy, redo it.

## Protected windows (America/New_York, half-open `[start, end)`)

| Window | Time | Source |
|--------|------|--------|
| ETL | 05:30-09:00 | Spec R1 (OQ1). The ETL starts 06:00 (`docs/PIPELINE_STATE.md:16`), so 05:30 is a 30-min margin. There is no end in the repo; 09:00 covers the 90-min crowd-report wait. |
| replay | 04:25-04:45 | Spec R1 (OQ1). The daily staging replay runs from 04:30 to 04:40 (`src/get_wait_times_from_queue_times.py:159-161`), plus 5-min margins. |

S11 (about 06:15 ET, about 110 s; `docs/PIPELINE_STATE.md:48`) falls inside the ETL window, so no marker is needed.

The script refuses to start inside a window (every mode, including dry runs and rollback) and checks again before each day's insert during `--apply`. Exit 8, `REFUSED: inside <name> window ...`. Days already inserted stay tagged with the load key and the rollback command is printed.

If the DB is locked (S11 or the scraper holds the write lock) the script exits 9 at once (exit 9, no retry; S11 needs no marker) with `LOCKED: ...`, no retry. If a load key was already used it prints the rollback command.

## Commands

1. **Dry run** (default; DB opened read-only, writes nothing):
   `python scripts/backfill_live_waits_oct1_9.py --output-base "$OB"`
2. **Load** (needs Fred's go):
   `python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --apply`
   Copy the `LOAD KEY inserted_at=<value>` line.
3. **Verify** (read-only):
   `duckdb -readonly "$DB" "SELECT park_date, count(*) FROM live_waits WHERE wait_time_type = 'POSTED' AND park_date BETWEEN '2026-10-01' AND '2026-10-09' GROUP BY 1 ORDER BY 1"`
   Each count must equal that day's `expected=`.
4. **Second dry run:** the step 1 command. `would_insert=0` for every day.
5. **Rollback dry run:**
   `python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --rollback --load-key '<logged value>'`
   Prints `ROLLBACK DRY RUN: would delete <n> rows with inserted_at=<key>` plus per-day counts. `<n>` must equal the load's inserted total.
6. **Rollback:** the same command plus `--apply`. Deletes exactly the rows with that `inserted_at` in one transaction, then prints `ROLLBACK VERIFY PASS`. Rows with a NULL or different `inserted_at` (the scraper's) never match.

Options: `--output-base`, `--db` (default `$OB/tpcr_live.duckdb`), `--apply`, `--load-key 'YYYY-MM-DD HH:MM:SS.ffffff'` (default now, UTC; required by `--rollback`), `--rollback`. A load key already present in `live_waits` is refused.

Last resort: restore the backup file over `$DB` while no writer has it open. Scraper rows written since the backup are lost.

## Expected output

First `MISSING DAYS: none` (else exit 4 with the list, before any DB access). Then one line per day:

`<day>: clean_posted=<n> excluded_PRIORITY=<n> excluded_ACTUAL=<n> excluded_other=<n> null_key=<n> bad_observed_at=<n> wait_nonpositive=<n> duplicate_keys=<n> park_date_mismatch=<n> expected=<n> already_in_live=<n> would_insert=<n>`

(`inserted=` instead of `would_insert=` after `--apply`.) Before the DB is opened, a reconcile line per day is printed (`<day> reconcile: clean_posted=<n> == expected=<n> + drops=<n> (null_key=.. + bad_observed_at=.. + wait_nonpositive=.. + duplicate_keys=.. + park_date_mismatch=..) OK`; `!=` and `MISMATCH` instead of `==` and `OK` when it fails). `clean_posted == expected + sum(drops)` must hold exactly for every day. If it does not, the script prints `MISMATCH` and `RECONCILE FAILED`, exits 7, and writes nothing. The five drop classes are independent: each is counted with its own predicate, in the order below, on the rows still remaining, and none is a remainder. A POSTED row that fits no class (e.g. a non-numeric `wait_time_minutes` such as `abc`, which the prep drops but no class counts) therefore makes the reconcile fail: exit 7, nothing written. That is intended; fix or investigate the clean file.

- `clean_posted`: rows with `wait_time_type == 'POSTED'`. `excluded_*`: all other rows by type (`excluded_other` includes a blank type). They are never inserted.
- `null_key`: POSTED rows with a null or blank `entity_code`, `observed_at` or `wait_time_type` (also logged by the prep).
- `bad_observed_at`: key present but `observed_at` unparseable.
- `wait_nonpositive`: `wait_time_minutes` parses as a number and is <= 0. A non-numeric wait is in no class (see reconcile above).
- `duplicate_keys`: repeat of an `(entity_code, observed_at, wait_time_type)` key.
- `park_date_mismatch`: derived park date (6 AM rule) is not the file's date. These are not inserted, so nothing outside Oct 1-9 is written.
- `expected`: clean POSTED minus the five drop classes above (and nothing else). VERIFY equality is checked against it.
- `expected` rows are the distinct keys eligible for that day. `already_in_live`: of those, already in `live_waits`. Expect 0.
- After `--apply`, per day `VERIFY PASS|FAIL <day>: live_posted=.. expected=..` (exact equality), then `LOAD KEY inserted_at=<value>` again and `CLEAN CHECKSUMS UNCHANGED (<n> files)`.

The dry run (the default) opens the DB read-only. Symlinks: the run refuses with exit 2 and writes nothing if `fact_tables` or `clean` is a symlink, if any symlink (file or dir, at any depth, on or off the read path) exists anywhere under `clean`, or, per Spec R3a, because any OSError during the pre-open scan exits 2 (the scan covers the symlink walk, every per-entry stat/is_symlink/access check (an unreadable or unsearchable dir and an unreadable file also refuse), listing, hashing and reading the clean files; the refusal names the path and nothing is written), or if anything resolves outside `clean` (the spec is silent on symlinks; refusing is the safer choice). The walk runs before any DB open.

A `VERIFY FAIL` exits 5 and prints the exact rollback commands (dry run, then `--apply`). Data stays loaded until you roll back.

Clean files are sha256-hashed at start, before the first write and at the end. A change prints `CHECKSUM CHANGED: <file>` and exits 6.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (dry run, load, or rollback) |
| 2 | Bad input: path outside `fact_tables/clean`, symlinked `fact_tables`/`clean`, any symlink (file or dir, any depth) under `clean`, Spec R3a: any OSError during the pre-open scan exits 2 (the scan covers the symlink walk, every per-entry stat/is_symlink/access check (an unreadable or unsearchable dir and an unreadable file also refuse), listing, hashing and reading the clean files; the refusal names the path and nothing is written), bad file name or park code, missing DB, bad `--load-key`, load key already present, `live_waits` has no `inserted_at`, `--rollback` without `--load-key` |
| 4 | A day 2026-10-01..09 has no clean CSV; no DB access |
| 5 | `VERIFY FAIL` for at least one day (data stays loaded), or the rollback count check failed |
| 6 | A clean file's checksum changed (`CHECKSUM CHANGED: <file>`) |
| 7 | Reconcile mismatch: `clean_posted != expected + sum(drops)` for at least one day; nothing written |
| 8 | Inside a protected window (at start, or before a day's insert) |
| 9 | DB locked (`LOCKED:`); no retry |
