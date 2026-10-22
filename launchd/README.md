# launchd jobs (Mac Mini)

| Label | When | What |
|-------|------|------|
| `com.hazeydata.tpcr-crowd-report` | 07:00 daily | `daily_report.py --wait-for-pipeline 90` — posts the Daily Crowd Report to #crowd-reports |
| `com.hazeydata.tpcr-crowd-report-watchdog` | 09:00 daily | `scripts/tpcr_crowd_report_watchdog.py --check-only --no-post` — verifies today's post landed; no healing, never posts (posting needs explicit `--post`) |

Plists use the placeholder `__HOME__` (launchd does not expand `~`); rollout
substitutes it with `$HOME`. Secrets (`DISCORD_BOT_TOKEN`) are not in the plists;
the scripts read `~/.env`. Both plists set
`TPCR_DATA_ROOT=__HOME__/hazeydata/pipeline` (matching the log paths); logs go to
`<root>/logs/`.

## Why 07:00 plus a wait

The pipeline (`pipeline/pipeline.py`) starts at 06:00 and typically takes ~10–12 min,
but `s01_sync` may time out after up to 30 min. At the end of a full run it writes
`<root>/logs/pipeline_metrics_<YYYY-MM-DD>.json` with `status` of `done`, `failed`,
or `sync_partial`. The report job's quality gate requires `status == "done"`.

The job waits up to 90 more minutes (polling every 60s), so a slow day still posts
by about 08:30. If the pipeline fails (`failed`), the job stops at once and posts
nothing. If the marker never appears, the job exits 3.

Exit codes: 0 posted / already posted / dry-run, 1 post failed or no token,
2 gate failed, 3 no marker, 4 corrupt state, 5 pending state, 6 claim write
failed, 7 final state write failed, 8 pipeline `sync_partial`.
A state file `<root>/state/crowd_report_posted_<date>.json` (written atomically)
holds `pending` (claimed before the POST), `posted`, or `failed`. `posted` and
`pending` block reposting (pending means a crash mid-post is possible: check
#crowd-reports, then use `--force`); `failed` does not block. `--force` overrides. Preview any time with `daily_report.py --dry-run`.

## Install

Via the Mini rollout, which seds `__HOME__` to `$HOME`, copies the plists to
`~/Library/LaunchAgents/`, and loads them. Manual equivalent:

```
sed "s|__HOME__|$HOME|g" launchd/<label>.plist > ~/Library/LaunchAgents/<label>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

Ensure `~/hazeydata/pipeline/logs/` exists first (launchd will not create it).
