#!/usr/bin/env python3
"""Daily /ask smoke test for TPCR Discord bot.

Runs the same ask_agent path as the /ask slash command, verifies the answer
against live DuckDB WTI for Magic Kingdom today, and posts PASS/FAIL to
#bot-commands every morning.

Usage:
  .venv/bin/python scripts/tpcr_ask_daily_smoke.py
  .venv/bin/python scripts/tpcr_ask_daily_smoke.py --quiet   # cron-friendly
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path.home() / ".env")

REPO = Path("/home/wilma/theme-park-crowd-report")
BOT_DIR = REPO / "tpcr-discord-bot"
DUCKDB_PATH = "/mnt/data/pipeline/tpcr_live.duckdb"
BOT_COMMANDS_CHANNEL = os.getenv("TPCR_BOT_COMMANDS_CHANNEL", "1478240248361128079")
RESULT_PATH = Path("/tmp/tpcr_ask_daily_smoke.json")
LOG_PATH = Path("/tmp/tpcr_ask_daily_smoke.log")

sys.path.insert(0, str(BOT_DIR))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or ""
HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "HazeyTPCRBot (https://hazeydata.ai, 1.0)",
}

# Known failure phrases from ask_agent / bot
_FAILURE_PATTERNS = (
    "data is updating right now",
    "try again in a minute",
    "database is temporarily busy",
    "i wasn't able to fully answer",
    "try rephrasing or asking something more specific",
    "query error:",
    "unable to access",
    "no data available",
    "hit a temporary glitch",
    "ai agent is not configured",
)


def log(msg: str, quiet: bool = False) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if not quiet:
        print(line)


def resolve_anthropic_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY") or ""
    if key:
        return key
    try:
        profiles = Path.home() / ".clawdbot/agents/anthropic/agent/auth-profiles.json"
        data = json.loads(profiles.read_text())
        profiles_d = data.get("profiles", data)
        return (profiles_d.get("default") or {}).get("key") or ""
    except Exception:
        return ""


def get_mk_wti_today() -> float | None:
    import duckdb

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        row = con.execute(
            """
            SELECT wti
            FROM wti
            WHERE park_code = 'MK'
              AND park_date = CURRENT_DATE
              AND (time_slot IS NULL OR time_slot = 'daily' OR time_slot = '')
            ORDER BY CASE WHEN source = 'actual' THEN 0 ELSE 1 END
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            # broader fallback — some schemas omit time_slot filter
            row = con.execute(
                """
                SELECT wti
                FROM wti
                WHERE park_code = 'MK' AND park_date = CURRENT_DATE
                LIMIT 1
                """
            ).fetchone()
        return float(row[0]) if row else None
    finally:
        con.close()


def post_embed(title: str, description: str, color: int, fields=None) -> dict:
    if not TOKEN:
        return {"ok": False, "status": 0, "id": None, "error": "no_discord_token"}
    embed = {
        "title": title[:256],
        "description": (description or "")[:4000],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "daily smoke · /ask backend · #bot-commands"},
    }
    if fields:
        embed["fields"] = fields[:25]
    r = requests.post(
        f"https://discord.com/api/v10/channels/{BOT_COMMANDS_CHANNEL}/messages",
        headers=HEADERS,
        json={"embeds": [embed]},
        timeout=30,
    )
    body: dict = {}
    try:
        body = r.json()
    except Exception:
        body = {"text": r.text[:200]}
    return {
        "ok": r.ok,
        "status": r.status_code,
        "id": body.get("id") if isinstance(body, dict) else None,
        "error": None if r.ok else body,
    }


def extract_wti_numbers(text: str) -> list[float]:
    """Pull plausible WTI numbers from answer text."""
    nums = []
    # Prefer explicit WTI mentions first
    for m in re.finditer(r"(?i)\bwti\b[^0-9]{0,20}(\d+(?:\.\d+)?)", text):
        try:
            nums.append(float(m.group(1)))
        except ValueError:
            pass
    if nums:
        return nums
    for m in re.finditer(r"\b(\d{1,2}(?:\.\d+)?)\b", text):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        # WTI typically ~1–60+
        if 1.0 <= v <= 80.0:
            nums.append(v)
    return nums


def answer_looks_correct(answer: str, expected_wti: float | None) -> tuple[bool, str]:
    if not answer or len(answer.strip()) < 10:
        return False, "answer too short/empty"
    lower = answer.lower()
    if any(p in lower for p in _FAILURE_PATTERNS):
        return False, "matched known failure phrase"
    if "magic kingdom" not in lower and "mk" not in lower:
        # soft: still allow if WTI number matches strongly
        pass
    if expected_wti is None:
        # No ground truth — require non-failure answer mentioning WTI or a number
        if "wti" in lower or extract_wti_numbers(answer):
            return True, "no DuckDB ground truth; answer non-empty and plausible"
        return False, "no DuckDB ground truth and answer lacks WTI signal"

    nums = extract_wti_numbers(answer)
    if not nums:
        return False, f"expected WTI ~{expected_wti:.1f} but no number found in answer"

    # Accept if any extracted number is within 1.0 of expected (rounding/display)
    for n in nums:
        if abs(n - expected_wti) <= 1.0:
            return True, f"WTI match: answer has {n}, expected {expected_wti:.1f}"
    # Also accept integer rounding of expected
    for n in nums:
        if abs(n - round(expected_wti)) <= 0.6:
            return True, f"WTI near-match: answer has {n}, expected {expected_wti:.1f}"
    return False, f"WTI mismatch: answer nums={nums}, expected {expected_wti:.1f}"


async def run_ask(question: str, api_key: str) -> str:
    from ask_agent import ask_agent

    return await ask_agent(question, "daily-ask-smoke", api_key, username="daily-ask-smoke")


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily /ask smoke → #bot-commands")
    ap.add_argument("--quiet", action="store_true", help="Less stdout (cron)")
    ap.add_argument(
        "--question",
        default="What is Magic Kingdom's WTI today? Reply in one short sentence including the numeric WTI.",
        help="Smoke question",
    )
    args = ap.parse_args()
    quiet = args.quiet

    started = datetime.now(timezone.utc).isoformat()
    result: dict = {
        "started": started,
        "command": "/ask",
        "channel": BOT_COMMANDS_CHANNEL,
        "ok": False,
    }

    try:
        expected = get_mk_wti_today()
        result["expected_wti"] = expected
        log(f"expected MK WTI today = {expected}", quiet)

        key = resolve_anthropic_key()
        if not key:
            result["error"] = "no_anthropic_key"
            post = post_embed(
                "❌ daily smoke /ask",
                "ANTHROPIC_API_KEY not available — cannot exercise /ask backend.",
                color=0xE74C3C,
            )
            result["post"] = post
            RESULT_PATH.write_text(json.dumps(result, indent=2, default=str))
            log("FAIL: no Anthropic key", quiet)
            return 2

        answer = asyncio.run(run_ask(args.question, key))
        result["question"] = args.question
        result["answer"] = answer
        result["answer_len"] = len(answer or "")

        correct, reason = answer_looks_correct(answer or "", expected)
        result["correct"] = correct
        result["reason"] = reason
        ok = bool(answer) and correct
        result["ok"] = ok

        color = 0x2ECC71 if ok else 0xE74C3C
        title = f"{'✅' if ok else '❌'} daily smoke /ask — {date.today().isoformat()}"
        exp_s = f"{expected:.1f}" if expected is not None else "n/a"
        desc = (
            f"**Path:** `/ask` backend (`ask_agent`)\n"
            f"**Q:** {args.question}\n"
            f"**Expected MK WTI (DuckDB):** {exp_s}\n"
            f"**Check:** {reason}\n"
            f"**Result:** {'PASS' if ok else 'FAIL'}\n\n"
            f"**A:** {(answer or '')[:3000]}"
        )
        post = post_embed(title, desc, color=color)
        result["post"] = post
        if not post.get("ok"):
            result["ok"] = False
            ok = False
            log(f"Discord post failed: {post}", quiet)

        RESULT_PATH.write_text(json.dumps(result, indent=2, default=str))
        log(
            f"{'PASS' if ok else 'FAIL'}: {reason}; answer_len={result['answer_len']}; "
            f"post_id={post.get('id')}",
            quiet,
        )
        return 0 if ok else 1
    except Exception as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()[-2000:]
        try:
            post = post_embed(
                f"❌ daily smoke /ask — {date.today().isoformat()}",
                f"```{type(e).__name__}: {e}```\n```{traceback.format_exc()[-1500:]}```",
                color=0xE74C3C,
            )
            result["post"] = post
        except Exception as pe:
            result["post_error"] = str(pe)
        RESULT_PATH.write_text(json.dumps(result, indent=2, default=str))
        log(f"FAIL exception: {e}", quiet)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
