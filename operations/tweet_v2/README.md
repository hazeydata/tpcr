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
Data host (no defaults; `tweet_v2.py` stops with a clear error if one is needed and unset): `TPCR_SSH_HOST` (ssh target for the data host),
`TPCR_CONTENT_DIR` (Step 14 content dir there; not needed with `--content-dir`), `TPCR_WTI_PARQUET` (wti.parquet path there; not needed with `--history`).
Env for `build_day.sh`: `PY` (python with pandas + pyarrow), `PYIMG` (python with Pillow, default `PY`), `CHROME`, `FFMPEG`.

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
`python3 -m unittest -v tests.test_tweet_v2 tests.test_tweet_guard tests.test_scorecard` (stubs `requests_oauthlib`; no network).
