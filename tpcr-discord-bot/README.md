# TPCR Discord Bot

Discord bot for Theme Park Crowd Report. Slash commands: `/today`, `/crowd`, `/best-day`, etc.

## Host

Per a read-only Mini check on 2026-10-21, the bot runs on the Mac Mini as the launchd job `com.hazeydata.tpcr-bot`:

```
com.hazeydata.tpcr-bot (running, pid 16323, last exit -15)
  program = <repo>/.venv/bin/python -u bot.py
  WorkingDirectory = <repo>/tpcr-discord-bot
```

Wilma is powered off per chief, 2026-10-21 (not live-verified). `docs/DISCORD_BOT_DUCKDB_MIGRATION.md` (Phase 3.1) and `SESSION_LOG_ARCHIVE.md:19` describe the old Wilma setup, where the bot ran under systemd. That old bot must stay disabled so two bots never share the token. If Wilma is booted again, run these steps on Wilma itself, by someone with access to it; do not run them remotely from here.

1. Confirm the unit name first. There are two candidate names, and neither is live-verified:
   - `tpcr-bot.service`, a system unit (per chief's records; the name does not appear in this repo's unit files or in the Mini check).
   - `tpcr-discord-bot.service` (per `docs/DISCORD_BOT_DUCKDB_MIGRATION.md`, which lists it under `/etc/systemd/system/` in one place and calls it a user service in another).

```
systemctl list-units --all | grep -i tpcr          # system level
systemctl --user list-units --all | grep -i tpcr   # user-level variant
```

2. Disable whichever unit step 1's `list-units` output actually shows, at the level it shows it. Rule: if it appears in the system-level list, use `sudo systemctl`; if it appears only in the `--user` list, use `systemctl --user`. Substitute the name step 1 found; do not assume either candidate. Examples (not live-verified):

```
# system unit (shown by the first list-units command)
sudo systemctl disable --now tpcr-bot.service
systemctl is-enabled tpcr-bot.service              # expect: disabled

# user unit (shown only by the --user list-units command)
systemctl --user disable --now tpcr-discord-bot.service
systemctl --user is-enabled tpcr-discord-bot.service   # expect: disabled
```

If both names show up, disable each one at the level it appears.

`stats_snapshot.py` reads `DISCORD_BOT_TOKEN` from the env file given by `--env-file` (default `~/.env`), falling back to the process environment. It writes to `tpcr-discord-bot/data/stats.csv` by default (or `--csv`).

## /ask paths (env)

`ask_agent.py` and `bot.py` read host paths and Qwen request settings from the environment. `bot.py` loads `~/.env`
with python-dotenv (`load_dotenv(os.path.expanduser("~/.env"))`), so set them there; launchd
`EnvironmentVariables` in the plist win over `~/.env` because `load_dotenv` does not override.

| env var | default (original Linux host) |
|---|---|
| `TPCR_DATA_ROOT` | `/mnt/data/pipeline` (pipeline root; DuckDB, WTI, forecasts, dimension tables, staging, fact parquet, accuracy archive, calendar images, live inference, and the /ask prompt all derive from it) |
| `TPCR_DUCKDB_PATH` | `$TPCR_DATA_ROOT/tpcr_live.duckdb` (explicit value wins over the root) |
| `TPCR_ASK_USAGE_FILE` | `<bot dir>/ask_usage.json` |
| `TPCR_ASK_QUESTION_LOG` | `<bot dir>/ask_questions.jsonl` |
| `TPCR_ASK_FEEDBACK_LOG` | `<bot dir>/ask_feedback.jsonl` (write failures log a WARNING) |
| `TPCR_QWEN_MAX_TOKENS` | `700` |
| `TPCR_QWEN_TEMPERATURE` | `0.2` |
| `TPCR_QWEN_THINKING` | off (`1`/`true`/`yes`/`on` enables; sent as `chat_template_kwargs.enable_thinking`) |

Bot-side paths (`bot.py`) are resolved once at startup, after `~/.env` is loaded; the Mini sets
`TPCR_DATA_ROOT=<pipeline dir>`. If Qwen returns `<tool_call>` JSON in text instead of structured
`tool_calls`, it is parsed and run; malformed blocks are ignored.

Usage tracking and the question log create
missing parent directories; if they still cannot write, they log an ERROR naming the env var and
/ask carries on. Each tool round logs `ask tool_call name=<tool> id=<id>` and
`ask tool_result name=<tool> status=ok|error` at INFO.

## Premium Integration

Users with the **Premium** Discord role (assigned via Stripe webhook when they subscribe) get:

- 90-day crowd forecasts (free = 7 days)
- 1-year outlook in `/best-day`

### Usage

1. Set `PREMIUM_ROLE_ID` in your bot's environment (same as in `~/.env` on the bot host).
2. In your `/best-day` (or similar) command, call `max_forecast_days(member)` to get the allowed range.
3. If the user requests more days than allowed, show `premium_teaser_message()` and return.

See `bot.py` for `has_premium_role()`, `max_forecast_days()`, and `premium_teaser_message()`.
