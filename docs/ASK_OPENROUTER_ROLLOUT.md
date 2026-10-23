# /ask via OpenRouter — rollout

## What changed

The `/ask` Claude path no longer uses the Anthropic SDK. It now POSTs to
`https://openrouter.ai/api/v1/chat/completions` (OpenAI-compatible) with `httpx`, the same client
the Qwen path uses (60 s timeout).

- Model: `anthropic/claude-sonnet-4.6` (`OPENROUTER_MODEL` in `tpcr-discord-bot/ask_agent.py`); this id is what gets logged as the answering model.
- Request: `max_tokens` 1500, system prompt as a `system` message, the existing `TOOLS` list (OpenAI function format), up to 5 tool rounds using `tool_calls` and `role: "tool"` messages.
- Headers: `Authorization: Bearer <key>`, `X-Title: TPCR /ask`.
- `bot.py` reads the key with `os.getenv("OPENROUTER_API_KEY")`; the old `~/.clawdbot/.../auth-profiles.json` loading is gone.
- The `anthropic` package is removed from `requirements.txt` and `tpcr-discord-bot/requirements-test.txt`.

## Required on the Mini

Set `OPENROUTER_API_KEY` in `~/.env` (name only; never commit or paste the value), then restart the bot.

## TPCR_ASK_PROVIDER

- `openrouter` (default): OpenRouter first, Qwen as fallback.
- `qwen`: Qwen only.
- `anthropic`: accepted as an alias for `openrouter`.
- Any other value fails at startup.

## Fallback behavior

Qwen answers, exactly as before, when the key is missing, OpenRouter returns an HTTP error, the call times out or raises anything, or the answer is empty. Logs carry only the exception class name and HTTP status, never the key or response body.

## Rollback

Set `TPCR_ASK_PROVIDER=qwen` in `~/.env` and restart the bot to stop calling OpenRouter immediately. To go back to the SDK path entirely, revert the change that introduced this file.
