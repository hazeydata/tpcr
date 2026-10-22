"""Tests for the daily crowd report: rendering, gate, CLI flags, launchd plists, watchdog.

No network: requests.post is replaced and fails the test if called unexpectedly.
"""

import importlib.util
import json
import plistlib
import shutil
import sys
import types
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

# daily_report needs dotenv/requests importable; stub if absent (as test_daily_report.py does).
for _mod in ("dotenv", "requests"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = types.ModuleType(_mod)
if not hasattr(sys.modules["dotenv"], "load_dotenv"):
    sys.modules["dotenv"].load_dotenv = lambda *a, **kw: None

BOT_DIR = Path(__file__).resolve().parent.parent
REPO = BOT_DIR.parent
sys.path.insert(0, str(BOT_DIR))

import daily_report  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "crowd_report"
TODAY = date(2026, 10, 12)
GOLDEN = (FIXTURES / "expected_2026-10-12.txt").read_text(encoding="utf-8")
LAUNCHD = REPO / "launchd"


# ── Helpers / fixtures ────────────────────────────────────────────────────

def _fail_post(*args, **kwargs):
    raise AssertionError("requests.post must not be called")


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """Data root with WTI parquet, accuracy summary, and a 'done' marker."""
    data = tmp_path / "pipeline"
    (data / "wti").mkdir(parents=True)
    (data / "accuracy").mkdir()
    (data / "logs").mkdir()
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT park_code, CAST(park_date AS DATE) AS park_date, wti "
        f"FROM read_csv_auto('{FIXTURES / 'wti_2026-10-12.csv'}')) "
        f"TO '{data / 'wti' / 'wti.parquet'}' (FORMAT PARQUET)"
    )
    con.close()
    shutil.copy(FIXTURES / "accuracy_summary.json", data / "accuracy" / "accuracy_summary.json")
    shutil.copy(FIXTURES / "pipeline_metrics_2026-10-12.json",
                data / "logs" / "pipeline_metrics_2026-10-12.json")
    monkeypatch.setenv("TPCR_DATA_ROOT", str(data))
    monkeypatch.delenv("TPCR_DUCKDB_PATH", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(daily_report.requests, "post", _fail_post, raising=False)
    return data


class FakeTime:
    """Fake clock + sleep. Sleeping beyond MAX_SLEEPS fails the test instead of hanging."""
    MAX_SLEEPS = 5

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.MAX_SLEEPS:
            raise AssertionError(f"sleep called more than {self.MAX_SLEEPS} times (missing cap?)")
        self.now += seconds

    def monotonic(self):
        return self.now


@pytest.fixture(autouse=True)
def fake_time(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(daily_report, "time",
                        SimpleNamespace(sleep=ft.sleep, monotonic=ft.monotonic))
    return ft


STATE_FILE = "crowd_report_posted_2026-10-12.json"


def _state_file(root):
    return root / "state" / STATE_FILE


def _read_state_file(root):
    return json.loads(_state_file(root).read_text())


def _write_state(root, content):
    (root / "state").mkdir(exist_ok=True)
    _state_file(root).write_text(content if isinstance(content, str) else json.dumps(content))


def _counting_post(monkeypatch, ok=True, status=200):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, timeout))
        return SimpleNamespace(ok=ok, status_code=status, text="boom", json=lambda: {"id": "999"})

    monkeypatch.setattr(daily_report.requests, "post", fake_post, raising=False)
    return calls


def _set_marker(root, status):
    (root / "logs" / "pipeline_metrics_2026-10-12.json").write_text(json.dumps({"status": status}))


def _payload(root):
    daily_report._refresh_paths()
    return daily_report.build_payload(TODAY, daily_report.get_wti, daily_report.load_accuracy())


# ── Rendering ─────────────────────────────────────────────────────────────

class TestRender:
    def test_text_matches_golden(self, root):
        assert daily_report.render_text(_payload(root)).strip() == GOLDEN.strip()

    def test_title_has_unpadded_day(self, root):
        embed = _payload(root)["embeds"][0]
        assert embed["title"] == "🏰 Daily Crowd Report — Monday, October 12, 2026"
        early = daily_report.build_payload(date(2026, 10, 5), lambda *a: 20.0, None)
        assert early["embeds"][0]["title"].endswith("Monday, October 5, 2026")

    def test_markdown_lines_and_section_order(self, root):
        embed = _payload(root)["embeds"][0]
        desc = embed["description"]
        thin = "─" * 35
        assert len(thin) == 35
        sections = desc.split("\n\n")
        assert [s.splitlines()[1] for s in sections] == [
            "🏰 Walt Disney World", "🎠 Disneyland Resort", "🦈 Universal Orlando",
            "🎬 Universal Hollywood", "🗼 Tokyo Disney Resort",
            "⭐ **Best Picks**", "📈 **Forecast Accuracy**",
        ]
        assert all(s.splitlines()[0] == thin for s in sections)
        assert "  ▸ **Magic Kingdom** — WTI **20** (*Typical day*)" in desc
        assert "  ▸ **Animal Kingdom** — WTI **40** (*Well above average*)" in desc
        assert "  ▸ Entity MAE: **8.2 min** (241 days evaluated)" in desc
        assert "  ▸ WTI MAE: **6.0** | Bias: **+0.7** (238 days)" in desc
        assert embed["footer"]["text"] == (
            "💡 /now [park] for live waits • /crowd [park] for forecasts • hazeydata.ai"
        )

    def test_best_picks_exclude_single_park_group(self, root):
        picks = _payload(root)["embeds"][0]["description"].split("\n\n")[5].splitlines()[2:]
        assert picks == [
            "  ▸ **Magic Kingdom** (WTI 20)",
            "  ▸ **Disneyland** (WTI 18)",
            "  ▸ **Universal Studios Florida** (WTI 18)",
            "  ▸ **Tokyo Disneyland** (WTI 9)",
        ]

    def test_bias_none_and_no_accuracy(self):
        acc = {"overall_mae": 8.2, "dates_evaluated": 241, "wti_mae": 6.0,
               "wti_bias": None, "wti_dates_evaluated": 238}
        desc = daily_report.build_payload(TODAY, lambda *a: 20.0, acc)["embeds"][0]["description"]
        assert "Bias: **n/a** (238 days)" in desc
        desc = daily_report.build_payload(TODAY, lambda *a: 20.0, None)["embeds"][0]["description"]
        assert "Forecast Accuracy" not in desc

    def test_missing_park_shows_no_data(self):
        payload = daily_report.build_payload(TODAY, lambda c, d: None if c == "UH" else 20.0, None)
        assert "  ▸ **Universal Studios Hollywood** — No data" in payload["embeds"][0]["description"]


# ── Quality gate ──────────────────────────────────────────────────────────

class TestGate:
    def _gate(self, **kw):
        return daily_report.quality_gate(daily_report.PARK_GROUPS, daily_report.get_wti, TODAY, **kw)

    def test_passes_with_done_marker(self, root):
        daily_report._refresh_paths()
        passed, reason = self._gate()
        assert passed, reason

    def test_fails_when_marker_missing(self, root):
        (root / "logs" / "pipeline_metrics_2026-10-12.json").unlink()
        daily_report._refresh_paths()
        passed, reason = self._gate()
        assert not passed and "marker missing" in reason

    @pytest.mark.parametrize("status", ["failed", "sync_partial"])
    def test_fails_when_status_not_done(self, root, status):
        _set_marker(root, status)
        daily_report._refresh_paths()
        passed, reason = self._gate()
        assert not passed and status in reason

    def test_skip_pipeline_check(self, root):
        _set_marker(root, "failed")
        daily_report._refresh_paths()
        passed, _ = self._gate(skip_pipeline_check=True)
        assert passed

    def test_gate_ignores_pipeline_state_forecast_completed(self, root):
        (root / "logs" / "pipeline_metrics_2026-10-12.json").unlink()
        (root / "state").mkdir()
        (root / "state" / "pipeline_state.json").write_text(
            json.dumps({"forecast_completed": "2026-10-12T06:10:00+00:00"}))
        daily_report._refresh_paths()
        passed, reason = self._gate()
        assert not passed and "marker missing" in reason

    def test_gate_passes_on_done_marker_without_pipeline_state(self, root):
        _set_marker(root, "done")
        assert not (root / "state" / "pipeline_state.json").exists()
        daily_report._refresh_paths()
        passed, reason = self._gate()
        assert passed, reason

    def test_marker_without_status_fails(self, root):
        (root / "logs" / "pipeline_metrics_2026-10-12.json").write_text(
            json.dumps({"forecast_completed": "2026-10-12T06:10:00+00:00"}))
        daily_report._refresh_paths()
        passed, reason = self._gate()
        assert not passed and "None" in reason

    def test_marker_run_date_mismatch_fails(self, root):
        (root / "logs" / "pipeline_metrics_2026-10-12.json").write_text(
            json.dumps({"status": "done", "run_date": "2026-10-11"}))
        daily_report._refresh_paths()
        passed, reason = self._gate()
        assert not passed and "run_date" in reason

    def test_wdw_wti_still_required(self, root):
        daily_report._refresh_paths()
        passed, reason = daily_report.quality_gate(
            daily_report.PARK_GROUPS, lambda c, d: None if c == "AK" else 20.0, TODAY)
        assert not passed and "AK" in reason


# ── CLI: main() ───────────────────────────────────────────────────────────

class TestMain:
    ARGS = ["--date", "2026-10-12"]

    def test_dry_run_prints_post_and_never_posts(self, root, capsys):
        rc = daily_report.main(self.ARGS + ["--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "GATE PASS" in out
        assert GOLDEN.strip() in out
        assert '"embeds"' in out
        assert not (root / "state").exists()

    def test_dry_run_gate_fail_exits_zero_and_says_so(self, root, capsys):
        _set_marker(root, "failed")
        rc = daily_report.main(self.ARGS + ["--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "GATE FAIL" in out

    def test_gate_failure_exits_2_without_posting(self, root, capsys, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
        _set_marker(root, "failed")
        assert daily_report.main(self.ARGS) == 2
        assert "GATE FAIL" in capsys.readouterr().out

    def test_missing_token_exits_1(self, root):
        assert daily_report.main(self.ARGS) == 1

    def test_posted_state_prevents_second_post(self, root, monkeypatch, capsys):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        calls = _counting_post(monkeypatch)
        assert daily_report.main(self.ARGS) == 0
        assert len(calls) == 1 and calls[0][1] == 30
        state = _read_state_file(root)
        assert state["state"] == "posted" and state["message_id"] == "999"
        assert state["run_date"] == "2026-10-12" and "posted_at" in state
        assert "999" in capsys.readouterr().out

        assert daily_report.main(self.ARGS) == 0
        assert "already posted" in capsys.readouterr().out
        assert len(calls) == 1

        assert daily_report.main(self.ARGS + ["--force"]) == 0
        assert "FORCE: overriding existing state posted" in capsys.readouterr().out
        assert len(calls) == 2

    def test_non_2xx_marks_failed_and_retry_posts(self, root, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        _counting_post(monkeypatch, ok=False, status=500)
        assert daily_report.main(self.ARGS) == 1
        assert _read_state_file(root)["state"] == "failed"
        calls = _counting_post(monkeypatch)
        assert daily_report.main(self.ARGS) == 0
        assert len(calls) == 1
        assert _read_state_file(root)["state"] == "posted"

    def test_exception_keeps_pending_and_blocks_retry(self, root, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")

        def boom(*a, **k):
            raise TimeoutError("slow")

        monkeypatch.setattr(daily_report.requests, "post", boom, raising=False)
        assert daily_report.main(self.ARGS) == 1
        assert _read_state_file(root)["state"] == "pending"
        monkeypatch.setattr(daily_report.requests, "post", _fail_post, raising=False)
        assert daily_report.main(self.ARGS) == 5

    def test_missing_token_writes_no_state(self, root):
        assert daily_report.main(self.ARGS) == 1
        assert not (root / "state").exists()

    def test_claim_write_failure_exits_6_without_posting(self, root, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        real = daily_report.write_state_atomic

        def flaky(path, data):
            if data.get("state") == "pending":
                raise OSError("disk full")
            return real(path, data)

        monkeypatch.setattr(daily_report, "write_state_atomic", flaky)
        assert daily_report.main(self.ARGS) == 6  # _fail_post would raise if called

    def test_final_write_failure_exits_7_and_pending_blocks(self, root, monkeypatch, capsys):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        calls = _counting_post(monkeypatch)
        real = daily_report.write_state_atomic

        def flaky(path, data):
            if data.get("state") == "posted":
                raise OSError("disk full")
            return real(path, data)

        monkeypatch.setattr(daily_report, "write_state_atomic", flaky)
        assert daily_report.main(self.ARGS) == 7
        assert len(calls) == 1
        assert _read_state_file(root)["state"] == "pending"
        assert daily_report.main(self.ARGS) == 5
        assert len(calls) == 1
        assert "PENDING" in capsys.readouterr().out

    def test_pending_blocks_and_force_posts(self, root, monkeypatch, capsys):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        calls = _counting_post(monkeypatch)
        _write_state(root, {"run_date": "2026-10-12", "state": "pending"})
        assert daily_report.main(self.ARGS) == 5
        assert calls == []
        assert daily_report.main(self.ARGS + ["--force"]) == 0
        assert len(calls) == 1
        assert "FORCE: overriding existing state pending" in capsys.readouterr().out

    @pytest.mark.parametrize("content", [
        "not json {{{",
        "[1, 2]",
        {"run_date": "2026-10-12"},
        {"run_date": "2026-10-11", "state": "posted"},
    ])
    def test_corrupt_state_exits_4_without_posting(self, root, monkeypatch, capsys, content):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        _write_state(root, content)
        assert daily_report.main(self.ARGS) == 4
        assert "STATE CORRUPT" in capsys.readouterr().out

    def test_wait_gives_up_with_exit_3(self, root, monkeypatch, fake_time, capsys):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        (root / "logs" / "pipeline_metrics_2026-10-12.json").unlink()
        assert daily_report.main(self.ARGS + ["--wait-for-pipeline", "2"]) == 3
        assert len(fake_time.sleeps) <= 3
        assert "never appeared" in capsys.readouterr().out
        assert not (root / "state").exists()

    def test_sync_partial_exits_8_without_waiting_or_posting(self, root, monkeypatch, fake_time):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        _set_marker(root, "sync_partial")
        assert daily_report.main(self.ARGS + ["--wait-for-pipeline", "90"]) == 8
        assert fake_time.sleeps == []
        assert not _state_file(root).exists()

    def test_dry_run_writes_nothing(self, root):
        daily_report._refresh_paths()
        assert daily_report.main(self.ARGS + ["--dry-run"]) == 0
        assert not daily_report.gate_log_path().exists()
        assert not (root / "state").exists()
        # A pending/corrupt state is neither read nor changed by a dry run.
        _write_state(root, "garbage")
        assert daily_report.main(self.ARGS + ["--dry-run"]) == 0
        assert _state_file(root).read_text() == "garbage"

    def test_real_run_writes_gate_log(self, root, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        _counting_post(monkeypatch)
        assert daily_report.main(self.ARGS) == 0
        assert daily_report.gate_log_path().exists()


class TestAtomicWrite:
    def test_uses_os_replace_and_leaves_no_tmp(self, tmp_path, monkeypatch):
        replaced = []
        real_replace = daily_report.os.replace

        def spy(src, dst):
            replaced.append((Path(src).parent, Path(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(daily_report.os, "replace", spy)
        target = tmp_path / "state" / "x.json"
        daily_report.write_state_atomic(target, {"a": 1})
        assert json.loads(target.read_text()) == {"a": 1}
        assert replaced == [(target.parent, target)]
        assert [p.name for p in target.parent.iterdir()] == ["x.json"]

    def test_failure_raises_and_cleans_tmp(self, tmp_path, monkeypatch):
        def boom(src, dst):
            raise OSError("nope")

        monkeypatch.setattr(daily_report.os, "replace", boom)
        target = tmp_path / "x.json"
        with pytest.raises(OSError):
            daily_report.write_state_atomic(target, {"a": 1})
        assert list(tmp_path.iterdir()) == []


class TestWait:
    def test_missing_marker_capped_by_minutes(self, root, fake_time):
        (root / "logs" / "pipeline_metrics_2026-10-12.json").unlink()
        assert daily_report.wait_for_pipeline(2, TODAY) is None
        assert len(fake_time.sleeps) <= 3

    def test_sync_partial_is_terminal(self, root, fake_time):
        _set_marker(root, "sync_partial")
        assert daily_report.wait_for_pipeline(90, TODAY) == "sync_partial"
        assert fake_time.sleeps == []

    def test_returns_done_once_marker_turns_done(self, root):
        _set_marker(root, "running")
        now = [0.0]

        def fake_sleep(s):
            now[0] += s
            _set_marker(root, "done")

        status = daily_report.wait_for_pipeline(
            10, TODAY, sleep=fake_sleep, clock=lambda: now[0])
        assert status == "done" and now[0] == 60

    def test_stops_immediately_on_failed(self, root):
        _set_marker(root, "failed")
        sleeps = []
        status = daily_report.wait_for_pipeline(
            90, TODAY, sleep=sleeps.append, clock=lambda: 0.0)
        assert status == "failed" and sleeps == []

    def test_zero_minutes_checks_once(self, root):
        (root / "logs" / "pipeline_metrics_2026-10-12.json").unlink()
        sleeps = []
        assert daily_report.wait_for_pipeline(0, TODAY, sleep=sleeps.append) is None
        assert sleeps == []


class TestPaths:
    def test_data_root_resolved_at_call_time(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TPCR_DATA_ROOT", str(tmp_path / "a"))
        assert daily_report.data_root() == tmp_path / "a"
        monkeypatch.setenv("TPCR_DATA_ROOT", str(tmp_path / "b"))
        assert daily_report.gate_log_path() == tmp_path / "b" / "logs" / "daily_report_gate.log"

    def test_default_root_and_duckdb_override(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TPCR_DATA_ROOT", raising=False)
        monkeypatch.delenv("TPCR_DUCKDB_PATH", raising=False)
        assert daily_report.data_root() == Path.home() / "hazeydata" / "pipeline"
        monkeypatch.setenv("TPCR_DUCKDB_PATH", str(tmp_path / "x.duckdb"))
        assert daily_report._duckdb_path() == str(tmp_path / "x.duckdb")


# ── launchd plists ────────────────────────────────────────────────────────

PLISTS = {
    "com.hazeydata.tpcr-crowd-report": {
        "script": "__HOME__/theme-park-crowd-report/tpcr-discord-bot/daily_report.py",
        "args": ["--wait-for-pipeline", "90"],
        "hour": 7,
        "log": "tpcr-crowd-report",
    },
    "com.hazeydata.tpcr-crowd-report-watchdog": {
        "script": "__HOME__/theme-park-crowd-report/scripts/tpcr_crowd_report_watchdog.py",
        "args": ["--check-only", "--no-post"],
        "hour": 9,
        "log": "tpcr-crowd-report-watchdog",
    },
}


@pytest.mark.parametrize("label", list(PLISTS))
class TestPlists:
    def _load(self, label):
        raw = (LAUNCHD / f"{label}.plist").read_bytes()
        return raw.decode(), plistlib.loads(raw)

    def test_contents(self, label):
        spec = PLISTS[label]
        _, pl = self._load(label)
        assert pl["Label"] == label
        assert pl["ProgramArguments"] == [
            "__HOME__/theme-park-crowd-report/.venv/bin/python", spec["script"], *spec["args"]]
        assert pl["StartCalendarInterval"] == {"Hour": spec["hour"], "Minute": 0}
        assert pl["RunAtLoad"] is False
        assert pl["StandardOutPath"] == f"__HOME__/hazeydata/pipeline/logs/{spec['log']}.log"
        assert pl["StandardErrorPath"] == f"__HOME__/hazeydata/pipeline/logs/{spec['log']}.err.log"
        assert pl["EnvironmentVariables"] == {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1",
            "TPCR_DATA_ROOT": "__HOME__/hazeydata/pipeline"}
        root = pl["EnvironmentVariables"]["TPCR_DATA_ROOT"]
        assert pl["StandardOutPath"].startswith(root + "/")
        assert pl["StandardErrorPath"].startswith(root + "/")

    def test_no_absolute_user_paths_or_secrets(self, label):
        text, pl = self._load(label)
        for bad in ("/" + "Users", "/" + "home/wilma", "/" + "workspace"):
            assert bad not in text
        for key in pl["EnvironmentVariables"]:
            assert not any(w in key.upper() for w in ("TOKEN", "SECRET", "KEY", "PASSWORD"))
        assert "DISCORD_BOT_TOKEN" not in text

    def test_referenced_scripts_exist(self, label):
        script = PLISTS[label]["script"].replace("__HOME__/theme-park-crowd-report/", "")
        assert (REPO / script).is_file()


# ── watchdog ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def watchdog():
    spec = importlib.util.spec_from_file_location(
        "tpcr_crowd_report_watchdog", REPO / "scripts" / "tpcr_crowd_report_watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWatchdog:
    @pytest.mark.parametrize("argv,expect_post", [
        ([], False),
        (["--check-only"], False),
        (["--check-only", "--no-post"], False),
        (["--post", "--no-post"], False),
        (["--post"], True),
        (["--check-only", "--post"], True),
    ])
    def test_posts_only_with_post_flag(self, watchdog, root, monkeypatch, argv, expect_post):
        posted = []
        monkeypatch.setattr(watchdog, "_write_result", lambda text: None)
        monkeypatch.setattr(watchdog, "discord_me", lambda: {"ok": True, "username": "bot"})
        monkeypatch.setattr(watchdog, "get_wdw_wti_today",
                            lambda today=None: {"MK": 20.0, "EP": 24.0, "HS": 32.0, "AK": 40.0})
        monkeypatch.setattr(watchdog, "fetch_recent_messages", lambda *a, **k: [])
        monkeypatch.setattr(watchdog, "post_embed",
                            lambda *a, **k: posted.append(a) or {"ok": True, "id": "1"})
        # No heal may shell out: the empty channel makes validation fail.
        monkeypatch.setattr(watchdog, "diagnose", lambda *a, **k: {"primary": "x", "classes": []})
        monkeypatch.setattr(watchdog, "heal", lambda *a, **k: [])
        monkeypatch.setattr(watchdog, "recheck",
                            lambda *a, **k: {"ok": False, "reasons": ["stub"]})
        watchdog.main(argv + ["--quiet"])
        assert bool(posted) is expect_post

    def test_repo_is_relative(self, watchdog):
        assert watchdog.REPO == REPO

    def test_display_tokens_match_new_title(self, watchdog):
        title = daily_report.build_payload(TODAY, lambda *a: 20.0, None)["embeds"][0]["title"]
        tokens = [t.lower() for t in watchdog.today_display_tokens(TODAY)]
        assert "monday, october 12, 2026" in tokens
        assert any(t in title.lower() for t in tokens)
        early = date(2026, 10, 5)
        title = daily_report.build_payload(early, lambda *a: 20.0, None)["embeds"][0]["title"]
        assert any(t.lower() in title.lower() for t in watchdog.today_display_tokens(early))

    def test_finds_report_with_new_title(self, watchdog):
        embed = daily_report.build_payload(TODAY, lambda *a: 20.0, None)["embeds"][0]
        found = watchdog.find_today_crowd_report([{"id": "1", "embeds": [embed]}], TODAY)
        assert found and found["id"] == "1"

    def test_marker_status(self, watchdog, root):
        status, age, err = watchdog.pipeline_marker_status(TODAY)
        assert status == "done" and err is None and age >= 0
        (root / "logs" / "pipeline_metrics_2026-10-12.json").unlink()
        status, age, err = watchdog.pipeline_marker_status(TODAY)
        assert status is None and err

    def test_wdw_wti_falls_back_to_simple_query(self, watchdog, root):
        # Fixture DB has no time_slot/source columns, so the first query errors.
        db = root / "tpcr_live.duckdb"
        con = duckdb.connect(str(db))
        con.execute(
            f"CREATE TABLE wti AS SELECT park_code, CAST(park_date AS DATE) AS park_date, wti "
            f"FROM read_csv_auto('{FIXTURES / 'wti_2026-10-12.csv'}')")
        con.close()
        scores = watchdog.get_wdw_wti_today(TODAY)
        assert scores == {"MK": 20.0, "EP": 24.0, "HS": 32.0, "AK": 40.0}
