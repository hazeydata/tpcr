#!/usr/bin/env python3
"""Pre-post guard for operations/scripts/wti_{observed,predicted}_tweet.py.

The launchd jobs com.hazeydata.tpcr-tweet-observed and com.hazeydata.tpcr-tweet-predicted run these repo scripts.
A live post is allowed only when no crontab line runs a repo tweet script and every loaded tweet job runs exactly
<python> <checkout>/<its script> (one arguments block, absolute paths without ~, resolved; a program line, if any,
must be that same <python>; checkout, python and scripts from config/tweet_guard.json,
checkout overridable by TPCR_TWEET_CHECKOUT, python by TPCR_TWEET_PYTHON). Anything else blocks (exit 3):
an old wti_*_tweet_local.py, another program, no arguments, or unreadable crontab/launchctl.

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


class Config:
    def __init__(self, cron_patterns, jobs, python, local_patterns):
        self.cron_patterns = cron_patterns
        self.jobs = jobs
        self.labels = list(jobs)
        self.python = python
        self.local_patterns = local_patterns


def _real(path):
    p = Path(path).expanduser()
    return str(Path(os.path.realpath(p.parent)) / p.name)


def load_config(path=CONFIG_FILE, environ=os.environ):
    cfg = json.loads(Path(path).read_text())
    checkout = Path(environ.get("TPCR_TWEET_CHECKOUT") or cfg["checkout"]).expanduser()
    python = checkout / Path(environ.get("TPCR_TWEET_PYTHON") or cfg["python"]).expanduser()
    jobs = {label: [_real(python), os.path.realpath(checkout / script)] for label, script in cfg["launchd_jobs"].items()}
    return Config(cfg["legacy_cron_patterns"], jobs, _real(python), cfg["local_script_patterns"])


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


def job_arguments(body):
    """Arguments of the single `arguments = {` block; None if absent, unclosed or repeated (launchd's would be ambiguous)."""
    lines = body.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "arguments = {"]
    if len(starts) != 1:
        return None
    args = []
    for inner in lines[starts[0] + 1:]:
        if inner.strip() == "}":
            return args
        args.append(inner.strip())
    return None


def job_programs(body):
    return [line.strip()[len("program = "):] for line in body.splitlines() if line.strip().startswith("program = ")]


def _literal_abs(arg):
    # launchd neither expands ~ nor resolves relative paths against our cwd, so only literal absolute paths can match.
    return "~" not in arg and os.path.isabs(arg)


def job_verdict(body, expected, local_patterns):
    local = sorted({m.group(0) for p in local_patterns for m in re.finditer(p, body)})
    if local:
        return "local", ", ".join(local)
    blocks = sum(1 for line in body.splitlines() if line.strip() == "arguments = {")
    if blocks > 1:
        return "unexpected", f"{blocks} arguments blocks"
    args = job_arguments(body)
    if not args:
        return "no_arguments", ""
    if not all(_literal_abs(a) for a in args):
        return "unexpected", " ".join(args)
    programs = job_programs(body)
    if len(programs) > 1 or (programs and (not _literal_abs(programs[0]) or _real(programs[0]) != expected[0])):
        return "unexpected", "program = " + "; ".join(programs)
    resolved = [_real(args[0])] + [os.path.realpath(a) for a in args[1:]]
    if resolved != expected:
        return "unexpected", " ".join(args)
    return "repo", ""


def legacy_side(cron_patterns, runner=_run):
    text, err = read_crontab(runner)
    if text is None:
        return None, err
    return active_cron_lines(text, cron_patterns), ""


def launchd_side(cfg, runner=_run):
    text, err = read_launchctl(runner)
    if text is None:
        return None, err
    jobs = []
    for label in loaded_labels(text, cfg.labels):
        body, err = read_job(label, runner)
        if body is None:
            return None, f"{label}: {err}"
        jobs.append((label, *job_verdict(body, cfg.jobs[label], cfg.local_patterns)))
    return jobs, ""


BLOCK_REASON = {"local": "launchd_runs_local_script", "no_arguments": "launchd_job_no_arguments",
                "unexpected": "launchd_unexpected_program"}


def preflight(runner=_run, config=CONFIG_FILE):
    cfg = load_config(config)
    hits, err = legacy_side(cfg.cron_patterns, runner)
    if hits is None:
        raise GuardBlocked("crontab_unreadable", err)
    if hits:
        raise GuardBlocked("legacy_cron_active", "; ".join(hits))
    jobs, err = launchd_side(cfg, runner)
    if jobs is None:
        raise GuardBlocked("launchctl_unreadable", err)
    bad = [(label, verdict, detail) for label, verdict, detail in jobs if verdict != "repo"]
    if bad:
        raise GuardBlocked(BLOCK_REASON[bad[0][1]], "; ".join(f"{l} -> {d or v}" for l, v, d in bad))


def enforce(check):
    try:
        check()
    except GuardBlocked as e:
        print(f"tweet_guard: {e.reason} {e.detail}".rstrip(), file=sys.stderr)
        sys.exit(3)


STATUS_TEXT = {"repo": "runs repo script", "local": "runs local script", "no_arguments": "no program arguments",
               "unexpected": "runs unexpected program"}


def status(runner=_run, config=CONFIG_FILE):
    cfg = load_config(config)
    cron_hits, cron_err = legacy_side(cfg.cron_patterns, runner)
    jobs, job_err = launchd_side(cfg, runner)
    lines = []
    if cron_hits is None:
        lines.append(f"legacy_cron: unreadable ({cron_err})")
    else:
        lines.append(f"legacy_cron: {'active' if cron_hits else 'inactive'} ({len(cron_hits)} line(s))")
        lines.extend(f"  {h}" for h in cron_hits)
    if jobs is None:
        lines.append(f"launchd: unreadable ({job_err})")
    else:
        lines.append(f"launchd: {len(jobs)} of {len(cfg.labels)} tweet job(s) loaded")
        loaded = {label: (verdict, detail) for label, verdict, detail in jobs}
        for label in cfg.labels:
            if label not in loaded:
                lines.append(f"  {label}: not loaded")
                continue
            verdict, detail = loaded[label]
            lines.append(f"  {label}: {STATUS_TEXT[verdict]}" + (f" ({detail})" if detail else ""))
    lines.append(f"expected: {cfg.python} <checkout>/operations/scripts/wti_*_tweet.py")
    ok = cron_hits == [] and jobs is not None and all(v == "repo" for _, v, _ in jobs)
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
