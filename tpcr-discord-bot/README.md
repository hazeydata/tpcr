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

## /ask wording rules

- **Bands:** each park's normal band is p25–p75 of historical WTI (2022-01-01 through yesterday, ≥365 days), read from `$TPCR_DATA_ROOT/wti/wti.parquet` and recomputed per day. Same definition as `operations/tweet_v2/cards.py` `boxes()` (a parity test keeps them in sync). It is listed in the `/ask` system prompt.
- **Labels:** WTI values are labelled only `below normal` / `normal` / `above normal` against that park's band. `enforce_wording()` labels a day/value line (e.g. `• Tue 11/24: 15.8`) only when the answer mentions WTI, Wait Time Index or crowd, and never when the value is followed by min, mins, minutes, m, %, hours, hrs or h (ride waits stay unlabelled). It replaces any model label on those lines.
- **Banned phrases:** "below/above average" (also "well/far below/above"), "typical day", "great/best day to visit", and comparisons against average/usual/typical ("quieter/busier/lighter/heavier/calmer/slower/less crowded/more crowded/less busy than average/usual/typical") are replaced by the computed label when the sentence's WTI values all map to one label, otherwise deleted (always deleted on day/value lines, or when no band exists).
- **Comparative "than normal":** "busier/heavier/more crowded than normal" is kept only when the same sentence has a WTI value above the park's band; "quieter/lighter/calmer/less crowded than normal" only with a value below it. Otherwise it is treated like a banned phrase.
- **Seasonal / beyond-data claims:** sentences with "typically", "usually", "historically", etc., or a date later than the last date in the shown data, are removed. A sentence naming a month (not part of a "Nov 22"-style date) or a holiday/season (Christmas, New Year, holiday season, spring break, Easter, summer, Halloween, Fourth of July, Memorial Day, Labor Day, MLK Day, Presidents Day, Thanksgiving) is also removed when its next occurrence is after the horizon (within 300 days). The horizon is the latest date on a data line, or today when the answer has none. Past references ("last", "this past", "back in", or a following earlier year) and the current month are kept.
- **Empty answer:** if every sentence is removed, the reply is "I don't have enough data to answer that. Try asking about specific dates!"
- Numbers are never changed.
- **Fixed bot output uses the same bands:** `/today`, `/crowd`, `/best-day`, the daily crowd report and the forecast-image "Best days" list label each WTI with the same three labels (`WTI 16 (normal)`), computed by `ask_agent.normal_bands()` / `band_label()` as of today (the band is historical through yesterday). When a park has no band (missing parquet, under 365 history days, unknown park, or no park code on the image) the label is omitted and only the number is shown. The emoji and embed colour are a separate visual scale and are unchanged. `generate_forecast_image(park_name, days_data, park_code="", as_of=None)` takes the optional `park_code` to enable the label.
