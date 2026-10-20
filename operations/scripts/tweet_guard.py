#!/usr/bin/env python3
"""Mutual-exclusion guard between legacy crontab tweet jobs and launchd _local tweet jobs.

Usage:
  tweet_guard.py status
  tweet_guard.py check-local    exit 0 only if the _local job may post
  tweet_guard.py check-legacy   exit 0 only if the legacy job may post
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("TWEET_GUARD_CONFIG") or Path(__file__).resolve().parents[2] / "config" / "tweet_guard.json")


class GuardBlocked(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def load_config(path=CONFIG_FILE):
    cfg = json.loads(Path(path).read_text())
    return cfg["legacy_cron_patterns"], cfg["local_launchd_patterns"]


def _run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr or "").strip() or f"rc={r.returncode}"
    return r.stdout, ""


def read_crontab(runner=_run):
    return runner(["crontab", "-l"])


def read_launchctl(runner=_run):
    return runner(["launchctl", "list"])


def active_cron_lines(text, patterns):
    regs = [re.compile(p) for p in patterns]
    hits = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if any(r.search(s) for r in regs):
            hits.append(s)
    return hits


def loaded_local_jobs(text, patterns):
    regs = [re.compile(p) for p in patterns]
    hits = []
    for line in text.splitlines():
        cols = line.split()
        if not cols or cols[-1] == "Label":
            continue
        if any(r.search(cols[-1]) for r in regs):
            hits.append(cols[-1])
    return hits


def legacy_side(cron_patterns, runner=_run):
    text, err = read_crontab(runner)
    if text is None:
        return None, err
    return active_cron_lines(text, cron_patterns), ""


def local_side(launchd_patterns, runner=_run):
    text, err = read_launchctl(runner)
    if text is None:
        return None, err
    return loaded_local_jobs(text, launchd_patterns), ""


def preflight_local(runner=_run, config=CONFIG_FILE):
    cron_patterns, _ = load_config(config)
    hits, err = legacy_side(cron_patterns, runner)
    if hits is None:
        raise GuardBlocked("crontab_unreadable", err)
    if hits:
        raise GuardBlocked("legacy_cron_active", "; ".join(hits))


def preflight_legacy(runner=_run, config=CONFIG_FILE):
    _, launchd_patterns = load_config(config)
    hits, err = local_side(launchd_patterns, runner)
    if hits is None:
        raise GuardBlocked("launchctl_unreadable", err)
    if hits:
        raise GuardBlocked("local_launchd_loaded", "; ".join(hits))


def enforce(preflight):
    try:
        preflight()
    except GuardBlocked as e:
        print(f"tweet_guard: {e.reason} {e.detail}".rstrip(), file=sys.stderr)
        sys.exit(3)


def status(runner=_run, config=CONFIG_FILE):
    cron_patterns, launchd_patterns = load_config(config)
    cron_hits, cron_err = legacy_side(cron_patterns, runner)
    local_hits, local_err = local_side(launchd_patterns, runner)
    lines = []
    if cron_hits is None:
        lines.append(f"legacy_cron: unreadable ({cron_err})")
    else:
        lines.append(f"legacy_cron: {'active' if cron_hits else 'inactive'} ({len(cron_hits)} line(s))")
        lines.extend(f"  {h}" for h in cron_hits)
    if local_hits is None:
        lines.append(f"local_launchd: unreadable ({local_err})")
    else:
        lines.append(f"local_launchd: {'loaded' if local_hits else 'not loaded'} ({len(local_hits)} job(s))")
        lines.extend(f"  {h}" for h in local_hits)
    local_ok = cron_hits == []
    legacy_ok = local_hits == []
    lines.append(f"allowed_local: {'yes' if local_ok else 'no'}")
    lines.append(f"allowed_legacy: {'yes' if legacy_ok else 'no'}")
    return "\n".join(lines)


def main(argv=None, runner=_run):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(status(runner))
        return 0
    if cmd in ("check-local", "check-legacy"):
        pre = preflight_local if cmd == "check-local" else preflight_legacy
        try:
            pre(runner)
        except GuardBlocked as e:
            print(f"tweet_guard: {e.reason} {e.detail}".rstrip(), file=sys.stderr)
            return 3
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
