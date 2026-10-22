# launchd jobs (Mac Mini)

| Label | When | What |
|-------|------|------|
| `com.hazeydata.tpcr-crowd-report` | 07:00 daily | `daily_report.py --wait-for-pipeline 90` — posts the Daily Crowd Report to #crowd-reports |
| `com.hazeydata.tpcr-crowd-report-watchdog` | 09:00 daily | `scripts/tpcr_crowd_report_watchdog.py --check-only` — verifies today's post landed; reports to #bot-commands, no healing |

Plists use the placeholder `__HOME__` (launchd does not expand `~`); rollout
substitutes it with `$HOME`. Secrets (`DISCORD_BOT_TOKEN`) are not in the plists;
the scripts read `~/.env`. Data root is `TPCR_DATA_ROOT` (default
`~/hazeydata/pipeline`); logs go to `<root>/logs/`.

## Why 07:00 plus a wait

The pipeline (`pipeline/pipeline.py`) starts at 06:00 and typically takes ~10–12 min,
but `s01_sync` may time out after up to 30 min. At the end of a full run it writes
`<root>/logs/pipeline_metrics_<YYYY-MM-DD>.json` with `status` of `done`, `failed`,
or `sync_partial`. The report job's quality gate requires `status == "done"`.

The job waits up to 90 more minutes (polling every 60s), so a slow day still posts
by about 08:30. If the pipeline fails (`failed`), the job stops at once and posts
nothing. If the marker never appears, the job exits 3.

Exit codes: 0 posted / already posted, 1 post failed, 2 gate failed, 3 no marker.
A sentinel `<root>/state/crowd_report_posted_<date>.json` prevents duplicates
(`--force` overrides). Preview any time with `daily_report.py --dry-run`.

## Install

Via the Mini rollout, which seds `__HOME__` to `$HOME`, copies the plists to
`~/Library/LaunchAgents/`, and loads them. Manual equivalent:

```
sed "s|__HOME__|$HOME|g" launchd/<label>.plist > ~/Library/LaunchAgents/<label>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

Ensure `~/hazeydata/pipeline/logs/` exists first (launchd will not create it).
