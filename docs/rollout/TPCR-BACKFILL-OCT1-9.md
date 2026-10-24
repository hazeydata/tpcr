# TPCR-BACKFILL-OCT1-9 — rollout note (for chief; not executed by the author)

`live_waits` in `tpcr_live.duckdb` has no rows for park dates 2026-10-01..09. The rows exist in `fact_tables/clean/2026-10/{park}_{date}.csv`. `scripts/backfill_live_waits_oct1_9.py` loads them using the scraper's own prep and an idempotent anti-join insert. It never writes CSVs and only reads under `fact_tables/clean`.

## Prerequisites

- Fred's separate go. Do not start without it.
- Pick a time away from the 6 AM ET pipeline / S11 window (e.g. mid-afternoon ET). The live run needs the DuckDB write lock.
- Nothing needs to be stopped or paused in advance. If the live run reports a lock error, retry once the scraper write finishes; do not kill the scraper.
- Below, `$OB` is the production output base and `$DB` is `$OB/tpcr_live.duckdb`.

## Steps

1. **Deploy.** After the PR is merged into `backup/mini-live-b4e6edd` (Fred's call), `git pull` that branch on the Mini. The script needs `src/live_waits_common.py`. Use the pipeline venv python for every command below.
2. **Backup.** Copy `$DB` to a dated file while no writer has it open (or use DuckDB `EXPORT DATABASE` to a dated directory):
   `cp "$DB" "$OB/tpcr_live.duckdb.bak-$(date +%Y%m%d)"`.
   Verify: sizes match (`ls -l`), and a read-only `SELECT count(*) FROM live_waits` on the backup equals the source. If the scraper wrote during the copy, redo it or use `EXPORT`.
3. **Dry run.** `python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --dry-run`
   Check:
   - `PARITY PASS` is printed (a. rows, b. entities, c. type mix, d. clean POSTED not in live all `ok`). On `PARITY FAIL` the exit code is 3 and nothing is loaded; stop and report the block to Fred.
   - Per-day `would insert` for 2026-10-01..09: `<N_01>` … `<N_09>`. Fill these in from the output. Each should be roughly a typical daily `live_waits` volume; a day far off is a reason to stop.
   - `already_present` is ~0 except for any partial scraper coverage. `derived_park_date_mismatch` is expected to be small (pre-6 AM rows in the next day's file).
   - Record: total would-insert `<N_total>`, `live_waits` rows before `<N_before>`.
4. **Live run.** `python scripts/backfill_live_waits_oct1_9.py --output-base "$OB"`
   Expect `inserted` per day == the dry run's `would insert` (a `WARNING ... inserted != expected` line means stop), and rows after == `<N_before>` + `<N_total>`.
5. **Verify** (read-only):
   `duckdb -readonly "$DB" "SELECT park_date, count(*) FROM live_waits WHERE park_date BETWEEN '2026-10-01' AND '2026-10-09' GROUP BY 1 ORDER BY 1"`
   Counts per day should match `<N_day>` (plus any `already_present`).
6. **Rerun the dry run** (step 3 command): `PARITY PASS` and `would insert=0` for every day. A real rerun also inserts 0.
7. **Rollback.** If anything is wrong: when no writer has the DB open, restore the backup over `$DB` (`cp "$OB/tpcr_live.duckdb.bak-<date>" "$DB"`), then verify the row count equals `<N_before>`. Rows written by the scraper after the backup are lost with a file restore. To avoid that, delete only the backfilled days instead, if Fred agrees (`DELETE FROM live_waits WHERE park_date BETWEEN '2026-10-01' AND '2026-10-09'` is only safe if the scraper had written none of them, which was the starting state).

## Oct 14-22 (S11 lock loss) — Fred ruled the loss not accepted, recover from clean; HOLD until OQ2 is explained and Fred's go

Same script, same parity gate and skip-existing anti-join. Select the days with `--start/--end` (inclusive park dates; mutually exclusive with `--glob`; both required; start <= end). Parity is checked on a day that is *not* loaded: use `--parity-date 2026-10-12` (R1: Oct 23 is not in clean yet); a range containing the parity date is refused (exit 2) before any file is read.

**Status: HOLD.** Do not run the live load until (a) OQ2 is explained (chief's gaps are 6.0k-8.1k/day for Oct 14-22, but control days Oct 12 and Oct 13 show gaps too, so the gap may be structural rather than lost batches) and (b) Fred gives his go. **chief runs the live load and the rollback on the Mini.** The dry run is read-only and may be run any time. Prerequisites, deploy and backup (steps 1-2 above) apply unchanged; take the backup first.

Dry run (read-only; parity runs first):
`python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --start 2026-10-14 --end 2026-10-22 --parity-date 2026-10-12 --dry-run`

Live run (after OQ2 and Fred's go; chief, on the Mini):
`python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --start 2026-10-14 --end 2026-10-22 --parity-date 2026-10-12`

Behavior, in order:
- **Missing days.** The run prints `MISSING DAYS: <d1>,<d2>` (comma-separated, no spaces) or `MISSING DAYS: none`: park dates in the range with no clean CSV. Those days are skipped and not recovered. If the list is non-empty and `--allow-missing` is not given, the run exits 4 before any DB access. With `--allow-missing` it continues with the present days (record the missing days as still broken). If every day is missing, exit 2.
- **Checksums.** Every target clean file and the `--parity-date` files are sha256-hashed at the start, re-hashed after parity (before any write) and again at the end (dry or live). Any change prints `CHECKSUM CHANGED: <file>` and exits 6 (before loading, if caught after parity). Success prints `CLEAN CHECKSUMS UNCHANGED (<n> files)`.
- **Parity.** `PARITY PASS`, else the per-day counts are still printed read-only under `DRY RUN (parity failed; counts only)`, nothing is written and the exit code is 3. Parity failure takes precedence over HOLD.
- **inserted_at gate.** If `live_waits` has no `inserted_at` column the run prints `HOLD: live_waits has no inserted_at column (OQ1); no live load` and exits 7 without writing (after the per-day counts). The column exists on the Mini (OQ1 closed); no schema change is made by this script.
- **Load key.** One value for the whole load: the current UTC time as a naive `TIMESTAMP` with microseconds (override: `--load-key 'YYYY-MM-DD HH:MM:SS.ffffff'`). It is logged as `LOAD KEY inserted_at=<value>` at load start and end; **copy it from the log**. Every inserted row gets it; scraper rows keep their own `inserted_at`. The run refuses (exit 2) if any `live_waits` row already has that key.
- **Per-day log line.** `<day>: files=.. raw=.. prepped=.. distinct_keys=.. already_present=X inserted=Y derived_park_date_mismatch=.. candidates=<rows read> dropped_null_key=<n> skipped_existing=<n>` (`would insert=` instead of `inserted=` in a dry run). `candidates` = rows read before prep; `skipped_existing` = keys already in `live_waits`.
- **Verify.** After the whole load, per day: `VERIFY PASS <day>` / `VERIFY FAIL <day>` with `live_waits` count for that park_date vs the distinct keys of all loaded clean rows whose derived park_date is that day, tolerance `--row-tol` (default 2%, abs diff / max(clean, 1)). Any FAIL exits 5; the data stays loaded and the log points at the rollback.

Expectations:
- `already_present` is **nonzero**: the scraper covered part of these days. Those keys are skipped.
- Per-day `would insert` is the rows lost to the lock (or a structural gap, see OQ2); record `<N_day>`, the total `<N_total>` and `<N_before>` from the dry run. Live `inserted` must equal it (a `WARNING ... inserted != expected` line means stop).
- Rerun the dry run afterwards: `would insert=0` for every day.

Verify (read-only):
`duckdb -readonly "$DB" "SELECT park_date, count(*) FROM live_waits WHERE park_date BETWEEN '2026-10-14' AND '2026-10-22' GROUP BY 1 ORDER BY 1"`

Rollback (by load key; chief, on the Mini, after Fred's go): `DELETE ... WHERE park_date BETWEEN ...` is NOT safe, scraper rows exist for these days. Use the key from the `LOAD KEY inserted_at=` log line:
1. `python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --rollback --load-key '<logged value>' --dry-run` prints `ROLLBACK DRY RUN: would delete <n> rows with inserted_at=<key>` and per-park_date counts (read-only). `<n>` must equal the load's inserted total.
2. Same command without `--dry-run` deletes exactly those rows in one transaction, prints `ROLLBACK: deleted <n> rows with inserted_at=<key>`, re-counts and prints `ROLLBACK VERIFY PASS: 0 rows remain with inserted_at=<key>` (exit 5 on a mismatch).
Rows with a NULL `inserted_at` or another key never match, so scraper rows of the same park dates stay. `--start/--end` may be added to restrict the rollback to a park-date range. Rollback does no parity check and reads no files; it exits 2 without `--load-key`, with a malformed key, or if `live_waits` has no `inserted_at`. A file restore of the backup remains the last resort (scraper rows written since the backup are lost).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (dry run or load) |
| 2 | Bad input: path outside `fact_tables/clean`, symlinked dir/file, no matching files (incl. every day missing), range containing `--parity-date`, missing DB, bad filename/park code, bad `--load-key`, load key already present in `live_waits`, `--rollback` without `--load-key` or without an `inserted_at` column, argument error |
| 3 | Source parity failed; nothing loaded (also in `--dry-run`; per-day counts are still printed) |
| 4 | Range has missing days (no clean CSV) and `--allow-missing` was not given; no DB access |
| 5 | Post-load `VERIFY FAIL` for at least one day (data stays loaded; roll back by key), or the rollback count check failed |
| 6 | A clean file's checksum changed during the run (`CHECKSUM CHANGED: <file>`); aborts before loading if caught after parity |
| 7 | HOLD: `live_waits` has no `inserted_at` column; no live load (dry run prints the counts first) |
