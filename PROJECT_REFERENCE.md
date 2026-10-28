# WTI / TPCR — Project Reference

**Stable reference data.** Changes rarely. Read on cold-start or when channel IDs / paths are needed.

---

## Repos

| Repo | Purpose | Default branch |
|---|---|---|
| `hazeydata/theme-park-crowd-report` | WTI pipeline, SESSION_LOG, governing specs | `main` |
| `hazeydata/operations` | Enterprise ops, briefings, cron schedule | `main` |
| `hazeydata/hazeydata.ai` | Website/blog | `master` (NOT main) |
| `hazeydata/data-hub` | Independent data collection platform | `main` |

## Discord channels (Slate Rock & Gravel Co., guild `1479350342318690505`)

| Channel | ID | Purpose |
|---|---|---|
| `#wti-pipeline` | `1479351574177513576` | Pipeline status, daily reports, shadow reports |
| `#barney-wilma-dev` | `1479937927378239550` | Barney ↔ Wilma dev loop |
| `#gazoo` | `1479351587129262232` | Auditor posts (2 AM / 4 PM ET) |
| `#fred-wilma` | `1479351572386414675` | Fred ↔ Wilma |
| `#pebbles` | `1479351583908171937` | Design |

**Customer server (TPCR):**
| Channel | ID |
|---|---|
| `#general` | `1478239791010287779` |
| `#feedback` | `1471935482513457266` |
| `#announcements` | `1471935589371609162` |
| `#crowd-reports` | `1478240066382860298` |
| `#bot-commands` | `1478240248361128079` |

## Governing docs

| Doc | Location |
|---|---|
| Pipeline V4 Design (governing spec) | `docs/PIPELINE_V4_DESIGN.md` (TPCR) |
| Pipeline Audit (Feb 2026) | `docs/PIPELINE_AUDIT_20260219.md` (TPCR) |
| The Quarry Architecture | `docs/THE_QUARRY_ARCHITECTURE.md` (TPCR) |
| Modeling & WTI Methodology | `docs/MODELING_AND_WTI_METHODOLOGY.md` (TPCR) |
| Audit & Redesign Playbook | `docs/AUDIT_REDESIGN_PLAYBOOK.md` (operations) |
| Barney cold-start memory | `docs/BARNEY.md` (operations) |
| Session log restructure spec | `docs/OPS_30_SESSION_LOG_RESTRUCTURE.md` (operations) |

## Agents

- **Wilma** — Sonnet via Clawdbot on wilma-server. Runs pipeline daily 6 AM ET. "Close fast, verify never" — demand evidence.
- **Dino** — Claude Code on Mac Mini M4 Pro. Executes server-side code, deployments, crons. Cannot read/post Discord. Briefings via `operations/docs/briefings/DINO_[TOPIC]_[YYYYMMDD].md` or direct terminal prompts from Fred.
- **Gazoo** — Independent auditor (Opus). Posts #gazoo at 2 AM / 4 PM ET. Audit lag can be 8-10 hours — do not trust as real-time.
- **Pebbles** — Designer (Sonnet). Frontend, dashboards.

## Standing rules

- **Rule 17:** Proof-batch before any cron goes live.
- **Rule 19:** Read docs first.
- No s06/s07/s08 changes deployed after midnight without proof-batching first.
- Background jobs: `systemd-run --scope --user`, NEVER `nohup`.
- Pipeline lock window: 6–8 AM ET — no deployments.
- Ticket first, then code.
- Scan repo docs before assuming any alert is a real outage (new rule, S32).
- Archive filenames MUST contain `YYYY-MM-DD` (hyphens) — evaluator regex silently skips others.
- Entity reports always show entity name alongside code.
- Evaluation lives in ONE place: `pipeline/competition/shadow_evaluate.py`. Orchestrators never carry their own eval SQL.
- DuckDB live table name: `live_waits` (NOT `live_wait_times`).

## Fred's hours

9 AM–4 PM ET and 9 PM–2 AM ET. Autonomous work outside these windows.

## SSH / infra

- `ssh fred@wilma-server` → `sudo su - wilma`
- Pipeline output: `/home/wilma/hazeydata/pipeline` on wilma-server
- Mac Mini: runs canonical `wti_predicted_tweet.py` (4 PM ET, forecast for tomorrow) and `wti_observed_tweet.py` (8:30 AM ET, reply about yesterday) crons. Both post the v2 card MP4 built by `operations/tweet_v2/build_day.sh`; see `operations/tweet_v2/README.md`. Parallel transition (TPCR-TWEET-RESTYLE-CUTOVER): new originals from the Oct 29 2026 4 PM ET run; a reply uses its original's format (tag store `tweet_formats.json` next to `tweet_state.json`); the old format retires 00:00 ET Nov 1 2026 (`config/tweet_format.json`)
- `operations/scripts/x_approved_reply_poster.py`: posts Fred-approved, hash-bound, Ed25519-signed X replies (poster holds the public key only); dry run by default, `--apply` needs Fred's go via chief. Not yet scheduled on the Mini. See `operations/scripts/x_approved_reply_poster.md`.
- `operations/scripts/x_reply_sign_approval.py`: approval-flow helper that Ed25519-signs queue entries (AC 3c); private key path given at runtime, never in the repo, queue or poster env.
