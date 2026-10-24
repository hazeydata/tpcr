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

## Oct 14-22 (S11 lock loss) — Fred ruled the loss not accepted, recover from clean; loading still needs Fred's go

Same script, same parity gate and skip-existing anti-join. Select the days with `--start/--end` (inclusive park dates; mutually exclusive with `--glob`; both required; start <= end). Parity is checked on a day that is *not* loaded, so use `--parity-date 2026-10-23`; a range containing the parity date is refused (exit 2). Do not start the live run without Fred's go. Prerequisites, deploy and backup (steps 1-2 above) apply unchanged; take the backup first.

Dry run (read-only; parity runs first):
`python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --start 2026-10-14 --end 2026-10-22 --parity-date 2026-10-23 --dry-run`

Live run (after Fred's go):
`python scripts/backfill_live_waits_oct1_9.py --output-base "$OB" --start 2026-10-14 --end 2026-10-22 --parity-date 2026-10-23`

Expectations:
- `PARITY PASS` for 2026-10-23, else exit 3 and nothing is loaded.
- `already_present` is **nonzero**: the scraper covered part of these days. Those keys are skipped.
- Per-day `would insert` is the rows lost to the lock; record `<N_day>` and `<N_before>` from the dry run. Live `inserted` must equal it (a `WARNING ... inserted != expected` line means stop).

Verify (read-only):
`duckdb -readonly "$DB" "SELECT park_date, count(*) FROM live_waits WHERE park_date BETWEEN '2026-10-14' AND '2026-10-22' GROUP BY 1 ORDER BY 1"`
Per day: previous count + `would insert`. Rerun the dry run: `would insert=0` for every day.

Rollback: restore the backup file over `$DB` when no writer has it open, then check the row count equals `<N_before>` (scraper rows written after the backup are lost). **`DELETE ... WHERE park_date BETWEEN '2026-10-14' AND '2026-10-22'` is NOT safe here**: scraper rows already exist for these days and would be deleted too. Only a file restore, or a DELETE of exactly the keys the run inserted (e.g. `inserted_at >= <run start>` and `park_date` in range, checked against the `inserted` totals first), removes only the backfilled rows.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (dry run or load) |
| 2 | Bad input: path outside `fact_tables/clean`, symlinked dir/file, no matching files, missing DB, bad filename/park code, argument error |
| 3 | Source parity failed; nothing loaded (also in `--dry-run`) |
