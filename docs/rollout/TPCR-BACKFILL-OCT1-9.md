# TPCR-BACKFILL-OCT1-9 — rollout note (for chief)

**Status.** Prep approved by Fred via chief, 1:06 AM ET Oct 24 2026. **NOT run.** Running it needs Fred's separate go. chief runs it on the Mini.

`live_waits` in `tpcr_live.duckdb` has no rows for park dates 2026-10-01..09. The POSTED rows exist in `fact_tables/clean/2026-10/{park}_{date}.csv`. `scripts/backfill_live_waits_oct1_9.py` loads them once, using the scraper's own prep and an exact-key anti-join insert. The days are fixed (2026-10-01..09). PRIORITY and ACTUAL rows in the clean files are out of scope. The script never writes CSVs and only reads under `fact_tables/clean`.

## Prerequisites

- Fred's separate go. Do not start without it.
- Run it mid-afternoon ET, outside the protected windows below.
- While `--apply` holds the write connection, the scraper's live write retries, then parks its batches in `state/live_waits_pending/` (replayed later). Nothing is lost, but keep the run short and in the afternoon.
- Use the pipeline venv python. `$OB` is the production output base, `$DB` is `$OB/tpcr_live.duckdb`.
- Deploy: `git pull` the branch on the Mini (the script needs `src/live_waits_common.py`).
- **Backup.** Copy `$DB` while no writer has it open (or DuckDB `EXPORT DATABASE` to a dated directory): `cp "$DB" "$OB/tpcr_live.duckdb.bak-$(date +%Y%m%d)"`. Check that sizes match (`ls -l`) and that a read-only `SELECT count(*) FROM live_waits` on the backup equals the source. If the scraper wrote during the copy, redo it.

## Protected windows (America/New_York, half-open `[start, end)`)

| Window | Time | Source |
|--------|------|--------|
| ETL | 05:30-07:00 | Spec. Daily pipeline cron 6:00 AM ET (`docs/PIPELINE_STATE.md:16,35`); s11 holds the live DB write lock about 6:15 AM ET for about 110 s (`docs/PIPELINE_STATE.md:48`, `docs/DISCORD_BOT_DUCKDB_MIGRATION.md:414`). |
| replay | 04:30-04:40 | Daily staging replay is due at/after 04:30 ET with a 240 s timeout (`src/get_wait_times_from_queue_times.py:159-161`), plus the 300 s loop interval (`scripts/queue-times-loop.service:30`), so the latest end is 04:39. |

The ETL end time (07:00) is the spec's value; no end time is in the repo.

The script refuses to start inside a window (every mode, including dry runs and rollback) and checks again before each day's insert during `--apply`. Exit 8, `REFUSED: inside <name> window ...`. Days already inserted stay tagged with the load key and the rollback command is printed.

If the DB is locked (S11 or the scraper holds the write lock) the script exits 9 at once with `LOCKED: ...`, no retry. If a load key was already used it prints the rollback command.

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

`<day>: clean_posted=<n> excluded_PRIORITY=<n> excluded_ACTUAL=<n> excluded_other=<n> null_key=<n> dropped_nonpositive_or_unparseable=<n> duplicate_keys=<n> park_date_mismatch=<n> expected=<n> already_in_live=<n> would_insert=<n>`

(`inserted=` instead of `would_insert=` after `--apply`.) A reconcile line follows: `clean_posted - null_key - dropped_nonpositive_or_unparseable - duplicate_keys - park_date_mismatch = expected`.

- `clean_posted`: rows with `wait_time_type == 'POSTED'`. `excluded_*`: all other rows by type (`excluded_other` includes a blank type). They are never inserted.
- `null_key`: POSTED rows with an empty key column (also logged by the prep).
- `dropped_nonpositive_or_unparseable`: wait not > 0 or unparseable time (scraper prep).
- `duplicate_keys`: repeat of an `(entity_code, observed_at, wait_time_type)` key.
- `park_date_mismatch`: derived park date (6 AM rule) is not the file's date. These are not inserted, so nothing outside Oct 1-9 is written.
- `expected`: distinct keys eligible for that day. `already_in_live`: of those, already in `live_waits`. Expect 0.
- After `--apply`, per day `VERIFY PASS|FAIL <day>: live_posted=.. expected=..` (exact equality), then `LOAD KEY inserted_at=<value>` again and `CLEAN CHECKSUMS UNCHANGED (<n> files)`.

A `VERIFY FAIL` exits 5 and prints the exact rollback commands (dry run, then `--apply`). Data stays loaded until you roll back.

Clean files are sha256-hashed at start, before the first write and at the end. A change prints `CHECKSUM CHANGED: <file>` and exits 6.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (dry run, load, or rollback) |
| 2 | Bad input: path outside `fact_tables/clean`, symlinked dir/file, bad file name or park code, missing DB, bad `--load-key`, load key already present, `live_waits` has no `inserted_at`, `--rollback` without `--load-key` |
| 4 | A day 2026-10-01..09 has no clean CSV; no DB access |
| 5 | `VERIFY FAIL` for at least one day (data stays loaded), or the rollback count check failed |
| 6 | A clean file's checksum changed (`CHECKSUM CHANGED: <file>`) |
| 8 | Inside a protected window (at start, or before a day's insert) |
| 9 | DB locked (`LOCKED:`); no retry |
