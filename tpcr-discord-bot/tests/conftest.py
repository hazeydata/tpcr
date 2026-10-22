"""Shared test setup for the Discord bot suite.

httpx and duckdb are real test dependencies (see requirements-test.txt).
Previously this file replaced them with empty stub modules when they were
missing, which turned a missing dependency into confusing AttributeErrors
deep inside unrelated tests. Fail fast with a clear message instead.
"""

import importlib.util

import pytest

_REQUIRED = ("duckdb", "httpx", "pandas", "pyarrow", "PIL")


def pytest_configure(config):
    missing = [m for m in _REQUIRED if importlib.util.find_spec(m) is None]
    if missing:
        raise pytest.UsageError(
            "tpcr-discord-bot tests need: " + ", ".join(missing)
            + ". Install with: pip install -r tpcr-discord-bot/requirements-test.txt"
        )


@pytest.fixture(autouse=True)
def _ask_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TPCR_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("TPCR_ASK_PROVIDER", "qwen")


@pytest.fixture(autouse=True)
def _alarm_backstop():
    """Backstop only: abort any single test that hangs for 20s (POSIX; needs SIGALRM)."""
    import signal

    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _on_alarm(signum, frame):
        raise TimeoutError("test exceeded 20s backstop timeout")

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(20)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
