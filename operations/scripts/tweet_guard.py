#!/usr/bin/env python3
"""Pre-post guard for operations/scripts/wti_{observed,predicted}_tweet.py.

The launchd jobs com.hazeydata.tpcr-tweet-observed and com.hazeydata.tpcr-tweet-predicted run these repo scripts.
A live post is blocked (exit 3) when a crontab line still runs a repo tweet script, or when a loaded tweet job
still points at an old ~/hazeydata/scripts/wti_*_tweet_local.py. Unreadable crontab/launchctl blocks too.

Usage:
  tweet_guard.py status
  tweet_guard.py check     exit 0 only if a repo tweet script may post
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
    return cfg["legacy_cron_patterns"], cfg["launchd_labels"], cfg["local_script_patterns"]


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


def read_job(label, runner=_run):
    return runner(["launchctl", "print", f"gui/{os.getuid()}/{label}"])


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


def loaded_labels(text, labels):
    loaded = set()
    for line in text.splitlines():
        cols = line.split()
        if cols and cols[-1] in labels:
            loaded.add(cols[-1])
    return [l for l in labels if l in loaded]


def legacy_side(cron_patterns, runner=_run):
    text, err = read_crontab(runner)
    if text is None:
        return None, err
    return active_cron_lines(text, cron_patterns), ""


def launchd_side(labels, local_patterns, runner=_run):
    text, err = read_launchctl(runner)
    if text is None:
        return None, err
    regs = [re.compile(p) for p in local_patterns]
    jobs = []
    for label in loaded_labels(text, labels):
        body, err = read_job(label, runner)
        if body is None:
            return None, f"{label}: {err}"
        local = sorted({m.group(0) for r in regs for m in r.finditer(body)})
        jobs.append((label, local))
    return jobs, ""


def preflight(runner=_run, config=CONFIG_FILE):
    cron_patterns, labels, local_patterns = load_config(config)
    hits, err = legacy_side(cron_patterns, runner)
    if hits is None:
        raise GuardBlocked("crontab_unreadable", err)
    if hits:
        raise GuardBlocked("legacy_cron_active", "; ".join(hits))
    jobs, err = launchd_side(labels, local_patterns, runner)
    if jobs is None:
        raise GuardBlocked("launchctl_unreadable", err)
    stale = [f"{label} -> {', '.join(local)}" for label, local in jobs if local]
    if stale:
        raise GuardBlocked("launchd_runs_local_script", "; ".join(stale))


def enforce(check):
    try:
        check()
    except GuardBlocked as e:
        print(f"tweet_guard: {e.reason} {e.detail}".rstrip(), file=sys.stderr)
        sys.exit(3)


def status(runner=_run, config=CONFIG_FILE):
    cron_patterns, labels, local_patterns = load_config(config)
    cron_hits, cron_err = legacy_side(cron_patterns, runner)
    jobs, job_err = launchd_side(labels, local_patterns, runner)
    lines = []
    if cron_hits is None:
        lines.append(f"legacy_cron: unreadable ({cron_err})")
    else:
        lines.append(f"legacy_cron: {'active' if cron_hits else 'inactive'} ({len(cron_hits)} line(s))")
        lines.extend(f"  {h}" for h in cron_hits)
    if jobs is None:
        lines.append(f"launchd: unreadable ({job_err})")
    else:
        lines.append(f"launchd: {len(jobs)} of {len(labels)} tweet job(s) loaded")
        loaded = dict(jobs)
        for label in labels:
            if label not in loaded:
                lines.append(f"  {label}: not loaded")
            elif loaded[label]:
                lines.append(f"  {label}: runs local script ({', '.join(loaded[label])})")
            else:
                lines.append(f"  {label}: runs repo script")
    ok = cron_hits == [] and jobs is not None and not any(local for _, local in jobs)
    lines.append(f"allowed_post: {'yes' if ok else 'no'}")
    return "\n".join(lines)


def main(argv=None, runner=_run):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(status(runner))
        return 0
    if cmd == "check":
        try:
            preflight(runner)
        except GuardBlocked as e:
            print(f"tweet_guard: {e.reason} {e.detail}".rstrip(), file=sys.stderr)
            return 3
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
