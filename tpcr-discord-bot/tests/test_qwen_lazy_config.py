"""Regression: QWEN_* must be read at call time, and bot.py must load ~/.env before importing ask_agent.

On the Mini, bot.py imported ask_agent before load_dotenv(~/.env), so the
request went out with the default model and Qwen returned HTTP 404.
"""

import ast
import asyncio
import importlib
import os
import sys
import types
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))


class _FakeResp:
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class _FakeClient:
    calls = []

    def __init__(self, timeout=None, **kw):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        _FakeClient.calls.append({"url": url, "json": json, "timeout": self.timeout})
        return _FakeResp()


def test_qwen_env_set_after_import_is_used(monkeypatch):
    for var in ("QWEN_MODEL", "QWEN_BASE_URL", "QWEN_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    sys.modules.pop("ask_agent", None)
    aa = importlib.import_module("ask_agent")  # imported with QWEN_* unset
    try:
        # Now simulate load_dotenv(~/.env) running after the import.
        monkeypatch.setenv("QWEN_MODEL", "local-models/Qwen-local-path")
        monkeypatch.setenv("QWEN_BASE_URL", "http://qwen.late:9999/")
        monkeypatch.setenv("QWEN_TIMEOUT", "17")
        _FakeClient.calls = []
        monkeypatch.setattr(aa, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))
        asyncio.run(aa._chat([{"role": "user", "content": "hi"}], use_tools=False))
        call = _FakeClient.calls[0]
        assert call["json"]["model"] == "local-models/Qwen-local-path"
        assert call["url"] == "http://qwen.late:9999/v1/chat/completions"
        assert call["timeout"] == 17.0
    finally:
        sys.modules.pop("ask_agent", None)
        importlib.import_module("ask_agent")


def test_qwen_defaults_unchanged(monkeypatch):
    for var in ("QWEN_MODEL", "QWEN_BASE_URL", "QWEN_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    import ask_agent as aa
    assert aa._qwen_config() == ("http://localhost:8080", "Qwen3.8-27B-8bit", 300.0)


def test_bot_loads_dotenv_before_ask_agent_import():
    tree = ast.parse((BOT_DIR / "bot.py").read_text())
    dotenv_line = import_line = None
    for node in tree.body:  # module-level statements, in order
        if (dotenv_line is None and isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "load_dotenv"):
            dotenv_line = node.lineno
        if import_line is None and (
                (isinstance(node, ast.ImportFrom) and node.module == "ask_agent")
                or (isinstance(node, ast.Import) and any(a.name == "ask_agent" for a in node.names))):
            import_line = node.lineno
    assert import_line is not None, "bot.py no longer imports ask_agent at module level"
    assert dotenv_line is not None, "bot.py has no module-level load_dotenv() call"
    assert dotenv_line < import_line, f"load_dotenv at L{dotenv_line} runs after ask_agent import at L{import_line}"
