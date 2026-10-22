#!/usr/bin/env python3
"""Morning watchdog for TPCR #crowd-reports daily post.

Checks that today's Daily Crowd Report landed correctly in #crowd-reports.
If missing or bad, runs a heal ladder (diagnose → ops fix → retest → redeploy),
then posts PASS/FAIL (+ actions taken) to #bot-commands.

Usage:
  .venv/bin/python scripts/tpcr_crowd_report_watchdog.py
  .venv/bin/python scripts/tpcr_crowd_report_watchdog.py --quiet
  .venv/bin/python scripts/tpcr_crowd_report_watchdog.py --check-only
  .venv/bin/python scripts/tpcr_crowd_report_watchdog.py --force-heal
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(Path.home() / ".env")

REPO = Path(__file__).resolve().parents[1]
BOT_DIR = REPO / "tpcr-discord-bot"
VENV_PY = REPO / ".venv" / "bin" / "python"
PIPELINE_PY = REPO / "pipeline" / "pipeline.py"
DAILY_REPORT = BOT_DIR / "daily_report.py"


def data_root() -> Path:
    """Data root resolved at call time: TPCR_DATA_ROOT else ~/hazeydata/pipeline."""
    return Path(os.environ.get("TPCR_DATA_ROOT") or "~/hazeydata/pipeline").expanduser()


def duckdb_path() -> str:
    override = os.environ.get("TPCR_DUCKDB_PATH")
    if override:
        return str(Path(override).expanduser())
    return str(data_root() / "tpcr_live.duckdb")


def marker_path(today: date) -> Path:
    return data_root() / "logs" / f"pipeline_metrics_{today.isoformat()}.json"


DUCKDB_PATH = duckdb_path()
GATE_LOG = data_root() / "logs" / "daily_report_gate.log"
DAILY_LOG = data_root() / "logs" / "tpcr-crowd-report.log"

CROWD_REPORTS_CHANNEL = os.getenv("TPCR_CROWD_REPORTS_CHANNEL", "1478240066382860298")
BOT_COMMANDS_CHANNEL = os.getenv("TPCR_BOT_COMMANDS_CHANNEL", "1478240248361128079")
RESULT_PATH = data_root() / "logs" / "tpcr_crowd_report_watchdog.json"
LOG_PATH = data_root() / "logs" / "tpcr_crowd_report_watchdog.log"

WDW = ("MK", "EP", "HS", "AK")
TOKEN = os.getenv("DISCORD_BOT_TOKEN") or ""
HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "HazeyTPCRBot (https://hazeydata.ai, 1.0)",
}

# Heal timeouts (seconds)
STEP_TIMEOUT = 900  # s09_wti / s11_deploy can take a few minutes
REPORT_TIMEOUT = 120


def log(msg: str, quiet: bool = False) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if not quiet:
        print(line)


def post_embed(title: str, description: str, color: int, fields=None) -> dict:
    if not TOKEN:
        return {"ok": False, "status": 0, "id": None, "error": "no_discord_token"}
    embed = {
        "title": title[:256],
        "description": (description or "")[:4000],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "morning watchdog · #crowd-reports · #bot-commands"},
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


def discord_me() -> dict:
    if not TOKEN:
        return {"ok": False, "status": 0, "error": "no_token"}
    r = requests.get(
        "https://discord.com/api/v10/users/@me",
        headers=HEADERS,
        timeout=20,
    )
    try:
        body = r.json()
    except Exception:
        body = {"text": r.text[:200]}
    return {
        "ok": r.ok,
        "status": r.status_code,
        "username": body.get("username") if isinstance(body, dict) else None,
        "error": None if r.ok else body,
    }


def fetch_recent_messages(channel_id: str, limit: int = 15) -> list[dict]:
    if not TOKEN:
        return []
    r = requests.get(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers=HEADERS,
        params={"limit": limit},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"discord fetch {channel_id} HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"discord fetch unexpected body: {data}")
    return data


def today_display_tokens(today: date) -> list[str]:
    """Match daily_report title formatting variants."""
    return [
        today.isoformat(),  # 2026-09-16
        today.strftime("%A, %B %d, %Y"),  # Wednesday, September 16, 2026
        today.strftime("%B %d, %Y"),  # September 16, 2026
        today.strftime("%B %-d, %Y") if sys.platform != "win32" else today.strftime("%B %d, %Y"),
        # current daily_report title: unpadded day
        f"{today.strftime('%A, %B')} {today.day}, {today.year}",
        # zero-padded day can appear as "September 06" vs "September 6"
        today.strftime("%B %d, %Y").replace(" 0", " "),
    ]


def extract_park_wti_from_text(text: str) -> dict[str, float]:
    """Pull park WTI values from embed description."""
    out: dict[str, float] = {}
    patterns = [
        (r"(?i)Magic Kingdom[^\n]{0,80}?WTI\s*\*?\*?(\d+(?:\.\d+)?)", "MK"),
        (r"(?i)\bEPCOT\b[^\n]{0,80}?WTI\s*\*?\*?(\d+(?:\.\d+)?)", "EP"),
        (r"(?i)Hollywood Studios[^\n]{0,80}?WTI\s*\*?\*?(\d+(?:\.\d+)?)", "HS"),
        (r"(?i)Animal Kingdom[^\n]{0,80}?WTI\s*\*?\*?(\d+(?:\.\d+)?)", "AK"),
    ]
    for pat, code in patterns:
        m = re.search(pat, text or "")
        if m:
            try:
                out[code] = float(m.group(1))
            except ValueError:
                pass
    return out


def find_today_crowd_report(messages: list[dict], today: date) -> dict | None:
    tokens = [t.lower() for t in today_display_tokens(today) if t]
    for m in messages:
        embeds = m.get("embeds") or []
        for emb in embeds:
            title = emb.get("title") or ""
            desc = emb.get("description") or ""
            blob = f"{title}\n{desc}".lower()
            if "daily crowd report" not in title.lower() and "daily crowd report" not in blob:
                continue
            if any(tok in blob for tok in tokens):
                return {
                    "id": m.get("id"),
                    "timestamp": m.get("timestamp"),
                    "title": title,
                    "description": desc,
                    "raw": m,
                }
    return None


def get_wdw_wti_today(today: date | None = None) -> dict[str, float | None]:
    import duckdb

    today = today or date.today()
    scores: dict[str, float | None] = {p: None for p in WDW}
    con = duckdb.connect(duckdb_path(), read_only=True)
    try:
        for code in WDW:
            row = None
            try:
                row = con.execute(
                    """
                    SELECT wti FROM wti
                    WHERE park_code = ? AND park_date = ?
                      AND (time_slot IS NULL OR time_slot = 'daily' OR time_slot = '')
                    ORDER BY CASE WHEN source = 'actual' THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    [code, today],
                ).fetchone()
            except Exception:
                row = None  # time_slot/source columns may not exist
            if row is None:
                row = con.execute(
                    "SELECT wti FROM wti WHERE park_code = ? AND park_date = ? LIMIT 1",
                    [code, today],
                ).fetchone()
            scores[code] = float(row[0]) if row else None
    finally:
        con.close()
    return scores


def pipeline_marker_status(today: date | None = None) -> tuple[str | None, float | None, str | None]:
    """Check pipeline_metrics_<today>.json: (status, age_hours_from_mtime, error)."""
    path = marker_path(today or date.today())
    try:
        status = json.loads(path.read_text()).get("status")
        age = (datetime.now().timestamp() - path.stat().st_mtime) / 3600.0
        return status, age, None
    except Exception as e:
        return None, None, f"{path.name}: {type(e).__name__}: {e}"


def _write_result(text: str) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(text)


def tail_text(path: Path, n: int = 40) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"(no log: {e})"


def run_cmd(cmd: list[str], timeout: int, cwd: str | None = None) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd or str(REPO),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ},
        )
        out = (p.stdout or "")[-4000:]
        err = (p.stderr or "")[-2000:]
        return {
            "cmd": cmd,
            "started": started,
            "returncode": p.returncode,
            "ok": p.returncode == 0,
            "stdout_tail": out,
            "stderr_tail": err,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "cmd": cmd,
            "started": started,
            "returncode": -1,
            "ok": False,
            "stdout_tail": (e.stdout or "")[-2000:] if isinstance(e.stdout, str) else "",
            "stderr_tail": f"TIMEOUT after {timeout}s",
        }
    except Exception as e:
        return {
            "cmd": cmd,
            "started": started,
            "returncode": -1,
            "ok": False,
            "stdout_tail": "",
            "stderr_tail": f"{type(e).__name__}: {e}",
        }


def validate_report(report: dict | None, expected: dict[str, float | None], today: date) -> dict:
    """Return {ok, reasons, extracted}."""
    reasons: list[str] = []
    if not report:
        return {"ok": False, "reasons": ["no today's Daily Crowd Report in #crowd-reports"], "extracted": {}}

    title = report.get("title") or ""
    desc = report.get("description") or ""
    blob = f"{title}\n{desc}"

    if re.search(r"(?i)\b(error|failed|unauthorized|exception)\b", title):
        reasons.append(f"title looks like error: {title[:120]}")

    extracted = extract_park_wti_from_text(blob)
    for code in WDW:
        if code not in extracted:
            reasons.append(f"missing {code} WTI in embed")
            continue
        exp = expected.get(code)
        if exp is None:
            continue
        # daily_report prints int(round) via :.0f
        if abs(extracted[code] - exp) > 1.6 and abs(extracted[code] - round(exp)) > 0.6:
            reasons.append(
                f"{code} WTI mismatch embed={extracted[code]} duckdb={exp:.1f}"
            )

    # Require all 4 WDW present in embed for PASS
    if len([c for c in WDW if c in extracted]) < 4:
        if "missing" not in " ".join(reasons):
            reasons.append(f"only {len(extracted)}/4 WDW parks parsed from embed")

    ok = len(reasons) == 0
    if ok:
        reasons.append(
            "today's report present with WDW WTI matching DuckDB "
            + json.dumps({k: extracted.get(k) for k in WDW})
        )
    return {"ok": ok, "reasons": reasons, "extracted": extracted, "message_id": report.get("id")}


def _today_log_slice(path: Path, today: date, max_lines: int = 80) -> str:
    """Return log lines that look like today (or last max_lines if undated)."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception as e:
        return f"(no log: {e})"
    markers = {
        today.isoformat(),
        today.strftime("%Y-%m-%d"),
        today.strftime("%b %d"),
        today.strftime("%B %d").replace(" 0", " "),
    }
    # Prefer lines containing today's date tokens
    today_lines = [ln for ln in lines if any(m in ln for m in markers)]
    if today_lines:
        return "\n".join(today_lines[-max_lines:])
    # Fall back: only the very end (fresh run may lack a date stamp)
    return "\n".join(lines[-min(20, max_lines):])


def diagnose(expected: dict[str, float | None], me: dict, validation: dict) -> dict:
    """Classify failure into actionable heal classes."""
    classes: list[str] = []
    notes: list[str] = []
    today = date.today()

    auth_ok = bool(TOKEN) and bool(me.get("ok"))
    if not TOKEN:
        classes.append("no_discord_token")
    elif not me.get("ok"):
        classes.append("discord_auth_fail")
        notes.append(f"@me status={me.get('status')}")

    missing = [p for p, v in expected.items() if v is None]
    bad = [p for p, v in expected.items() if v is not None and (v <= 0 or v > 70)]
    if missing:
        classes.append("missing_wti_data")
        notes.append(f"missing DuckDB WTI: {missing}")
    if bad:
        classes.append("bad_wti_values")
        notes.append(f"out-of-bounds WTI: { {p: expected[p] for p in bad} }")

    pstatus, age, age_err = pipeline_marker_status(today)
    if age_err:
        classes.append("pipeline_state_unreadable")
        notes.append(f"pipeline marker missing/unreadable: {age_err}")
    elif pstatus != "done":
        classes.append("pipeline_state_stale")
        notes.append(f"pipeline marker status={pstatus!r} age={age:.1f}h")

    # Only inspect today's log slice so yesterday's 401s don't block heal forever
    log_tail = _today_log_slice(DAILY_LOG, today, 80)
    if (not auth_ok) and "401" in log_tail and "Unauthorized" in log_tail:
        if "discord_auth_fail" not in classes:
            classes.append("discord_auth_fail")
        notes.append("today's daily_report.log shows 401 Unauthorized")
    elif auth_ok and "401" in log_tail and "Unauthorized" in log_tail:
        notes.append("ignoring historical 401 in log — live Discord @me is OK")

    if "GATE FAIL" in log_tail:
        classes.append("quality_gate_failed")
        m = re.findall(r"GATE FAIL: ([^\n]+)", log_tail)
        if m:
            notes.append(f"last gate fail: {m[-1][:200]}")
    if re.search(r"(?m)^(Traceback \(most recent call last\):|[A-Za-z_]+Error:)", log_tail):
        classes.append("daily_report_exception")
        notes.append("exception signals in today's daily_report.log")

    # If validation failed and auth is fine, always ensure a redeploy class exists
    if not validation.get("ok") and auth_ok:
        if not any(
            c in classes
            for c in (
                "missing_wti_data",
                "bad_wti_values",
                "quality_gate_failed",
                "daily_report_exception",
                "pipeline_state_stale",
            )
        ):
            classes.append("post_missing_or_stale")
            notes.append("ops path OK; need re-run daily_report.py")
        elif "post_missing_or_stale" not in classes and "missing_wti_data" not in classes:
            # still may need re-post after data heal
            notes.append("will re-run daily_report after data/state heal")

    if not validation.get("ok") and not classes:
        classes.append("post_missing_or_stale")
        notes.append("ops path OK; need re-run daily_report.py")

    # Priority order for heal
    priority = [
        "no_discord_token",
        "discord_auth_fail",
        "missing_wti_data",
        "bad_wti_values",
        "pipeline_state_stale",
        "pipeline_state_unreadable",
        "quality_gate_failed",
        "daily_report_exception",
        "post_missing_or_stale",
    ]
    primary = next((c for c in priority if c in classes), classes[0] if classes else "unknown")
    return {
        "classes": classes,
        "primary": primary,
        "notes": notes,
        "pipeline_age_h": age,
        "expected_wti": expected,
        "log_tail": log_tail[-1500:],
        "auth_ok": auth_ok,
    }


def heal(diagnosis: dict, quiet: bool) -> list[dict]:
    """Execute heal ladder. Returns list of action result dicts."""
    actions: list[dict] = []
    primary = diagnosis.get("primary")
    classes = set(diagnosis.get("classes") or [])

    def add(name: str, detail: dict) -> None:
        detail = {"action": name, **detail}
        actions.append(detail)
        log(f"heal:{name} ok={detail.get('ok')} rc={detail.get('returncode')}", quiet)

    # Hard stops — cannot invent tokens
    if primary in ("no_discord_token", "discord_auth_fail") or "discord_auth_fail" in classes:
        add(
            "blocked_discord_auth",
            {
                "ok": False,
                "returncode": 1,
                "stdout_tail": "",
                "stderr_tail": "Cannot auto-heal invalid/missing DISCORD_BOT_TOKEN — needs Fred reset in Developer Portal",
            },
        )
        return actions

    py = str(VENV_PY if VENV_PY.exists() else sys.executable)

    # Data path: refresh WTI + deploy when missing/bad/gate failed on WTI
    need_wti = bool(
        classes & {"missing_wti_data", "bad_wti_values", "quality_gate_failed", "pipeline_state_stale"}
    )
    if need_wti:
        add(
            "pipeline_s09_wti",
            run_cmd(
                [py, str(PIPELINE_PY), "--output-base", str(data_root()), "--step", "s09_wti"],
                timeout=STEP_TIMEOUT,
                cwd=str(REPO),
            ),
        )
        # Only deploy if s09 ok OR we still want to try push existing parquet
        add(
            "pipeline_s11_deploy",
            run_cmd(
                [py, str(PIPELINE_PY), "--output-base", str(data_root()), "--step", "s11_deploy"],
                timeout=STEP_TIMEOUT,
                cwd=str(REPO),
            ),
        )

    # Always attempt re-post when we got this far (unless auth blocked).
    # daily_report honors its posted sentinel, so this cannot duplicate a post.
    # Append marker to daily_report.log
    try:
        with open(DAILY_LOG, "a") as f:
            f.write(
                f"\n=== crowd_report_watchdog heal re-run {datetime.now().astimezone().isoformat()} ===\n"
            )
    except Exception:
        pass

    report_cmd = [py, str(DAILY_REPORT)]
    add(
        "rerun_daily_report",
        run_cmd(report_cmd, timeout=REPORT_TIMEOUT, cwd=str(BOT_DIR)),
    )

    # Lightweight code-path smoke: import quality_gate path by re-running is enough.
    # If daily_report threw, capture and attempt one known safe fix class later.
    last_report = actions[-1] if actions else {}
    if not last_report.get("ok"):
        combined = (last_report.get("stdout_tail") or "") + "\n" + (last_report.get("stderr_tail") or "")
        # Known safe fix: missing BOT_TOKEN already handled. Syntax/import errors need human.
        if "ModuleNotFoundError" in combined or "SyntaxError" in combined:
            add(
                "code_bug_detected",
                {
                    "ok": False,
                    "returncode": 1,
                    "stdout_tail": combined[-1500:],
                    "stderr_tail": "Code-level failure — not auto-patching production bot in unattended cron. Needs operator/agent session.",
                },
            )
        elif "GATE FAIL" in combined:
            # Second chance after the data heal (sentinel prevents duplicates)
            add(
                "rerun_daily_report_retry",
                run_cmd(report_cmd, timeout=REPORT_TIMEOUT, cwd=str(BOT_DIR)),
            )

    return actions


def recheck(today: date, expected: dict[str, float | None]) -> dict:
    msgs = fetch_recent_messages(CROWD_REPORTS_CHANNEL, limit=10)
    report = find_today_crowd_report(msgs, today)
    # refresh expected after heal (data may have landed)
    expected2 = get_wdw_wti_today(today)
    # prefer fresh expected if available
    for k, v in expected2.items():
        if v is not None:
            expected[k] = v
    return validate_report(report, expected, today) | {
        "report": {
            "id": (report or {}).get("id"),
            "title": (report or {}).get("title"),
            "timestamp": (report or {}).get("timestamp"),
        }
        if report
        else None,
        "expected_wti": expected,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Morning #crowd-reports watchdog + heal")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--check-only", action="store_true", help="Detect only; no heal")
    ap.add_argument("--force-heal", action="store_true", help="Heal even if post looks OK")
    ap.add_argument("--no-post", action="store_true", help="Skip #bot-commands embed")
    args = ap.parse_args()
    quiet = args.quiet

    today = date.today()
    started = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "started": started,
        "date": today.isoformat(),
        "ok": False,
        "healed": False,
        "channel_crowd": CROWD_REPORTS_CHANNEL,
        "channel_bot_commands": BOT_COMMANDS_CHANNEL,
    }

    try:
        me = discord_me()
        result["discord_me"] = {k: me.get(k) for k in ("ok", "status", "username")}
        log(f"discord @me ok={me.get('ok')} user={me.get('username')}", quiet)

        expected = get_wdw_wti_today(today)
        result["expected_wti"] = expected
        log(f"DuckDB WDW WTI today={expected}", quiet)

        msgs = fetch_recent_messages(CROWD_REPORTS_CHANNEL, limit=15)
        report = find_today_crowd_report(msgs, today)
        validation = validate_report(report, expected, today)
        result["initial"] = {
            "ok": validation["ok"],
            "reasons": validation["reasons"],
            "extracted": validation.get("extracted"),
            "message_id": validation.get("message_id"),
            "title": (report or {}).get("title"),
        }
        log(f"initial ok={validation['ok']} reasons={validation['reasons']}", quiet)

        need_heal = args.force_heal or not validation["ok"]
        actions: list[dict] = []
        final = validation

        if need_heal and args.check_only:
            diagnosis = diagnose(expected, me, validation)
            result["diagnosis"] = diagnosis
            result["ok"] = False
            result["check_only"] = True
        elif need_heal:
            diagnosis = diagnose(expected, me, validation)
            result["diagnosis"] = diagnosis
            log(f"diagnosis primary={diagnosis.get('primary')} classes={diagnosis.get('classes')}", quiet)
            actions = heal(diagnosis, quiet)
            result["actions"] = [
                {
                    "action": a.get("action"),
                    "ok": a.get("ok"),
                    "returncode": a.get("returncode"),
                    "stderr_tail": (a.get("stderr_tail") or "")[:500],
                    "stdout_tail": (a.get("stdout_tail") or "")[-800:],
                }
                for a in actions
            ]
            # recheck after heal
            final = recheck(today, dict(expected))
            result["final"] = {
                "ok": final["ok"],
                "reasons": final["reasons"],
                "extracted": final.get("extracted"),
                "message_id": final.get("message_id") or (final.get("report") or {}).get("id"),
                "report": final.get("report"),
                "expected_wti": final.get("expected_wti"),
            }
            result["healed"] = True
            result["ok"] = bool(final["ok"])
            log(f"final ok={final['ok']} reasons={final['reasons']}", quiet)
        else:
            result["ok"] = True
            result["final"] = result["initial"]
            final = validation

        # Status embed → #bot-commands
        ok = bool(result["ok"])
        if args.force_heal and ok:
            title_prefix = "✅ crowd-report watchdog (forced heal OK)"
        elif ok and result.get("healed"):
            title_prefix = "✅ crowd-report watchdog — HEALED"
        elif ok:
            title_prefix = "✅ crowd-report watchdog"
        else:
            title_prefix = "❌ crowd-report watchdog"

        color = 0x2ECC71 if ok else 0xE74C3C
        exp_s = ", ".join(
            f"{k}={v:.1f}" if v is not None else f"{k}=∅" for k, v in (result.get("expected_wti") or {}).items()
        )
        action_lines = []
        for a in result.get("actions") or []:
            mark = "ok" if a.get("ok") else "FAIL"
            action_lines.append(f"• `{a.get('action')}` → **{mark}** (rc={a.get('returncode')})")
        if not action_lines:
            action_lines = ["• (no heal actions)"]

        reasons = (result.get("final") or result.get("initial") or {}).get("reasons") or []
        msg_id = (result.get("final") or result.get("initial") or {}).get("message_id")
        diag = result.get("diagnosis") or {}
        desc = (
            f"**Date:** {today.isoformat()}\n"
            f"**#crowd-reports:** {'PASS' if ok else 'FAIL'}"
            f"{' (after heal)' if result.get('healed') else ''}\n"
            f"**DuckDB WDW:** {exp_s}\n"
            f"**Message id:** `{msg_id or 'n/a'}`\n"
            f"**Check:** {'; '.join(reasons)[:800]}\n"
        )
        if diag:
            desc += (
                f"**Diagnosis:** `{diag.get('primary')}` "
                f"classes={diag.get('classes')}\n"
                f"**Notes:** {'; '.join(diag.get('notes') or [])[:500]}\n"
            )
        desc += "**Actions:**\n" + "\n".join(action_lines[:12])
        if not ok and (result.get("actions") or []):
            # include last failure tail
            fails = [a for a in (result.get("actions") or []) if not a.get("ok")]
            if fails:
                last = fails[-1]
                tail = (last.get("stderr_tail") or last.get("stdout_tail") or "")[-700:]
                if tail:
                    desc += f"\n\n**Last fail tail (`{last.get('action')}`):**\n```\n{tail}\n```"

        if not args.no_post:
            post = post_embed(
                f"{title_prefix} — {today.isoformat()}",
                desc,
                color=color,
            )
            result["post"] = post
            if not post.get("ok"):
                log(f"bot-commands post failed: {post}", quiet)
                # don't flip ok if crowd-report itself is fine
        else:
            result["post"] = {"skipped": True}

        result["finished"] = datetime.now(timezone.utc).isoformat()
        _write_result(json.dumps(result, indent=2, default=str))
        log(
            f"{'PASS' if ok else 'FAIL'} healed={result.get('healed')} "
            f"post_id={(result.get('post') or {}).get('id')}",
            quiet,
        )
        return 0 if ok else 1

    except Exception as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()[-2500:]
        try:
            if not args.no_post:
                result["post"] = post_embed(
                    f"❌ crowd-report watchdog — {today.isoformat()}",
                    f"```{type(e).__name__}: {e}```\n```{traceback.format_exc()[-1500:]}```",
                    color=0xE74C3C,
                )
        except Exception as pe:
            result["post_error"] = str(pe)
        _write_result(json.dumps(result, indent=2, default=str))
        log(f"FAIL exception: {e}", quiet)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
