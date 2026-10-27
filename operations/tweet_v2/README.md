# Tweet v2: forecast card and observed reply

Locked design from tpcr-meta PR #12 (`cycles/2026-10-24-forecast-tweet-theme/`). Go-live cycle:
tpcr-meta `cycles/2026-10-26-tweet-v2-golive/`. Closed row: `cycles/2026-10-26-tweet-v2-closed-row/`.

## Timing rule
Fred: "we never post about today... always posting about tomorrow or yesterday".
For park day D:
- **Forecast** posts at 4 PM ET on D-1 (`operations/scripts/wti_predicted_tweet.py`, default D = today + 1).
- **Observed reply** posts at 8:30 AM ET on D+1 (`operations/scripts/wti_observed_tweet.py`, default D = today - 1),
  as a reply to D's forecast tweet. The tweet ID comes from `tweet_state.json` `predicted[D].tweet_id`
  (or `last_predicted` when its `reference_date` is D). No forecast tweet for D means no reply.

## Files
| File | Job |
|---|---|
| `cards.py` | Static card HTML + alt text + `*.meta.json` from Step 14 content (`predicted_D.json`, `observed_D.json`) and `wti.parquet` history (each park's own p25–p75 since Jan 2022). Refuses on wrong date, non-ready status or a missing park. |
| `build_day.sh` | Runs `cards.py`, screenshots PNGs (headless Chrome), renders the MP4s, checks the last frame equals the static PNG. No network. |
| `render_anim.sh`, `anim_forecast.js`, `anim_reply.js` | 30 fps frames -> H.264 MP4 (about 7 s). |
| `tweet_copy.py` | Tweet text, only from Writing's edge copy v2. A case with no approved copy returns None -> silent. |
| `park_status.py` | `open` / `closed` / `unknown` per park. `closed` only from the hand-filled `data/closures.csv`; `open` from `dimparkhours.csv` official rows; else `unknown`. |
| `headline_copy.py` | Every fallback headline ("All open parks normal", "<Park> normal", "All open parks within 2 of forecast", and the all-four lines). Final copy from Lindsay; `cards.py` takes them only from here. |

Scripts find `build_day.sh` via `TPCR_TWEET_V2_DIR` (default: `operations/tweet_v2` in this checkout).
Data host (no defaults; `tweet_v2.py`, `wti_observed_tweet.py` and `wti_predicted_tweet.py` stop with `<NAME> is not set` if one is needed and unset): `TPCR_SSH_HOST` (ssh target for the data host),
`TPCR_CONTENT_DIR` (Step 14 content dir there; not needed with `--content-dir`), `TPCR_WTI_PARQUET` (wti.parquet path there; not needed with `--history`).
Env for `build_day.sh`: `PY` (python with pandas + pyarrow), `PYIMG` (python with Pillow, default `PY`), `CHROME`, `FFMPEG`.

## Format transition (TPCR-TWEET-RESTYLE-CUTOVER, Fred Oct 27 2026)
A parallel transition. The window lives only in `config/tweet_format.json` (read by `operations/scripts/tweet_format.py`):
`first_new_run` 2026-10-28 16:00 ET, `old_retires` 2026-10-31 00:00 ET. Overlap: Oct 28, 29, 30.
- **Originals:** `wti_predicted_tweet.py` posts only the new format. A live run before `first_new_run` logs
  `skipped_before_cutover` and posts nothing (exit 0). Each post is tagged `new` in `predicted[D].format` and in the store.
- **Store:** `tweet_formats.json` next to `tweet_state.json` (`{tweet_id: {format, kind, reference_date, posted_at}}`), written
  atomically by both scripts after each post (replies too). A failed write is logged, never fatal.
- **Replies:** `wti_observed_tweet.py` reads the original's format (`tweet_format.reply_format`). Before `old_retires`: an original
  posted before `first_new_run` is old; otherwise its tag; untagged = by its `posted_at`; nothing known = old. From `old_retires` on:
  always new, with a `NOTE untagged original` log line when the original had no tag. A missing or corrupt store (or state file)
  never raises; it falls back as above. A naive `now` is taken as ET (America/Toronto).
- **Config failure:** a missing or corrupt `config/tweet_format.json` (unreadable, bad JSON, missing key, or a date without a UTC
  offset) does not crash either script at import. `tweet_format` logs `ERROR tweet_format: config ... unusable` to stderr, sets
  `CONFIG_ERROR`, and treats the old format as already retired: every original and reply is new, and the predicted run is never
  skipped as pre-cutover.
- **Old reply:** `operations/scripts/tweet_v1_legacy.py`: the pre-v2 Remotion `DailyWTIAll` video rendered on the data host
  (`TPCR_SSH_HOST`, `TPCR_REMOTION_DIR`; no defaults) and the pre-v2 reply text, now with alt text (each park's WTI and crowd label). A dry run renders nothing and
  writes `dry_run.json` with `"format": "old"`. Delete it (and this section's old branch) after Oct 31 2026.
- **Migration:** `python3 operations/scripts/tweet_format.py --migrate <tweet_state.json> [--dry-run]` tags every untagged tweet in
  the state file by `posted_at` (idempotent). Runs outside a checkout set `TPCR_TWEET_FORMAT_CONFIG` to the config path.

## Posting
1. #494 guard (`tweet_guard.enforce`) runs first on every live run; blocked = exit 3, nothing built or posted.
2. Validate `data/closures.csv` (or `--closures` / `TPCR_CLOSURES`). Missing file, wrong header, unknown park, bad date or a
   non-https `source_url` = exit 1 plus a Discord alert, before anything is built or posted.
3. No ready content = `skipped_no_data`: exit 0, nothing posted. Streak kept in `no_data_streak.json` next to `tweet_state.json`
   (per kind, consecutive reference days); one Discord alert when it reaches 2 days in a row. A day with fresh content clears it.
   AC 5b (R5): a `skipped_stale` day neither counts toward the streak nor resets it, so skip, stale, skip alerts on the second skip.
   Dry runs log it but write no streak state. Crashes and API errors (upload, alt text, tweet) still exit 1.
4. Freshness (AC 16): the forecast posts only if the content's `generated_at` is within 36h of the run (final, R5 closed OQ-6) and its
   ET date is on or after D - 1; missing `generated_at` counts as stale. The reply posts only if `wti.parquet`'s last `park_date`
   equals D. Otherwise `skipped_stale`: exit 0, one Discord alert, nothing built or posted.
5. Unreadable `--park-hours` file (AC 2a): every park `unknown` (silent, never Closed) and one `park_hours_unreadable` alert per run.
6. Build the card. No card (missing data, partial observed, bad content) = silent (Fred, Oct 26: "Stay silent on missing-data days"). No text-only fallback.
7. Upload the MP4 (v1.1 chunked INIT/APPEND/FINALIZE), then set alt text with `POST /1.1/media/metadata/create.json`
   `{media_id, alt_text:{text}}`. Alt text failure = no tweet (fail closed).
8. Post (v2 tweets), save `predicted[D]` / `observed[D]` in `tweet_state.json`. A second run for the same D posts nothing (#468).

## Edge cases (edge copy v2)
- Headline: `<Park> busy`, `<Park> quiet` or `All four parks normal`; no "today". Exact tie names every tied park in order MK, EP, HS, AK.
- `{lo}–{hi}` is each park's own p25–p75; only the axis label is the shared 10–35 scale.
- E1, E5: no post. E6: a closed park shows one `Closed` row, alt text "<Park> closed"; closed and unknown parks are left out of the
  headline pick and ties (a tie between open parks next to a closed park names both open parks, MK, EP, HS, AK order); with one park open the line names it. Every park closed: no forecast, no reply, log `skipped_all_closed`.
- Closure source (closed-row R3, Fred via chief): only `data/closures.csv`, header `park,date,source_url,note`, committed header-only and
  filled by hand in rare events (hurricane etc.). A park is closed only for a row with its code, the park-local date and an https
  `source_url`. `dimparkhours` is never used for closure: a missing official row stays `unknown`. So in production the closed path
  normally never triggers; it is tested on fixtures. Runs outside a checkout (the Mini copies) must set `TPCR_CLOSURES`.
- One-park-open and all-open fallback headlines: see `headline_copy.py`.
- E4 (late reply), E8 (over 280: cut order is in `tweet_copy.py`), E9 (weekly roll-up) is not built.

## Dry run
Both scripts take `--dry-run`: builds everything, blocks every HTTP call in-process, skips `~/.env` and credentials,
writes no state (no tweet state, no no-data streak). A dry run that reaches the build step writes `dry_run.json`
(would_post, posts_at, text, media, alt text, reply target, guard status; `result: skipped_all_closed` when every park is
closed). Runs that stop before the build write no `dry_run.json`; they only print the log line: `skipped_no_data`,
`skipped_stale` (forecast content, or wti.parquet not ending on D), "Already posted" (idempotency guard), and a bad
`closures.csv` (exit 1).

```
python3 operations/scripts/wti_predicted_tweet.py --dry-run --date 2026-10-28 --out /tmp/f28
python3 operations/scripts/wti_observed_tweet.py  --dry-run --date 2026-10-28 --out /tmp/o28
```
Optional: `--content-dir DIR` (local `predicted_D.json`/`observed_D.json` instead of wilma over ssh), `--history wti.parquet`,
`--state-file`, `--park-hours dimparkhours.csv`, `--closures closures.csv`.

## Tests
`python3 -m unittest -v tests.test_tweet_v2 tests.test_tweet_guard tests.test_scorecard tests.test_tweet_format tests.test_tweet_v2_samples`
(stubs `requests_oauthlib`; no network; use a python with pandas + pyarrow or the sample tests skip).
- `test_tweet_format`: the transition window, boundaries (incl. an original posted exactly at 16:00:00), tagged/untagged originals, naive clock, corrupt store, missing/corrupt config, old/new reply through the scripts.
- `test_tweet_v2_samples`: dry-renders every tpcr-meta#12 sample (forecast Sep 2-6, reply C Sep 2-6) from the same real data
  (`tests/fixtures/tweet_v2_samples/`) and compares: replies byte-identical; forecasts identical after Fred's three Oct 26 edits.
- The three older suites run with the window moved to the past (`setUpModule`), so they test the new-format path.
