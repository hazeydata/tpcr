# TPCR Discord Bot

Discord bot for Theme Park Crowd Report. Slash commands: `/today`, `/crowd`, `/best-day`, etc.

## Host

The bot runs on the Mac Mini as the launchd job `com.hazeydata.tpcr-bot`. That is the only supported run path.

The old Wilma systemd unit `tpcr-bot.service` is retired and stays disabled. Wilma is off. If Wilma is ever booted again, run this on Wilma before anything else so two bots never share the token:

```
sudo systemctl disable --now tpcr-bot.service
```

`stats_snapshot.py` reads `DISCORD_BOT_TOKEN` from `~/.env` on the Mini (or `--env-file`) and writes to `tpcr-discord-bot/data/stats.csv` by default (or `--csv`).

## Premium Integration

Users with the **Premium** Discord role (assigned via Stripe webhook when they subscribe) get:

- 90-day crowd forecasts (free = 7 days)
- 1-year outlook in `/best-day`

### Usage

1. Set `PREMIUM_ROLE_ID` in your bot's environment (same as in `~/.env` on the Mac Mini bot host).
2. In your `/best-day` (or similar) command, call `max_forecast_days(member)` to get the allowed range.
3. If the user requests more days than allowed, show `premium_teaser_message()` and return.

See `bot.py` for `has_premium_role()`, `max_forecast_days()`, and `premium_teaser_message()`.
