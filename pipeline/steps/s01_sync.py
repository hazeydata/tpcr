"""Step 1: S3 Sync.

Mirrors s3://<bucket>/export/<source>/ into <raw_data_dir>/export/<source>/ in
batches of (source, S3 LastModified date). After each batch a checkpoint is
written atomically, so a timed-out or killed run resumes at the first
uncommitted batch. On timeout the step writes status `sync_partial` and raises
SyncPartial; pipeline.py exits 75 and downstream steps do not run.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections import OrderedDict
from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.core.logging import PipelineLogger

EXIT_SYNC_PARTIAL = 75
STATUS_FILE = "s01_sync_status.json"
CHECKPOINT_FILE = "s01_sync_checkpoint.json"
INCLUDE_CHUNK = 200


class SyncPartial(Exception):
    pass


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _state_dir(cfg: PipelineConfig) -> Path:
    return Path(cfg.prod_output_base) / "state"


def read_status(cfg: PipelineConfig) -> dict:
    return _read_json(_state_dir(cfg) / STATUS_FILE)


def downstream_blocked(cfg: PipelineConfig) -> str | None:
    status = read_status(cfg).get("status")
    if status is None or status == "sync_complete":
        return None
    return status


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _list_source(cfg: PipelineConfig, source: str, deadline: float) -> list[tuple[str, str, str, int]]:
    prefix = f"s3://{cfg.s3_bucket}/export/{source}/"
    r = subprocess.run(
        ["aws", "s3", "ls", prefix, "--recursive"],
        capture_output=True, text=True, timeout=max(_remaining(deadline), 0.001),
    )
    if r.returncode != 0:
        raise RuntimeError(f"S3 list failed for {source} (rc={r.returncode}): {r.stderr[:500]}")
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4 or not parts[3].endswith(".csv"):
            continue
        day, clock, size, key = parts
        out.append((key, day, f"{day} {clock}", int(size)))
    return out


def _plan(cfg: PipelineConfig, deadline: float) -> "OrderedDict[str, dict[str, str]]":
    batches: dict[str, dict[str, str]] = {}
    for source in cfg.s3_sync_sources:
        for key, day, stamp, size in _list_source(cfg, source, deadline):
            batches.setdefault(f"{source}/{day}", {})[key] = f"{stamp} {size}"
    return OrderedDict(sorted(batches.items()))


def _fetch(cfg: PipelineConfig, source: str, keys: list[str], deadline: float) -> None:
    root = Path(cfg.raw_data_dir)
    src = f"s3://{cfg.s3_bucket}/export/{source}/"
    dst = root / "export" / source
    dst.mkdir(parents=True, exist_ok=True)
    rel_prefix = f"export/{source}/"
    for i in range(0, len(keys), INCLUDE_CHUNK):
        argv = ["aws", "s3", "sync", src, str(dst) + "/", "--no-progress", "--only-show-errors", "--exclude", "*"]
        for k in keys[i:i + INCLUDE_CHUNK]:
            argv += ["--include", k[len(rel_prefix):]]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=max(_remaining(deadline), 0.001))
        if r.returncode != 0:
            raise RuntimeError(f"S3 sync failed for {source} (rc={r.returncode}): {r.stderr[:500]}")


def run(cfg: PipelineConfig, log: PipelineLogger) -> dict:
    log.info("=" * 60)
    log.info("STEP 1: S3 SYNC")
    log.info("=" * 60)

    if cfg.shadow:
        log.info("Shadow mode: using production data, no separate sync needed")
        return {"rows": 0, "action": "skipped_shadow"}

    timeout_s = int(cfg.s01_sync_timeout_s)
    log.info(f"S3 sync timeout: {timeout_s}s (S01_SYNC_TIMEOUT_S)", timeout_s=timeout_s)
    deadline = time.monotonic() + timeout_s
    state = _state_dir(cfg)
    ckpt_path = state / CHECKPOINT_FILE
    status_path = state / STATUS_FILE
    _write_json_atomic(status_path, {"status": "sync_running", "timeout_s": timeout_s})

    done = done_batches = 0
    total = 0
    try:
        plan = _plan(cfg, deadline)
        total = len(plan)
        ckpt = _read_json(ckpt_path)
        for batch_id, objects in plan.items():
            if ckpt.get(batch_id) == objects:
                done_batches += 1
                continue
            committed = ckpt.get(batch_id, {})
            stale = sorted(k for k, sig in objects.items() if committed.get(k) != sig)
            if _remaining(deadline) <= 0:
                raise subprocess.TimeoutExpired("s01_sync", timeout_s)
            _fetch(cfg, batch_id.split("/", 1)[0], stale, deadline)
            ckpt[batch_id] = objects
            _write_json_atomic(ckpt_path, ckpt)
            done += 1
            done_batches += 1
            log.info(f"Batch {batch_id} committed ({len(stale)} file(s))")
    except subprocess.TimeoutExpired:
        _write_json_atomic(status_path, {
            "status": "sync_partial", "timeout_s": timeout_s,
            "batches_done": done_batches, "batches_total": total,
        })
        log.warning(f"S3 sync hit {timeout_s}s timeout: {done_batches}/{total} batches committed (sync_partial)")
        raise SyncPartial(f"sync_partial: {done_batches}/{total} batches committed")
    except Exception:
        _write_json_atomic(status_path, {"status": "sync_failed", "timeout_s": timeout_s})
        raise

    _write_json_atomic(status_path, {
        "status": "sync_complete", "timeout_s": timeout_s,
        "batches_done": done_batches, "batches_total": total,
    })
    log.info(f"S3 sync complete: {done} batch(es) fetched, {total - done} already committed")
    return {"rows": 0, "action": "synced", "batches_fetched": done, "batches_total": total}
