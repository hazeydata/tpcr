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
