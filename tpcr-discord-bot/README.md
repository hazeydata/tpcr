# TPCR Discord Bot

Discord bot for Theme Park Crowd Report. Slash commands: `/today`, `/crowd`, `/best-day`, etc.

## Host

Per a read-only Mini check on 2026-10-21, the bot runs on the Mac Mini as the launchd job `com.hazeydata.tpcr-bot`:

```
com.hazeydata.tpcr-bot (running, pid 16323, last exit -15)
  program = <repo>/.venv/bin/python -u bot.py
  WorkingDirectory = <repo>/tpcr-discord-bot
```

Wilma is reported off. `docs/DISCORD_BOT_DUCKDB_MIGRATION.md` (Phase 3.1) and `SESSION_LOG_ARCHIVE.md:19` describe the old Wilma setup, where the bot ran as a systemd user service (`tpcr-bot.service`). That service should stay disabled. If Wilma is booted again, run this on Wilma as the bot's user so two bots never share the token:

```
systemctl --user disable --now tpcr-bot.service
systemctl --user is-enabled tpcr-bot.service   # expect: disabled
```

If `systemctl --user status tpcr-bot.service` finds no such unit, it may have been installed as a system unit instead: `sudo systemctl disable --now tpcr-bot.service`.

`stats_snapshot.py` reads `DISCORD_BOT_TOKEN` from the env file given by `--env-file` (default `~/.env`), falling back to the process environment. It writes to `tpcr-discord-bot/data/stats.csv` by default (or `--csv`).

## Premium Integration

Users with the **Premium** Discord role (assigned via Stripe webhook when they subscribe) get:

- 90-day crowd forecasts (free = 7 days)
- 1-year outlook in `/best-day`

### Usage

1. Set `PREMIUM_ROLE_ID` in your bot's environment (same as in `~/.env` on the bot host).
2. In your `/best-day` (or similar) command, call `max_forecast_days(member)` to get the allowed range.
3. If the user requests more days than allowed, show `premium_teaser_message()` and return.

See `bot.py` for `has_premium_role()`, `max_forecast_days()`, and `premium_teaser_message()`.
