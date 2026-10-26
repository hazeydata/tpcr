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
| `park_status.py` | `open` / `closed` / `unknown` per park from `dimparkhours.csv` (official rows only). |

Scripts find `build_day.sh` via `TPCR_TWEET_V2_DIR` (default: `operations/tweet_v2` in this checkout).
Env for `build_day.sh`: `PY` (python with pandas + pyarrow), `PYIMG` (python with Pillow, default `PY`), `CHROME`, `FFMPEG`.

## Posting
1. #494 guard (`tweet_guard.enforce`) runs first on every live run; blocked = exit 3, nothing built or posted.
2. Build the card. No card (missing data, partial observed, bad content) = silent (Fred, Oct 26: "Stay silent on missing-data days"). No text-only fallback.
3. Upload the MP4 (v1.1 chunked INIT/APPEND/FINALIZE), then set alt text with `POST /1.1/media/metadata/create.json`
   `{media_id, alt_text:{text}}`. Alt text failure = no tweet (fail closed).
4. Post (v2 tweets), save `predicted[D]` / `observed[D]` in `tweet_state.json`. A second run for the same D posts nothing (#468).

## Edge cases (edge copy v2)
- Headline: `<Park> busy`, `<Park> quiet` or `All four parks normal`; no "today". Exact tie names every tied park in order MK, EP, HS, AK.
- `{lo}–{hi}` is each park's own p25–p75; only the axis label is the shared 10–35 scale.
- E1, E5: no post. E6: a closed park shows one `Closed` row, alt text "<Park> closed"; closed and unknown parks are left out of the
  headline pick and ties; with one park open the line names it. Every park closed: no forecast, no reply, log `skipped_all_closed`.
- Closed rows need `--park-hours` (or `TPCR_PARK_HOURS`) and a closure marker. OQ-1: `dimparkhours.csv` has no closure field today
  (`is_official` is True only when real hours exist), so `park_status.CLOSED_MARKER` is None and live runs never show Closed.
- E4 (late reply), E8 (over 280: cut order is in `tweet_copy.py`), E9 (weekly roll-up) is not built.

## Dry run
Both scripts take `--dry-run`: builds everything, blocks every HTTP call in-process, skips `~/.env` and credentials,
writes no state, and writes `dry_run.json` (would_post, posts_at, text, media, alt text, reply target, guard status).

```
python3 operations/scripts/wti_predicted_tweet.py --dry-run --date 2026-10-28 --out /tmp/f28
python3 operations/scripts/wti_observed_tweet.py  --dry-run --date 2026-10-28 --out /tmp/o28
```
Optional: `--content-dir DIR` (local `predicted_D.json`/`observed_D.json` instead of wilma over ssh), `--history wti.parquet`,
`--state-file`, `--park-hours dimparkhours.csv`.

## Tests
`python3 -m unittest -v tests.test_tweet_v2` (stubs `requests_oauthlib`; no network).
