"""Firecrawl v2 migration: the school-calendar scrapers' scrape calls, with mock HTTP only (no network, no credits).

The Firecrawl v1 API returns HTTP 410 from 2026-12-10 15:00 UTC. These tests pin the exact v2 request
(URL, method, headers, JSON body, client timeout) and the v2 REST envelope parsing ({"success", "data": {"markdown"}})
of data/school_schedules/{pipeline_v3,brave_scraper,qa_sweep}.py, plus their error paths (HTTP error,
network error, success false, missing key). A repo-wide guard keeps v1 calls out of runtime code.

Run: python -m pytest tests/test_firecrawl_v2.py -q   (or python -m unittest tests.test_firecrawl_v2)
"""
import importlib.util
import io
import json
import re
import socket
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCHED = ROOT / "data" / "school_schedules"
V2_SCRAPE = "https://api.firecrawl" + ".dev/v2/scrape"
KEY = "mock-not-a-key"


def _load(name, filename, extra_modules=None):
    """Import a script by path without its import-time side effects leaking (log file handler, socket timeout, sys.path)."""
    spec = importlib.util.spec_from_file_location(name, SCHED / filename)
    mod = importlib.util.module_from_spec(spec)
    old_timeout = socket.getdefaulttimeout()
    with mock.patch("logging.basicConfig"), mock.patch.object(sys, "path", list(sys.path)), \
            mock.patch.dict(sys.modules, extra_modules or {}):
        try:
            spec.loader.exec_module(mod)
        finally:
            socket.setdefaulttimeout(old_timeout)
    return mod


class FakeResp:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self, *a):
        return self.body


def call(fn, *args, reply):
    """Run fn with urlopen answered by `reply` (a JSON body, or an exception to raise); return (result, requests seen)."""
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append((req, timeout))
        if isinstance(reply, BaseException):
            raise reply
        return FakeResp(reply)

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        out = fn(*args)
    return out, seen


def http_error(code, body):
    return urllib.error.HTTPError(V2_SCRAPE, code, "error", hdrs=None, fp=io.BytesIO(json.dumps(body).encode()))


def v2_ok(markdown):
    return {"success": True, "data": {"markdown": markdown, "metadata": {"sourceURL": "https://d.example/cal", "statusCode": 200}}}


class _ScrapeCase(unittest.TestCase):
    """Shared request/response assertions for one firecrawl_scrape function."""
    page_timeout_ms = None
    client_timeout = None

    def scrape(self, url):
        raise NotImplementedError

    def assert_v2_request(self, seen, url):
        self.assertEqual(len(seen), 1)
        req, timeout = seen[0]
        self.assertEqual(req.full_url, V2_SCRAPE)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {KEY}")
        self.assertEqual(json.loads(req.data), {"url": url, "formats": ["markdown"], "onlyMainContent": True,
                                                "timeout": self.page_timeout_ms})
        self.assertEqual(timeout, self.client_timeout)

    def test_sends_v2_scrape_and_reads_data_markdown(self):
        out, seen = call(self.scrape, "https://d.example/cal", reply=v2_ok("# 2025-26 calendar"))
        self.assert_v2_request(seen, "https://d.example/cal")
        self.assertEqual(out, "# 2025-26 calendar")

    def test_success_false_returns_empty(self):
        out, _ = call(self.scrape, "https://d.example/cal", reply={"success": False, "error": "Request timed out"})
        self.assertEqual(out, "")

    def test_http_errors_return_empty(self):
        for code in (402, 408, 410, 429, 500):
            out, seen = call(self.scrape, "https://d.example/cal", reply=http_error(code, {"success": False, "error": "x"}))
            self.assertEqual((out, len(seen)), ("", 1), code)      # one attempt, no retry (unchanged behaviour)

    def test_network_error_returns_empty(self):
        for exc in (urllib.error.URLError("no route"), socket.timeout("timed out")):
            out, _ = call(self.scrape, "https://d.example/cal", reply=exc)
            self.assertEqual(out, "")

    def test_non_json_body_returns_empty(self):
        out, _ = call(self.scrape, "https://d.example/cal", reply=b"<html>bad gateway</html>")
        self.assertEqual(out, "")


class PipelineV3Scrape(_ScrapeCase):
    page_timeout_ms, client_timeout = 20000, 30

    @classmethod
    def setUpClass(cls):
        cls.mod = _load("fcv2_pipeline_v3", "pipeline_v3.py")

    def setUp(self):
        self.proc = object.__new__(self.mod.DistrictProcessor)
        self.proc.cost_tracker = self.mod.CostTracker()
        patcher = mock.patch.object(self.mod, "FIRECRAWL_API_KEY", KEY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scrape(self, url):
        return self.mod.DistrictProcessor.firecrawl_scrape(self.proc, url)

    def test_credit_counter_counts_answered_calls_only(self):
        call(self.scrape, "https://d.example/a", reply=v2_ok("x"))
        call(self.scrape, "https://d.example/b", reply={"success": False, "error": "x"})
        call(self.scrape, "https://d.example/c", reply=http_error(429, {"success": False}))
        self.assertEqual(self.proc.cost_tracker.firecrawl_calls, 2)

    def test_markdown_is_capped(self):
        out, _ = call(self.scrape, "https://d.example/a", reply=v2_ok("m" * (self.mod.MAX_CONTENT_CHARS + 50)))
        self.assertEqual(len(out), self.mod.MAX_CONTENT_CHARS)

    def test_no_key_makes_no_call(self):
        with mock.patch.object(self.mod, "FIRECRAWL_API_KEY", ""):
            out, seen = call(self.scrape, "https://d.example/a", reply=v2_ok("x"))
        self.assertEqual((out, seen), ("", []))


class BraveScraperScrape(_ScrapeCase):
    page_timeout_ms, client_timeout = 15000, 25

    @classmethod
    def setUpClass(cls):
        # On main, brave_scraper imports confirmation_scraper from its own directory, but that module now lives
        # only under archive/scripts/; a stub stands in so the scrape function can be tested.
        stub = types.ModuleType("confirmation_scraper")
        stub.extract_dates = stub.validate = stub.parse_month_day = lambda *a, **k: None
        stub.MONTHS = {}
        cls.mod = _load("fcv2_brave_scraper", "brave_scraper.py", {"confirmation_scraper": stub})

    def setUp(self):
        patcher = mock.patch.object(self.mod, "FIRECRAWL_API_KEY", KEY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scrape(self, url):
        return self.mod.firecrawl_scrape(url)

    def test_no_key_makes_no_call(self):
        with mock.patch.object(self.mod, "FIRECRAWL_API_KEY", ""):
            out, seen = call(self.scrape, "https://d.example/a", reply=v2_ok("x"))
        self.assertEqual((out, seen), ("", []))


class QaSweepScrape(_ScrapeCase):
    page_timeout_ms, client_timeout = 12000, 15

    @classmethod
    def setUpClass(cls):
        cls.mod = _load("fcv2_qa_sweep", "qa_sweep.py")

    def setUp(self):
        self.logged = []
        for name, value in (("FIRECRAWL_API_KEY", KEY), ("log", self.logged.append)):
            patcher = mock.patch.object(self.mod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def scrape(self, url):
        return self.mod.firecrawl_scrape(url)

    def test_timeouts_follow_the_config(self):
        self.assertEqual((self.mod.FIRECRAWL_TIMEOUT * 1000, self.mod.FIRECRAWL_TIMEOUT + 3), (12000, 15))

    def test_markdown_is_capped_and_errors_are_logged(self):
        out, _ = call(self.mod.firecrawl_scrape, "https://d.example/a", 10, reply=v2_ok("abcdefghijklmnop"))
        self.assertEqual(out, "abcdefghij")
        call(self.scrape, "https://d.example/a", reply=http_error(429, {"success": False}))
        self.assertTrue(any("Firecrawl error" in m and "429" in m for m in self.logged), self.logged)

    def test_no_key_makes_no_call(self):
        with mock.patch.object(self.mod, "FIRECRAWL_API_KEY", ""):
            out, seen = call(self.scrape, "https://d.example/a", reply=v2_ok("x"))
        self.assertEqual((out, seen), ("", []))


del _ScrapeCase  # base class only; keep it out of collection


# ---- repo-wide guard ---------------------------------------------------------------------------------------
# Scope: every git-tracked code or config file (by suffix, or an extensionless file with a shebang), minus
# directories named archive/archives (historical copies kept unchanged). Nothing is hand-picked. The standalone
# v1 extract, deep-research and llmstxt endpoints are outside the v2 migration (Firecrawl's guide), so allowed.
# The patterns are assembled from pieces so this file never matches itself.
HOST = re.escape("api.firecrawl" + ".dev")
V1_REST = re.compile(HOST + r"/v" + "1" + r"(?!/(?:extract|deep-research|llmstxt)\b)(?![\w.-])")
APP = "Firecrawl" + "App"
V1_SDK = re.compile("|".join([
    r"\b(?:Async)?V1" + APP + r"\b", r"\b" + APP + r"V1\b", r"\bfirecrawl\.v" + "1" + r"\b",
    r"\bfrom\s+firecrawl\s+import\s[^\n]*\b" + APP + r"\b", r"\bnew\s+" + APP + r"\b",
    r"mendableai/" + "firecrawl-go", r"firecrawl-mcp@" + r"1\.",
]))
CODE_SUFFIXES = {".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb",
                 ".yml", ".yaml", ".toml", ".ini", ".cfg", ".json", ".plist", ".service", ".timer", ".conf"}
HISTORICAL_DIRS = {"archive", "archives"}
MAX_BYTES = 5_000_000


def is_runtime_code(rel, root=ROOT):
    p = Path(rel)
    if HISTORICAL_DIRS & {part.lower() for part in p.parts[:-1]}:
        return False
    if p.suffix.lower() in CODE_SUFFIXES:
        return True
    if p.suffix == "":
        try:
            with open(root / p, "rb") as f:
                return f.read(2) == b"#!"
        except OSError:
            return False
    return False


def violations(root, files):
    found = []
    for rel in files:
        path = root / rel
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            if V1_REST.search(line) or V1_SDK.search(line):
                found.append(f"{rel}:{n}: {line.strip()[:160]}")
    return found


class NoFirecrawlV1InRuntimeCode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError) as e:
            raise unittest.SkipTest(f"git ls-files unavailable: {e}")
        cls.files = [f for f in out.decode("utf-8", "surrogateescape").split("\0") if f and is_runtime_code(f)]

    def test_scope_covers_the_firecrawl_callers(self):
        self.assertGreater(len(self.files), 100)
        for f in ("data/school_schedules/pipeline_v3.py", "data/school_schedules/brave_scraper.py",
                  "data/school_schedules/qa_sweep.py"):
            self.assertIn(f, self.files)

    def test_no_runtime_code_calls_firecrawl_v1(self):
        self.assertEqual(violations(ROOT, self.files), [])

    def test_guard_catches_planted_v1_calls(self):
        base = "https://api.firecrawl" + ".dev/v" + "1"
        planted = {"a.py": f'urllib.request.Request("{base}/scrape")\n', "b.js": f'const BASE = "{base}";\n',
                   "c.py": "from firecrawl import " + APP + "\n", "d.yaml": f"url: {base}/map\n"}
        allowed = {"e.py": f'"{base}/extract"\n"{base}/deep-research"\n"{base}/llmstxt"\n',
                   "f.py": f'"{V2_SCRAPE}"\nfrom firecrawl import Firecrawl\n'}
        with tempfile.TemporaryDirectory() as d:
            for name, text in {**planted, **allowed}.items():
                (Path(d) / name).write_text(text)
            hits = violations(Path(d), sorted({**planted, **allowed}))
        self.assertEqual(sorted({h.split(":", 1)[0] for h in hits}), sorted(planted))

    def test_historical_dirs_are_out_of_scope_but_live_dirs_are_in(self):
        self.assertFalse(is_runtime_code("data/school_schedules/archive/scripts/old.py"))
        self.assertFalse(is_runtime_code("archives/2026-04-05/x.json"))
        self.assertTrue(is_runtime_code("data/school_schedules/new_scraper.py"))
        self.assertTrue(is_runtime_code("launchd/com.example.job.plist"))


if __name__ == "__main__":
    unittest.main()
