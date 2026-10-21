"""Stub heavy third-party modules that ask_agent imports when they're not installed."""

import sys
import types

for _name in ("httpx", "duckdb"):
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = types.ModuleType(_name)
