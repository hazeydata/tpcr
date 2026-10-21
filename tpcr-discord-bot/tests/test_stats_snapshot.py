"""Tests for stats_snapshot.py — no network access."""

import csv
import io
import json
import logging
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stats_snapshot  # noqa: E402

FAKE_TOKEN = "FAKE.TOKEN.abc123"
GUILD = "123456789"
NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TPCR_DISCORD_GUILD_ID", raising=False)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, members=10, presence=3, error_status=None):
        self.members = members
        self.presence = presence
        self.error_status = error_status
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error_status is not None:
            raise urllib.error.HTTPError(
                req.full_url, self.error_status, "Unauthorized", {}, io.BytesIO(b"body")
            )
        return FakeResponse(
            {
                "approximate_member_count": self.members,
                "approximate_presence_count": self.presence,
            }
        )


@pytest.fixture()
def env_file(tmp_path):
    p = tmp_path / "fake.env"
    p.write_text(f"DISCORD_BOT_TOKEN={FAKE_TOKEN}\n")
    return p


def run(env_file, csv_path, opener, when):
    return stats_snapshot.main(
        ["--guild-id", GUILD, "--csv", str(csv_path), "--env-file", str(env_file)],
        opener=opener,
        now=when,
    )


def read_rows(csv_path):
    with open(csv_path, newline="") as fh:
        return list(csv.reader(fh))


def test_success_writes_header_and_row(tmp_path, env_file):
    csv_path = tmp_path / "sub" / "stats.csv"
    opener = FakeOpener(members=42, presence=7)
    run(env_file, csv_path, opener, datetime(2026, 9, 15, 12, 0, tzinfo=NY))

    assert read_rows(csv_path) == [
        ["date", "approximate_member_count", "approximate_presence_count"],
        ["2026-09-15", "42", "7"],
    ]
    assert len(opener.requests) == 1
    req = opener.requests[0]
    assert req.full_url == f"https://discord.com/api/v10/guilds/{GUILD}?with_counts=true"
    assert req.get_header("Authorization") == f"Bot {FAKE_TOKEN}"
    assert req.get_header("User-agent")


def test_idempotent_per_date(tmp_path, env_file):
    csv_path = tmp_path / "stats.csv"
    d1 = datetime(2026, 9, 15, 8, 0, tzinfo=NY)
    run(env_file, csv_path, FakeOpener(10, 1), d1)
    run(env_file, csv_path, FakeOpener(20, 2), d1)
    rows = read_rows(csv_path)
    assert rows[0][0] == "date"
    assert rows[1:] == [["2026-09-15", "20", "2"]]

    run(env_file, csv_path, FakeOpener(30, 3), datetime(2026, 9, 16, 8, 0, tzinfo=NY))
    rows = read_rows(csv_path)
    assert [r[0] for r in rows].count("date") == 1
    assert rows[1:] == [["2026-09-15", "20", "2"], ["2026-09-16", "30", "3"]]


def test_today_eastern_converts_timezone():
    utc = datetime(2026, 9, 16, 2, 0, tzinfo=ZoneInfo("UTC"))
    assert stats_snapshot.today_eastern(utc) == "2026-09-15"


def test_token_not_in_output_success(tmp_path, env_file, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    run(env_file, tmp_path / "s.csv", FakeOpener(), datetime(2026, 9, 15, tzinfo=NY))
    out = capsys.readouterr()
    assert FAKE_TOKEN not in out.out + out.err
    assert FAKE_TOKEN not in caplog.text


def test_token_not_in_output_401(tmp_path, env_file, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    csv_path = tmp_path / "s.csv"
    with pytest.raises(SystemExit) as exc:
        run(env_file, csv_path, FakeOpener(error_status=401), datetime(2026, 9, 15, tzinfo=NY))
    assert exc.value.code not in (0, None)
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "401" in combined
    assert FAKE_TOKEN not in combined
    assert FAKE_TOKEN not in caplog.text
    assert not csv_path.exists()


def test_missing_guild_id_exits_2(tmp_path, env_file):
    with pytest.raises(SystemExit) as exc:
        stats_snapshot.main(["--csv", str(tmp_path / "s.csv"), "--env-file", str(env_file)])
    assert exc.value.code == 2


def test_missing_token_exits_1(tmp_path, capsys):
    empty = tmp_path / "empty.env"
    empty.write_text("# nothing\n")
    with pytest.raises(SystemExit) as exc:
        stats_snapshot.load_token(str(empty))
    assert exc.value.code == 1
    assert "DISCORD_BOT_TOKEN" in capsys.readouterr().err


def test_env_fallback_when_file_lacks_token(tmp_path, monkeypatch):
    empty = tmp_path / "empty.env"
    empty.write_text("OTHER=1\n")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "FAKE.ENV.xyz")
    assert stats_snapshot.load_token(str(empty)) == "FAKE.ENV.xyz"


def test_file_takes_precedence_over_environ(tmp_path, env_file, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "FAKE.ENV.xyz")
    assert stats_snapshot.load_token(str(env_file)) == FAKE_TOKEN


@pytest.mark.parametrize(
    "line",
    [
        f"DISCORD_BOT_TOKEN={FAKE_TOKEN}",
        f'DISCORD_BOT_TOKEN="{FAKE_TOKEN}"',
        f"DISCORD_BOT_TOKEN='{FAKE_TOKEN}'",
        f"export DISCORD_BOT_TOKEN={FAKE_TOKEN}",
        f'export DISCORD_BOT_TOKEN="{FAKE_TOKEN}"',
    ],
)
def test_env_parsing_variants(tmp_path, line):
    p = tmp_path / "x.env"
    p.write_text(f"\n# a comment\n# DISCORD_BOT_TOKEN=wrong\n\n{line}\n")
    assert stats_snapshot.load_token(str(p)) == FAKE_TOKEN
