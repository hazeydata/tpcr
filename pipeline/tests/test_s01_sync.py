import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures" / "s01"

RUNNER = """
import sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from pipeline.config import load_config
from pipeline.core.logging import PipelineLogger
from pipeline.steps import s01_sync
cfg = load_config(output_base=Path(sys.argv[1]))
try:
    s01_sync.run(cfg, PipelineLogger("s01_sync"))
except s01_sync.SyncPartial:
    sys.exit(s01_sync.EXIT_SYNC_PARTIAL)
"""


def make_s3(root: Path, days: int, props=("wdw", "dlr"), entities=4, rows=50):
    manifest = {}
    for source in ("wait_times", "fastpass_times"):
        for d in range(days):
            day = f"2026-10-{10 + d:02d}"
            for prop in props:
                for e in range(entities):
                    key = f"export/{source}/{prop}/{day}/{prop}{e:02d}.csv"
                    p = root / key
                    p.parent.mkdir(parents=True, exist_ok=True)
                    lines = ["entity,observed_at,wait"] + [
                        f"{source}:{prop}{e:02d},{day}T{h:02d}:00,{(h * 7 + e) % 90}" for h in range(rows)
                    ]
                    p.write_text("\n".join(lines) + "\n")
                    manifest[key] = f"{day} 05:00:00"
    (root / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def tree_digest(base: Path):
    h = hashlib.sha256()
    for p in sorted((base / "raw").rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(base)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def all_rows(base: Path):
    rows = []
    for p in sorted((base / "raw").rglob("*.csv")):
        rows += p.read_text().splitlines()[1:]
    return rows


class S01Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.s3 = self.tmp / "s3"
        self.manifest = make_s3(self.s3, days=7)
        self.env = dict(os.environ)
        self.env["PATH"] = f"{FIX}:{self.env['PATH']}"
        self.env["FAKE_S3_ROOT"] = str(self.s3)
        self.env.pop("S01_SYNC_TIMEOUT_S", None)

    def start(self, out: Path, **extra):
        env = dict(self.env, **extra)
        return subprocess.Popen(
            [sys.executable, "-c", RUNNER.format(repo=str(REPO)), str(out)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def run_step(self, out: Path, **extra):
        p = self.start(out, **extra)
        stdout, _ = p.communicate(timeout=300)
        return p.returncode, stdout

    def status(self, out: Path):
        return json.loads((out / "state" / "s01_sync_status.json").read_text())

    def ckpt(self, out: Path):
        return json.loads((out / "state" / "s01_sync_checkpoint.json").read_text())

    def test_default_timeout_logged_and_env_override(self):
        rc, out = self.run_step(self.tmp / "a")
        self.assertEqual(rc, 0, out)
        self.assertIn("S3 sync timeout: 1800s", out)
        rc, out = self.run_step(self.tmp / "b", S01_SYNC_TIMEOUT_S="42")
        self.assertEqual(rc, 0, out)
        self.assertIn("S3 sync timeout: 42s", out)

    def test_seven_day_catchup_within_default_timeout(self):
        out = self.tmp / "a"
        rc, log = self.run_step(out)
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.status(out)["status"], "sync_complete")
        self.assertEqual(self.status(out)["batches_total"], 14)
        self.assertEqual(len(self.ckpt(out)), 14)
        self.assertEqual(len(list((out / "raw").rglob("*.csv"))), len(self.manifest))

    def test_kill_midrun_then_resume_matches_uninterrupted(self):
        ref = self.tmp / "ref"
        rc, log = self.run_step(ref)
        self.assertEqual(rc, 0, log)

        out = self.tmp / "killed"
        calls = self.tmp / "calls.log"
        p = self.start(out, FAKE_AWS_DELAY="0.3", FAKE_AWS_LOG=str(calls))
        ck = out / "state" / "s01_sync_checkpoint.json"
        deadline = time.time() + 120
        while time.time() < deadline:
            if ck.exists() and len(json.loads(ck.read_text())) >= 4:
                break
            time.sleep(0.05)
        p.send_signal(signal.SIGKILL)
        p.wait()
        committed = len(json.loads(ck.read_text()))
        self.assertGreaterEqual(committed, 4)
        self.assertLess(committed, 14)
        self.assertEqual(self.status(out)["status"], "sync_running")

        calls.write_text("")
        rc, log = self.run_step(out, FAKE_AWS_LOG=str(calls))
        self.assertEqual(rc, 0, log)
        syncs = [l for l in calls.read_text().splitlines() if l == "s3 sync"]
        self.assertLessEqual(len(syncs), 14 - committed + 1)
        self.assertEqual(tree_digest(out), tree_digest(ref))
        self.assertEqual(self.ckpt(out), self.ckpt(ref))
        rows = all_rows(out)
        self.assertEqual(len(rows), len(set(rows)))
        self.assertEqual(sorted(rows), sorted(all_rows(ref)))

    def test_timeout_is_partial_75_then_resume_completes(self):
        out = self.tmp / "a"
        rc, log = self.run_step(out, FAKE_AWS_DELAY="0.4", S01_SYNC_TIMEOUT_S="2")
        self.assertEqual(rc, 75, log)
        st = self.status(out)
        self.assertEqual(st["status"], "sync_partial")
        self.assertLess(st["batches_done"], st["batches_total"])
        rc, log = self.run_step(out)
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.status(out)["status"], "sync_complete")
        ref = self.tmp / "ref"
        self.run_step(ref)
        self.assertEqual(tree_digest(out), tree_digest(ref))

    def test_rerun_noop_does_not_fetch(self):
        out = self.tmp / "a"
        self.run_step(out)
        calls = self.tmp / "calls.log"
        rc, log = self.run_step(out, FAKE_AWS_LOG=str(calls))
        self.assertEqual(rc, 0, log)
        self.assertNotIn("s3 sync", calls.read_text())

    def test_new_day_fetches_only_new_batch(self):
        out = self.tmp / "a"
        self.run_step(out)
        obj = "export/wait_times/wdw/2026-10-17/wdw00.csv"
        (self.s3 / obj).parent.mkdir(parents=True)
        (self.s3 / obj).write_text("entity,observed_at,wait\nwdw00,2026-10-17T08:00,12\n")
        self.manifest[obj] = "2026-10-17 05:00:00"
        (self.s3 / "manifest.json").write_text(json.dumps(self.manifest))
        calls = self.tmp / "calls.log"
        rc, log = self.run_step(out, FAKE_AWS_LOG=str(calls))
        self.assertEqual(rc, 0, log)
        self.assertEqual(calls.read_text().splitlines().count("s3 sync"), 1)
        self.assertTrue((out / "raw" / obj).exists())


class DownstreamGate(unittest.TestCase):
    def run_pipeline(self, base: Path, status):
        state = base / "state"
        state.mkdir(parents=True, exist_ok=True)
        if status:
            (state / "s01_sync_status.json").write_text(json.dumps({"status": status}))
        return subprocess.run(
            [sys.executable, str(REPO / "pipeline" / "pipeline.py"), "--step", "s12_validate", "--output-base", str(base)],
            capture_output=True, text=True, timeout=300,
        )

    def test_partial_skips_downstream_with_75(self):
        base = Path(tempfile.mkdtemp())
        r = self.run_pipeline(base, "sync_partial")
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertIn("Skipping s12_validate: s01_sync status is sync_partial", r.stdout + r.stderr)

    def test_interrupted_run_also_blocks(self):
        base = Path(tempfile.mkdtemp())
        r = self.run_pipeline(base, "sync_running")
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
