"""
Daily Crowd Report — Posts to #crowd-reports every morning.

Runs on the Mac Mini via launchd (launchd/com.hazeydata.tpcr-crowd-report.plist,
07:00 local) with --wait-for-pipeline so it posts only after the 06:00 pipeline
has written pipeline_metrics_<date>.json with status "done".

Data root: TPCR_DATA_ROOT (env / ~/.env) else ~/hazeydata/pipeline.
Optional TPCR_DUCKDB_PATH overrides the live DuckDB file.

Post state lives in <root>/state/crowd_report_posted_<date>.json, written
atomically: "pending" is claimed before the POST, then "posted" (or "failed" on
a definite non-2xx response, which does not block a retry). "pending" and
"posted" block reposting unless --force.

Exit codes:
  0 ok / already posted / dry-run
  1 post failed or no token
  2 quality gate failed
  3 pipeline marker never appeared within the wait
  4 corrupt state file
  5 pending state file (possible crash mid-post)
  6 claim (pending) write failed; nothing was posted
  7 final (posted) state write failed; pending marker remains
  8 pipeline status is sync_partial
  9 stale pending state (claimed >= 6h ago, unparseable claimed_at, or for another
    run_date); never reposted automatically, check #crowd-reports then --force

  daily_report.py --dry-run              # gate + render to stdout, never posts, writes nothing
  daily_report.py --wait-for-pipeline 90
  daily_report.py --force                # override existing post state
  daily_report.py --skip-pipeline-check  # manual use only
"""

import argparse
import json as _json
import os
import sys
import tempfile
import time
import requests
import duckdb
from datetime import date, datetime as _dt, timedelta as _td, timezone as _tz
from pathlib import Path as _Path
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.env"))

CROWD_REPORTS_CHANNEL = "1478240066382860298"  # #crowd-reports (recreated Mar 2)

DATA_ROOT_ENV = "TPCR_DATA_ROOT"
DUCKDB_PATH_ENV = "TPCR_DUCKDB_PATH"
DEFAULT_DATA_ROOT = "~/hazeydata/pipeline"
STALE_PENDING_HOURS = 6


def data_root() -> _Path:
    """Data root resolved at call time (env may be loaded after import)."""
    return _Path(os.environ.get(DATA_ROOT_ENV) or DEFAULT_DATA_ROOT).expanduser()


def _duckdb_path() -> str:
    override = os.environ.get(DUCKDB_PATH_ENV)
    if override:
        return str(_Path(override).expanduser())
    return str(data_root() / "tpcr_live.duckdb")


def _wti_path() -> str:
    return str(data_root() / "wti" / "wti.parquet")


# Module attributes (computed after load_dotenv); helpers read these.
# main() refreshes them so a changed environment is honored.
DUCKDB_PATH = _duckdb_path()
WTI_PATH = _wti_path()
_USE_DUCKDB = os.path.exists(DUCKDB_PATH)


def _refresh_paths():
    global DUCKDB_PATH, WTI_PATH, _USE_DUCKDB
    DUCKDB_PATH = _duckdb_path()
    WTI_PATH = _wti_path()
    _USE_DUCKDB = os.path.exists(DUCKDB_PATH)


def accuracy_path() -> _Path:
    return data_root() / "accuracy" / "accuracy_summary.json"


def gate_log_path() -> _Path:
    return data_root() / "logs" / "daily_report_gate.log"


def state_path(target: date) -> _Path:
    return data_root() / "state" / f"crowd_report_posted_{target.isoformat()}.json"


def write_state_atomic(path, data: dict) -> None:
    """Write JSON to a tmp file in the same dir, fsync, then os.replace. Raises on failure."""
    path = _Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(_json.dumps(data))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return _dt.now(_tz.utc).isoformat()


def read_state(target: date):
    """Return (state_dict | None, corrupt_reason | None). None/None = no state file.

    A pending file whose run_date differs from `target` yields ("stale", reason).
    """
    path = state_path(target)
    if not path.exists():
        return None, None
    try:
        data = _json.loads(path.read_text())
    except Exception as e:
        return None, f"unparseable ({type(e).__name__})"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    if data.get("state") not in ("pending", "posted", "failed"):
        return None, f"missing or unknown state {data.get('state')!r}"
    if data.get("state") == "pending" and data.get("run_date") != target.isoformat():
        return "stale", f"pending state is for run_date {data.get('run_date')!r}, not {target.isoformat()}"
    if data.get("run_date") != target.isoformat():
        return None, f"run_date {data.get('run_date')!r} != {target.isoformat()}"
    return data, None


def marker_path(target: date) -> _Path:
    return data_root() / "logs" / f"pipeline_metrics_{target.isoformat()}.json"


# WTI code mapping (TDL and TDS now have their own WTI scores)
WTI_CODE_MAP = {}

WDW_PARKS = ["MK", "EP", "HS", "AK"]  # Must all have WTI scores

PARK_GROUPS = [
    ("🏰 Walt Disney World", [
        ("MK", "Magic Kingdom"), ("EP", "EPCOT"),
        ("HS", "Hollywood Studios"), ("AK", "Animal Kingdom"),
    ]),
    ("🎠 Disneyland Resort", [
        ("DL", "Disneyland"), ("CA", "California Adventure"),
    ]),
    ("🦈 Universal Orlando", [
        ("UF", "Universal Studios Florida"), ("IA", "Islands of Adventure"),
        ("EU", "Epic Universe"),
    ]),
    ("🎬 Universal Hollywood", [
        ("UH", "Universal Studios Hollywood"),
    ]),
    ("🗼 Tokyo Disney Resort", [
        ("TDL", "Tokyo Disneyland"),
        ("TDS", "Tokyo DisneySea"),
    ]),
]

THIN_LINE = "─" * 35
FOOTER_TEXT = "\U0001f4a1 /now [park] for live waits • /crowd [park] for forecasts • hazeydata.ai"


TERMINAL_STATUSES = ("done", "failed", "sync_partial")


def _read_marker(target: date):
    try:
        with open(marker_path(target)) as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_pipeline_status(target: date):
    """Return status string from pipeline_metrics_<date>.json, or None if missing/unreadable."""
    data = _read_marker(target)
    status = data.get("status") if data else None
    return str(status) if status is not None else None


def wait_for_pipeline(minutes: float, target: date, sleep=None, clock=None) -> str | None:
    """Poll the marker every 60s until status is terminal (done/failed/sync_partial) or timeout.

    Returns the last observed status (None if the marker never appeared).
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + minutes * 60
    while True:
        status = read_pipeline_status(target)
        if status in TERMINAL_STATUSES:
            return status
        if clock() >= deadline:
            return status
        print(f"Waiting for pipeline marker (status={status}) ...", flush=True)
        sleep(60)


def quality_gate(park_groups, get_wti_fn, today, skip_pipeline_check=False) -> tuple[bool, str]:
    """Quality gate — verify data before posting to customers.

    Returns (passed: bool, reason: str).
    A missing report is better than a broken one.
    """
    # Check 1: today's pipeline run finished (completion marker with status done)
    if not skip_pipeline_check:
        marker = marker_path(today)
        if not marker.exists():
            return False, f"Pipeline marker missing ({marker.name})"
        status = read_pipeline_status(today)
        if status != "done":
            return False, f"Pipeline status is {status!r}, expected 'done' ({marker.name})"
        marker_data = _read_marker(today) or {}
        if "run_date" not in marker_data:
            return False, f"Pipeline marker missing run_date ({marker.name})"
        marker_date = marker_data.get("run_date")
        if marker_date != today.isoformat():
            return False, f"Pipeline marker run_date {marker_date!r} != {today.isoformat()} ({marker.name})"

    # Check 2: All 4 WDW parks have WTI scores
    wdw_scores = {}
    for park_code in WDW_PARKS:
        wti = get_wti_fn(park_code, today)
        wdw_scores[park_code] = wti
        if wti is None:
            return False, f"Missing WTI for {park_code}"
        if wti <= 0:
            return False, f"Zero/negative WTI for {park_code}: {wti}"
        if wti > 70:
            return False, f"WTI out of bounds for {park_code}: {wti}"

    # Check 3: At least one non-WDW park has data (basic sanity)
    non_wdw_count = 0
    for _, parks in park_groups:
        for park_code, _ in parks:
            if park_code not in WDW_PARKS:
                wti = get_wti_fn(park_code, today)
                if wti is not None and wti > 0:
                    non_wdw_count += 1
    # Don't fail on non-WDW — just log
    if non_wdw_count == 0:
        print(f"⚠️ Warning: No non-WDW parks have data")

    return True, f"All checks passed (WDW: {wdw_scores})"


def log_gate_result(passed: bool, reason: str):
    """Log quality gate result."""
    try:
        path = gate_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": _dt.now(_tz.utc).isoformat(),
            "passed": passed,
            "reason": reason,
        }
        with open(path, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def wti_emoji(wti: float) -> str:
    if wti <= 12: return "❄️"
    elif wti <= 18: return "💎"
    elif wti <= 25: return "⚪"
    elif wti <= 34: return "🌸"
    elif wti <= 42: return "🔥"
    elif wti <= 50: return "🔴"
    else: return "💀"


def wti_label(wti: float) -> str:
    if wti <= 12: return "Well below avg WTI"
    elif wti <= 18: return "Below avg WTI"
    elif wti <= 25: return "Typical day"
    elif wti <= 34: return "Above average"
    elif wti <= 42: return "Well above average"
    elif wti <= 50: return "Very high WTI"
    else: return "Extreme WTI"


def get_embed_color(wti: float) -> int:
    if wti <= 12: return 0x0A2F8F
    elif wti <= 18: return 0x3C78D2
    elif wti <= 25: return 0xD2C8DC
    elif wti <= 34: return 0xFFB1C9
    elif wti <= 42: return 0xEB427B
    elif wti <= 50: return 0xA60038
    else: return 0x50001E


def _read_wti_parquet(wti_code: str, target_date: date):
    """Read WTI from the parquet file (fallback path)."""
    # Dedicated connection + execute(): DuckDB 2.x relations (duckdb.sql)
    # have no fetchone(), and the shared default connection rejects new
    # work while a prior execute() result is unfinished.
    _con = duckdb.connect()
    try:
        r = _con.execute(
            "SELECT wti FROM read_parquet(?) WHERE park_code = ? AND park_date = ? LIMIT 1",
            [WTI_PATH, wti_code, target_date],
        ).fetchone()
    finally:
        _con.close()
    return float(r[0]) if r else None


def get_wti(park_code: str, target_date: date) -> float:
    wti_code = WTI_CODE_MAP.get(park_code, park_code)

    if _USE_DUCKDB:
        try:
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            r = con.execute(
                "SELECT wti FROM wti WHERE park_code = ? AND park_date = ? LIMIT 1",
                [wti_code, target_date],
            ).fetchone()
            con.close()
            if r:
                return float(r[0])
        except Exception:
            pass
        # DuckDB had no row or errored — fall back to parquet
        try:
            return _read_wti_parquet(wti_code, target_date)
        except Exception:
            return None

    try:
        return _read_wti_parquet(wti_code, target_date)
    except Exception:
        return None


def load_accuracy():
    """Load accuracy_summary.json, or None (with a note) if unavailable."""
    try:
        with open(accuracy_path()) as f:
            return _json.load(f)
    except Exception as e:
        print(f"⚠️ Accuracy section skipped: {e}")
        return None


def build_payload(today: date, get_wti_fn, accuracy) -> dict:
    """Pure renderer: Discord payload {"embeds": [embed]} for `today`."""
    date_display = f"{today.strftime('%A, %B')} {today.day}, {today.year}"
    desc_parts = []
    best_picks = []

    for group_name, parks in PARK_GROUPS:
        group_best_park = None
        group_best_wti = 999

        section = THIN_LINE + "\n" + group_name + "\n"
        park_lines = []
        for park_code, park_name in parks:
            wti = get_wti_fn(park_code, today)
            if wti is not None:
                label = wti_label(wti)
                park_lines.append(f"  ▸ **{park_name}** — WTI **{wti:.0f}** (*{label}*)")
                if wti < group_best_wti:
                    group_best_wti = wti
                    group_best_park = park_name
            else:
                park_lines.append(f"  ▸ **{park_name}** — No data")

        section += "\n".join(park_lines)
        desc_parts.append(section)

        if group_best_park and len(parks) > 1:
            best_picks.append((group_name, group_best_park, group_best_wti))

    if best_picks:
        picks_section = THIN_LINE + "\n⭐ **Best Picks**\n"
        pick_lines = [f"  ▸ **{park}** (WTI {wti:.0f})" for _, park, wti in best_picks]
        picks_section += "\n".join(pick_lines)
        desc_parts.append(picks_section)

    if accuracy:
        acc_lines = []
        entity_mae = accuracy.get("overall_mae")
        if entity_mae is not None:
            dates_eval = accuracy.get("dates_evaluated", 0)
            acc_lines.append(f"  ▸ Entity MAE: **{entity_mae:.1f} min** ({dates_eval} days evaluated)")

        wti_mae = accuracy.get("wti_mae")
        wti_bias = accuracy.get("wti_bias")
        if wti_mae is not None:
            wti_dates = accuracy.get("wti_dates_evaluated", 0)
            bias_s = f"{wti_bias:+.1f}" if wti_bias is not None else "n/a"
            acc_lines.append(f"  ▸ WTI MAE: **{wti_mae:.1f}** | Bias: **{bias_s}** ({wti_dates} days)")

        if acc_lines:
            desc_parts.append(THIN_LINE + "\n\U0001f4c8 **Forecast Accuracy**\n" + "\n".join(acc_lines))

    embed = {
        "title": f"\U0001f3f0 Daily Crowd Report — {date_display}",
        "color": get_embed_color(best_picks[0][2]) if best_picks else 0x0A2F8F,
        "description": "\n\n".join(desc_parts),
        "footer": {"text": FOOTER_TEXT},
    }
    return {"embeds": [embed]}


def render_text(payload: dict) -> str:
    """Plain-text view of the post (markdown emphasis stripped): title, description, footer."""
    chunks = []
    for emb in payload.get("embeds", []):
        chunks.append("\n\n".join([
            emb.get("title", ""),
            emb.get("description", ""),
            (emb.get("footer") or {}).get("text", ""),
        ]))
    return "\n\n".join(chunks).replace("**", "").replace("*", "")


def _mark_failed(today: date, message: str) -> None:
    """Best-effort: record a definite failure so a retry is allowed."""
    try:
        write_state_atomic(state_path(today), {
            "run_date": today.isoformat(), "state": "failed",
            "failed_at": _now_iso(), "error": message[:200],
        })
    except Exception as e:
        print(f"⚠️ Could not write failed state: {e}")


def post_report(payload: dict, today: date) -> int:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("❌ DISCORD_BOT_TOKEN is not set; cannot post.")
        return 1
    try:
        write_state_atomic(state_path(today), {
            "run_date": today.isoformat(), "state": "pending",
            "claimed_at": _now_iso(), "pid": os.getpid(),
        })
    except Exception as e:
        print(f"❌ Could not write pending state ({type(e).__name__}: {e}); not posting.")
        return 6
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    try:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{CROWD_REPORTS_CHANNEL}/messages",
            headers=headers, json=payload, timeout=30,
        )
    except Exception as e:
        # Outcome unknown: the pending marker stays and blocks automatic reposts.
        print(f"❌ Post failed: {type(e).__name__} (outcome unknown; pending marker kept)")
        return 1
    if not r.ok:
        print(f"❌ Failed: {r.status_code} {r.text[:200]}")
        _mark_failed(today, f"HTTP {r.status_code}")
        return 1
    try:
        message_id = r.json().get("id")
    except Exception:
        message_id = None
    try:
        write_state_atomic(state_path(today), {
            "run_date": today.isoformat(), "state": "posted",
            "message_id": message_id, "posted_at": _now_iso(),
        })
    except Exception as e:
        print(f"❌ Posted (message id {message_id}) but could not write posted state "
              f"({type(e).__name__}: {e}); pending marker remains and blocks reposts.")
        return 7
    print(f"✅ Daily report posted to #crowd-reports for {today} (message id {message_id})")
    return 0


def _pending_age_hours(state: dict):
    """Hours since claimed_at, or None if missing/unparseable."""
    try:
        claimed = _dt.fromisoformat(str(state["claimed_at"]))
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=_tz.utc)
        return (_dt.now(_tz.utc) - claimed).total_seconds() / 3600.0
    except Exception:
        return None


def check_state(today: date, force: bool):
    """Return an exit code if existing state forbids posting, else None."""
    yesterday = today - _td(days=1)
    prev, _ = read_state(yesterday)
    if isinstance(prev, dict) and prev.get("state") == "pending":
        print(f"WARN: state for {yesterday} is still pending; check #crowd-reports "
              f"for a possible missed or duplicate post.")
    state, corrupt = read_state(today)
    path = state_path(today)
    if state == "stale":
        if force:
            print(f"FORCE: overriding stale pending state ({corrupt})")
            return None
        print(f"STALE PENDING marker ({corrupt}); refusing automatic repost; "
              f"check #crowd-reports, then use --force")
        return 9
    if corrupt:
        if force:
            print(f"FORCE: overriding corrupt state ({corrupt})")
            return None
        print(f"STATE CORRUPT ({path.name}: {corrupt}); refusing to post. "
              f"Check #crowd-reports, fix or remove the file, or use --force.")
        return 4
    if state is None or state["state"] == "failed":
        return None
    st = state["state"]
    if st == "pending":
        age_h = _pending_age_hours(state)
        if age_h is None or age_h >= STALE_PENDING_HOURS:
            if force:
                print("FORCE: overriding stale pending state")
                return None
            age_s = "unknown" if age_h is None else f"{age_h:.1f}"
            print(f"STALE PENDING marker (claimed {age_s}h ago, limit {STALE_PENDING_HOURS}h); "
                  f"refusing automatic repost; check #crowd-reports, then use --force")
            return 9
    if force:
        print(f"FORCE: overriding existing state {st}")
        return None
    if st == "posted":
        print(f"Daily report for {today} already posted ({path.name}); use --force to repost.")
        return 0
    print("PENDING marker from an earlier run (possible crash mid-post); refusing automatic "
          "repost; check #crowd-reports, then use --force")
    return 5


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Daily Crowd Report")
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Target date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the gate and print the post; never POST, no token needed")
    parser.add_argument("--wait-for-pipeline", type=float, default=0, metavar="MINUTES",
                        help="Poll the pipeline marker every 60s for up to MINUTES (default 0)")
    parser.add_argument("--skip-pipeline-check", action="store_true",
                        help="Skip gate check 1 (manual use only)")
    parser.add_argument("--force", action="store_true",
                        help="Override existing post state (posted/pending/corrupt)")
    args = parser.parse_args(argv)
    today = args.date or date.today()
    if not args.dry_run:
        print(f"RUN START run_date={today.isoformat()} at {_now_iso()}", flush=True)

    _refresh_paths()

    if not args.dry_run:
        rc = check_state(today, args.force)
        if rc is not None:
            return rc

    if args.wait_for_pipeline > 0 and not args.skip_pipeline_check:
        status = wait_for_pipeline(args.wait_for_pipeline, today)
        if status is None and not args.dry_run:
            print(f"❌ Pipeline marker for {today} never appeared within {args.wait_for_pipeline:g} min.")
            return 3

    if (not args.dry_run and not args.skip_pipeline_check
            and read_pipeline_status(today) == "sync_partial"):
        print(f"❌ Pipeline status for {today} is sync_partial; not posting.")
        return 8

    # === QUALITY GATE ===
    passed, reason = quality_gate(PARK_GROUPS, get_wti, today,
                                  skip_pipeline_check=args.skip_pipeline_check)
    if not args.dry_run:
        log_gate_result(passed, reason)
    if not passed:
        print(f"GATE FAIL: {reason}")
        if args.dry_run:
            print("(dry-run: continuing to render anyway)")
        else:
            print("Skipping #crowd-reports post — a missing report is better than a broken one.")
            return 2
    else:
        print(f"GATE PASS: {reason}")

    payload = build_payload(today, get_wti, load_accuracy())

    if args.dry_run:
        print("\n===== DRY RUN: post preview (not sent) =====")
        print(render_text(payload))
        print("\n===== JSON payload =====")
        print(_json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    return post_report(payload, today)


if __name__ == "__main__":
    sys.exit(main())
