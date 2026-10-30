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
| `render_anim.sh`, `anim_forecast.js`, `anim_reply.js` | 30 fps frames -> H.264 MP4 (about 7 s). Portable to macOS/BSD: the frame count is compared as a number (BSD `wc -l` pads with spaces); a short render fails with `missing frames: GOT of N`. Tested by `tests/test_render_anim.py` (stub Chrome/ffmpeg): an exact count passes; one frame too many, one too few after a successful render step, or a failed frame render all fail and write no MP4. |
| `tweet_copy.py` | New-format tweet text (see Tweet text below), from `F-D.meta.json` / `R-D.meta.json`. Over 280 (X weighted) returns None -> silent. |
| `park_status.py` | `open` / `closed` / `unknown` per park. `closed` only from the hand-filled `data/closures.csv`; `open` from `dimparkhours.csv` official rows; else `unknown`. |
| `headline_copy.py` | Every fallback headline ("All open parks normal", "<Park> normal", "All open parks within 3 of forecast", and the all-four lines). Final copy from Lindsay; `cards.py` takes them only from here. |

Scripts find `build_day.sh` via `TPCR_TWEET_V2_DIR` (default: `operations/tweet_v2` in this checkout).
Data host (no defaults; `tweet_v2.py`, `wti_observed_tweet.py` and `wti_predicted_tweet.py` stop with `<NAME> is not set` if one is needed and unset): `TPCR_SSH_HOST` (ssh target for the data host),
`TPCR_CONTENT_DIR` (Step 14 content dir there; not needed with `--content-dir`), `TPCR_WTI_PARQUET` (wti.parquet path there; not needed with `--history`).
Env for `build_day.sh`: `PY` (python with pandas + pyarrow), `PYIMG` (python with Pillow, default `PY`), `CHROME`, `FFMPEG`.

## Scheduling (Mac Mini launchd)
Two launchd jobs in `~/Library/LaunchAgents/` run the repo scripts from a separate clean checkout `~/tpcr-tweet`
(rulings, relayed by chief: Fred, Oct 30 2026 1:06 PM ET, "Repoint the tweet jobs to the repo's scripts"; 1:26 PM ET,
"Use a separate clean checkout at ~/tpcr-tweet for the tweet jobs"). They share the pipeline checkout's venv
`~/theme-park-crowd-report/.venv`; the pipeline checkout itself is not used by the tweet jobs:

| Label | When (ET) | ProgramArguments |
|---|---|---|
| `com.hazeydata.tpcr-tweet-observed` | 08:30 | `~/theme-park-crowd-report/.venv/bin/python ~/tpcr-tweet/operations/scripts/wti_observed_tweet.py` |
| `com.hazeydata.tpcr-tweet-predicted` | 16:00 | `~/theme-park-crowd-report/.venv/bin/python ~/tpcr-tweet/operations/scripts/wti_predicted_tweet.py` |

- Interpreter: `~/theme-park-crowd-report/.venv/bin/python` (Python 3.12, pandas + pyarrow). `WorkingDirectory` is `~/tpcr-tweet`; the scripts find
  `build_day.sh`, `config/` and `data/closures.csv` from their own path, so the cwd only matters for logs.
- Env names the scripts read (values come from the plist or `~/.env`, never from this repo): `TWITTER_CONSUMER_KEY`,
  `TWITTER_CONSUMER_SECRET`, `TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_TOKEN_SECRET`, `DISCORD_BOT_TOKEN`, `TPCR_SSH_HOST`,
  `TPCR_CONTENT_DIR`, `TPCR_WTI_PARQUET`, optional `TPCR_PARK_HOURS`, `TPCR_CLOSURES`, `TPCR_TWEET_V2_DIR`, `PY`, `PYIMG`,
  `CHROME`, `FFMPEG`, `TPCR_REMOTION_DIR` (old-format reply until Nov 1), `TPCR_TWEET_FORMAT_CONFIG`, the guard's `TPCR_TWEET_CHECKOUT` / `TPCR_TWEET_PYTHON`, and the dry-run switch `TPCR_TWEET_DRY_RUN`.
- `TPCR_TWEET_DRY_RUN` (compared as the exact string, no trimming or case folding; `--dry-run` always gives a dry run):

  | `TPCR_TWEET_DRY_RUN` | run |
  |---|---|
  | unset | live |
  | `0` | live |
  | anything else, including `1`, `` (empty), ` 0`, `false`, `true` | dry |

  The plists keep `TPCR_TWEET_DRY_RUN=1` until a manual dry run shows the new format.
- The old `~/hazeydata/scripts/wti_{observed,predicted}_tweet_local.py` are not in this repo and print coloured circles
  (`crowd_bar`). They are the rollback target only.
- Guard (`operations/scripts/tweet_guard.py`, config `config/tweet_guard.json`): a live run is allowed only if no crontab
  line runs `wti_*_tweet.py` and every loaded tweet label runs exactly the expected command, read from the `arguments`
  block of `launchctl print gui/<uid>/<label>`: `<python> <checkout>/operations/scripts/wti_observed_tweet.py` for
  `com.hazeydata.tpcr-tweet-observed`, `.../wti_predicted_tweet.py` for `com.hazeydata.tpcr-tweet-predicted`, and nothing
  else. The `arguments` block must appear exactly once and every argument must be a literal absolute path: launchd does not
  expand `~` and does not resolve relative paths against the guard's cwd, so `~/...`, `python` or
  `operations/scripts/...` block. The `program` line, if present, must be that same `<python>` (launchd execs `program`,
  not `argv[0]`); any other `program` blocks. `<checkout>` is `checkout` in the config (`~/tpcr-tweet`) or `TPCR_TWEET_CHECKOUT`; `<python>` is `python` in the
  config (`~/theme-park-crowd-report/.venv/bin/python`) or `TPCR_TWEET_PYTHON` (a relative value is taken inside the checkout).
  Paths are compared resolved: symlinked directories and `..` are resolved, but the python's own name is not followed, so a
  job running the venv's target interpreter directly does not match. Anything else exits 3 and posts nothing:
  `launchd_runs_local_script` (a `wti_*_tweet_local.py`), `launchd_unexpected_program` (another program or `program` line, wrong script or
  python, extra arguments, a repeated `arguments` block, a `~` or relative path), `launchd_job_no_arguments` (no `arguments` block), `launchctl_unreadable` / `crontab_unreadable`
  (including a non-zero exit), `legacy_cron_active`. A label that isn't loaded (manual run) is fine.
  `python3 operations/scripts/tweet_guard.py status` prints each label as `runs repo script`, `runs local script`,
  `runs unexpected program`, `no program arguments` or `not loaded`, the expected command, then `allowed_post: yes|no`;
  `check` exits 0 or 3. If launchd runs a checkout other than the default, the job (and any manual `check`) must have
  `TPCR_TWEET_CHECKOUT` set to it.

## Emoji scope
No coloured-circle emojis (🟢🟡🟠🔴🔵⚪⚫) appear in the new-format code: `operations/tweet_v2/`, `operations/scripts/tweet_v2.py`, `tweet_format.py`, `tweet_guard.py`, `tweet_note.py`. The legacy code still has them and is unchanged: `crowd_bar` in `wti_predicted_tweet.py:174` and `wti_observed_tweet.py:214` (neither file calls its own `format_tweet`), `tweet_v1_legacy.py:19` (used only by `reply_old_format`), and `scripts/post_{predicted,observed}_tweet.py`. The old-format reply path ends at `old_retires` (2026-11-01 00:00 ET in `config/tweet_format.json`, enforced in `tweet_format.reply_format`, `tweet_format.py:153`). Nothing in the repo retires `scripts/post_*_tweet.py`. The two tweet launchd jobs run the repo scripts (see Scheduling); the guard refuses a live post while either still points at a `_local` script. Whether `scripts/post_*_tweet.py` are scheduled anywhere else is not visible from the repo.

## Format transition (TPCR-TWEET-RESTYLE-CUTOVER, Fred Oct 27 2026)
A parallel transition. The window lives only in `config/tweet_format.json` (read by `operations/scripts/tweet_format.py`):
`first_new_run` 2026-10-29 16:00 ET, `old_retires` 2026-11-01 00:00 ET (both moved +24h from Oct 28 16:00 / Oct 31 00:00). First new-format reply: Oct 31 08:30 ET. Overlap: Oct 29, 30, 31.
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
  `CONFIG_ERROR`, and treats the old format as already retired: **every reply is new** (reason `config missing/invalid:
  everything new`), the one exception to "a reply uses its original's format". Because `first_new_run` is then unknown, the
  4 PM new-format original run is **skipped** (stdout `skipped_bad_config`, `ERROR ...` on stderr, nothing posted) until the
  tag store or `tweet_state.json` already records a new-format predicted original. So a broken config never posts a new
  original before Oct 29 16:00 ET. **Worst case:** if the config is already broken at Oct 29 16:00, that first new original
  and every later 4 PM original are skipped (a skip records nothing, so it never recovers by itself); skips continue until
  the config is fixed, and fixing the config resumes the next 4 PM run. Each skip sends one Discord #wti-pipeline alert naming
  the config path and that the forecast original was skipped (stderr only if `DISCORD_BOT_TOKEN` is unset). If the alert itself
  fails (timeout, connection error, HTTP non-2xx such as a Discord 500 or 403, any exception), the failure is logged to stderr
  (exception type only, e.g. `HTTPError`; never the token or response body) and the run still prints `skipped_bad_config`
  and exits 0. `post_discord` in `wti_predicted_tweet.py` raises on any non-2xx; every other predicted-script alert goes
  through `safe_alert`, which logs `ERROR: Discord alert failed (<type>); run outcome unchanged` and never changes the
  run's exit code or stops a post. Once a new
  original is recorded, a later broken config does not stop the forecast.
- **Old reply:** `operations/scripts/tweet_v1_legacy.py`: the pre-v2 Remotion `DailyWTIAll` video rendered on the data host
  (`TPCR_SSH_HOST`, `TPCR_REMOTION_DIR`; no defaults) and the pre-v2 reply text, now with alt text (each park's WTI and crowd label; the opening line mentions labels only for parks that have one). A dry run renders nothing and
  writes `dry_run.json` with `"format": "old"`. Delete it (and this section's old branch) after Nov 1 2026 00:00 ET.
- **Migration:** `python3 operations/scripts/tweet_format.py --migrate <tweet_state.json> [--dry-run]` tags every untagged tweet in
  the state file by `posted_at` (idempotent). Runs outside a checkout set `TPCR_TWEET_FORMAT_CONFIG` to the config path.

## Posting
1. Guard (`tweet_guard.enforce(tweet_guard.preflight)`, see Scheduling) runs first on every live run; blocked = exit 3, nothing built or posted.
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
- E4 (late reply), E9 (weekly roll-up) is not built.

## Tweet text (new format)
Forecast, three lines:
```
WDW Crowd FORECAST: Wed, Oct 28
Animal Kingdom busier than normal. Other parks normal.
#WDW #DisneyWorld #WaitTimes
```
Line 2 uses each park's card label, in order MK, EP, HS, AK: parks `busier than normal`, then `quieter than normal`, then `closed`,
then the rest. One normal park left is named (`Animal Kingdom normal.`), two or more are `Other parks normal.`; no busier, quieter or
closed park gives `All four parks normal.` Every park closed: no post.

Observed reply:
```
WDW Crowds OBSERVED: Wed, Oct 28
Three parks close to forecast. Animal Kingdom 6.7 quieter.

✅ Magic Kingdom: 21.5 (forecast 20.0)
✅ EPCOT: 18.0 (forecast 20.0)
✅ Hollywood Studios: 22.0 (forecast 20.0)
⏬ Animal Kingdom: 23.3 (forecast 30.0)

Lower WTI = shorter waits.
```
Each park line has a tier by |observed − forecast| (gap rounded to 0.1): ✅ if <= A; ⬇️ quieter / ⬆️ busier if A < miss <= B;
⏬ / ⏫ if > B. A and B are `reply_tier_a` / `reply_tier_b` in `config/tweet_format.json`, set by Fred on Oct 28 2026 to A = 3.0, B = 6.0; while unset or invalid
(need 0 < A < B) a new-format reply run logs `skipped_bad_config`, sends one Discord alert, posts nothing and exits 0 (old-format
replies are unaffected). The example above uses A = 2, B = 5 (with the shipped A = 3, B = 6, Animal Kingdom's 6.7 miss is still ⏬). Line 2 counts the open parks within A (`One park` / `Two parks` /
`Three parks close to forecast.`, `No park close to forecast.`, `All four` or `All open parks close to forecast.`) and names the
largest miss. The card headline's on-target rule (`cards.ON_TARGET`) is the same A, read from `reply_tier_a` (Fred, Oct 28 2026 2:57 PM ET), so text and card always agree; with `reply_tier_a` unset or invalid a reply card refuses to build (forecast cards are unaffected).
A closed park's row is `<Park>: closed`. No colour-coded circles. Every text is under 280 by X's weighted count (emoji 2, non-Latin 2; no URL = 23 rule, as the texts carry no URLs);
`tests/test_tweet_text.py` checks every label, tier and closure combination. A reply to an old-format original keeps the old text
(`operations/scripts/tweet_v1_legacy.py`).

## Dry run
Both scripts take `--dry-run` (or `TPCR_TWEET_DRY_RUN` set to anything but unset/`0`, see the table under Scheduling): builds everything, blocks every HTTP call in-process, skips `~/.env` and credentials,
writes no state (no tweet state, no no-data streak). A dry run that reaches the build step writes `dry_run.json`
(would_post, posts_at, text, media, alt text, reply target, guard status; `result: skipped_all_closed` when every park is
closed). Runs that stop before the build write no `dry_run.json`; they only print the log line: `skipped_no_data`,
`skipped_stale` (forecast content, or wti.parquet not ending on D), "Already posted" (idempotency guard), and a bad
`closures.csv` (exit 1).

```
python3 operations/scripts/wti_predicted_tweet.py --dry-run --date 2026-10-29 --out /tmp/f29
python3 operations/scripts/wti_observed_tweet.py  --dry-run --date 2026-10-29 --out /tmp/o29
```
Optional: `--content-dir DIR` (local `predicted_D.json`/`observed_D.json` instead of wilma over ssh), `--history wti.parquet`,
`--state-file`, `--park-hours dimparkhours.csv`, `--closures closures.csv`.

## Tests
`python3 -m unittest -v tests.test_tweet_v2 tests.test_tweet_guard tests.test_scorecard tests.test_tweet_format tests.test_tweet_v2_samples`
(stubs `requests_oauthlib`; no network; use a python with pandas + pyarrow or the sample tests skip).
- `test_tweet_format`: the transition window, boundaries (incl. an original posted exactly at 16:00:00), tagged/untagged originals, naive clock, corrupt store, missing/corrupt config, custom `store_name`, a lookup failure inside and after the window, bad config skipping the 4 PM original (Discord alert, alert failure incl. HTTP 500/4xx logged with exit 0, stderr-only without a token, state-only unblock, observed/reply tags don't unblock, fixed config resumes the next run), old-video alt text with missing labels, old/new reply through the scripts, `post_discord` raising on non-2xx without leaking the token or body, and `safe_alert` keeping the post and state when the success or park-hours alert fails.
- `test_tweet_v2_samples`: dry-renders every tpcr-meta#12 sample (forecast Sep 2-6, reply C Sep 2-6) from the same real data
  (`tests/fixtures/tweet_v2_samples/`) and compares: replies identical after Fred's Oct 28 on-target ruling (headline only); forecasts identical after Fred's three Oct 26 edits.
- The three older suites run with the window moved to the past (`setUpModule`), so they test the new-format path.
