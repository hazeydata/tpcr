import argparse
import json
import os
import sys
from pathlib import Path

ALERT_REASONS = ("drift", "systematic", "inputs_stale")
DEFAULT_TO = "fred@hazeydata.ai"


def recipient():
    return os.environ.get("TPCR_DRIFT_ALERT_TO") or DEFAULT_TO


def alert_flags(status):
    return sorted((f for f in status.get("flags", []) if f["reason"] in ALERT_REASONS), key=lambda f: (f["level"], f["key"]))


def build(status):
    flags = alert_flags(status)
    if not flags:
        return None
    lines = [f"Subject: TPCR drift: {len(flags)} flag(s) as of {status['as_of']}", f"To: {recipient()}", ""]
    lines += [f"{f['key']} {f['level']} {f['reason']} value={f['value']} threshold={f['threshold']} window={f['window_days']}" for f in flags]
    lines += ["", f"scorecard: {status.get('scorecard', 'none')}"]
    return "\n".join(lines) + "\n"


def unique_path(d, stem):
    p, i = d / f"{stem}.eml.txt", 2
    while p.exists():
        p, i = d / f"{stem}_r{i}.eml.txt", i + 1
    return p


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    status = json.loads(a.status.read_text())
    text = build(status)
    if text is None:
        print("no_alert")
        return 0
    out = (a.out or Path(os.environ["TPCR_SCORECARD_DIR"])) / "alerts"
    out.mkdir(parents=True, exist_ok=True)
    p = unique_path(out, f"alert_{status['as_of']}")
    p.write_text(text)
    print(p)
    print("dry_run" if a.dry_run else "awaiting_approval")
    return 0


if __name__ == "__main__":
    sys.exit(main())
