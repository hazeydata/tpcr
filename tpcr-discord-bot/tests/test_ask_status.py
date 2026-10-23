"""/ask rotating status: park detection, rotation/mixing, stop, 429 backoff,
bad-file fallback, cadence. No discord, no network."""
import ast
import asyncio
import json
import logging
import os
import random
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ask_status as st  # noqa: E402

BOT = Path(__file__).resolve().parent.parent / "bot.py"


def _park_names():
    tree = ast.parse(BOT.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "PARK_NAMES":
            return ast.literal_eval(node.value)
    raise AssertionError("PARK_NAMES not found")


PARKS = _park_names()


class HTTPErr(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"http {status}")
        self.status = status
        if retry_after is not None:
            self.retry_after = retry_after


class FakeMsg:
    def __init__(self, fail=()):
        self.edits = []
        self.fail = list(fail)

    async def edit(self, **kw):
        if self.fail:
            e = self.fail.pop(0)
            if e is not None:
                raise e
        self.edits.append(kw)


class FakeClock:
    """Records sleeps; yields to the loop so cancel works."""

    def __init__(self):
        self.sleeps = []

    async def sleep(self, s):
        self.sleeps.append(s)
        await asyncio.sleep(0)


async def _drive(rot, n_sleeps, clock):
    rot.start()
    for _ in range(10000):
        if len(clock.sleeps) >= n_sleeps:
            break
        await asyncio.sleep(0)
    await rot.stop()


def _no_leaks():
    cur = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not cur and not t.done()]


# ── data file ──────────────────────────────────────────────────────
def test_shipped_file_has_generic_and_every_park():
    data = st.load_status_lines()
    assert len(data["generic"]) >= 10
    codes = {v[0] for v in PARKS.values()}
    for c in codes:
        assert 3 <= len(data["parks"].get(c, [])) <= 5, c
    assert "Crunching the numbers…" in data["generic"]
    assert any("Soarin" in s for s in data["parks"]["EP"])


@pytest.mark.parametrize("content", [None, "{not json", "[]", '{"generic": []}', '{"generic": ["a"], "parks": []}'])
def test_bad_or_missing_file_falls_back_with_warning(tmp_path, caplog, content):
    p = tmp_path / "lines.json"
    if content is not None:
        p.write_text(content)
    with caplog.at_level(logging.WARNING, logger="ask_status"):
        data = st.load_status_lines(p)
    assert data == {"generic": list(st.FALLBACK_GENERIC), "parks": {}}
    assert any("built-in generic" in r.message for r in caplog.records)


def test_blank_lines_dropped(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"generic": ["a", " ", 3], "parks": {"ep": ["x", ""], "MK": []}}))
    assert st.load_status_lines(p) == {"generic": ["a"], "parks": {"EP": ["x"]}}


# ── park detection ─────────────────────────────────────────────────
@pytest.mark.parametrize("q,code", [
    ("Best day for Epcot next week?", "EP"),
    ("is MK busy?", "MK"),
    ("Hollywood Studios vs Animal Kingdom", "HS"),
    ("crowds at Universal Studios Hollywood", "UH"),
    ("Universal Studios on Friday", "UF"),
    ("tokyo disneysea in May", "TDS"),
    ("dca or disneyland?", "CA"),
    ("IOA wait times", "IA"),
])
def test_detect_park(q, code):
    assert st.detect_park(q, PARKS) == code


@pytest.mark.parametrize("q", ["What should I pack?", "can I bring a stroller", "how deep is the queue", ""])
def test_detect_no_park_and_no_substring_false_hits(q):
    assert st.detect_park(q, PARKS) is None


# ── rotation order and mixing ──────────────────────────────────────
def test_generic_only_without_park():
    data = {"generic": ["g1", "g2", "g3"], "parks": {"EP": ["p1", "p2"]}}
    lines = st.lines_for_question("what to pack", PARKS, data, random.Random(1))
    assert sorted(lines) == ["g1", "g2", "g3"]


def test_park_lines_alternate_with_generic():
    data = {"generic": ["g1", "g2", "g3"], "parks": {"EP": ["p1", "p2"]}}
    lines = st.lines_for_question("epcot tomorrow?", PARKS, data, random.Random(1))
    assert sorted(lines) == ["g1", "g2", "g3", "p1", "p2"]
    kinds = [s[0] for s in lines]
    assert kinds == ["p", "g", "p", "g", "g"]


def test_rotator_cycles_lines_in_order_with_spinner():
    async def go():
        msg, clock = FakeMsg(), FakeClock()
        rot = st.StatusRotator(msg, ["a", "b", "c"], sleep=clock.sleep, rng=random.Random(0))
        await _drive(rot, 6, clock)
        assert _no_leaks() == []
        return msg
    msg = asyncio.run(go())
    contents = [e["content"] for e in msg.edits]
    assert contents[:5] == [st.render(1, "b"), st.render(2, "c"), st.render(3, "a"),
                            st.render(4, "b"), st.render(5, "c")]
    glyphs = [c.split(" ", 1)[0] for c in contents[:4]]
    assert len(set(glyphs)) == 4 and all(g in st.SPINNER for g in glyphs)


# ── cadence ────────────────────────────────────────────────────────
def test_cadence_is_jittered_3_to_5_seconds():
    async def go():
        clock = FakeClock()
        rot = st.StatusRotator(FakeMsg(), ["a", "b"], sleep=clock.sleep, rng=random.Random(42))
        await _drive(rot, 50, clock)
        return clock.sleeps
    sleeps = asyncio.run(go())
    assert all(st.MIN_INTERVAL <= s <= st.MAX_INTERVAL for s in sleeps)
    assert 3.5 <= sum(sleeps) / len(sleeps) <= 4.5
    assert len(set(sleeps)) > 1


def test_real_clock_cadence_short(monkeypatch):
    monkeypatch.setattr(st, "MIN_INTERVAL", 0.03)
    monkeypatch.setattr(st, "MAX_INTERVAL", 0.05)

    async def go():
        msg = FakeMsg()
        rot = st.StatusRotator(msg, ["a", "b"]).start()
        await asyncio.sleep(0.22)
        await rot.stop()
        n = len(msg.edits)
        await asyncio.sleep(0.12)
        assert len(msg.edits) == n  # nothing after stop
        assert _no_leaks() == []
        return n
    n = asyncio.run(go())
    assert 3 <= n <= 7


# ── 429 / HTTPException backoff ────────────────────────────────────
def test_429_backs_off_by_retry_after_then_resumes():
    async def go():
        msg = FakeMsg(fail=[HTTPErr(429, retry_after=12.5), None, None])
        clock = FakeClock()
        rot = st.StatusRotator(msg, ["a", "b"], sleep=clock.sleep, rng=random.Random(3))
        await _drive(rot, 4, clock)
        return msg, clock, rot
    msg, clock, rot = asyncio.run(go())
    assert rot.backoffs == [12.5]
    assert clock.sleeps[1] == 12.5
    assert len(msg.edits) >= 2


def test_other_http_errors_back_off_exponentially_and_cap():
    async def go():
        msg = FakeMsg(fail=[HTTPErr(500)] * 8 + [HTTPErr(429, retry_after=999)])
        clock = FakeClock()
        rot = st.StatusRotator(msg, ["a"], sleep=clock.sleep, rng=random.Random(3))
        await _drive(rot, 11, clock)
        return rot
    rot = asyncio.run(go())
    assert rot.backoffs[:4] == [10.0, 20.0, 40.0, 60.0]
    assert max(rot.backoffs) == st.MAX_BACKOFF


def test_not_found_stops_quietly():
    async def go():
        msg = FakeMsg(fail=[HTTPErr(404)])
        clock = FakeClock()
        rot = st.StatusRotator(msg, ["a", "b"], sleep=clock.sleep).start()
        for _ in range(50):
            await asyncio.sleep(0)
        assert rot._task.done() and rot._task.exception() is None
        await rot.stop()
        assert _no_leaks() == []
    asyncio.run(go())


def test_stop_is_idempotent_and_safe_before_start():
    async def go():
        rot = st.StatusRotator(FakeMsg(), ["a"])
        await rot.stop()
        rot.start()
        await rot.stop()
        await rot.stop()
        assert _no_leaks() == []
    asyncio.run(go())


# ── bot.py ask_command flow (function lifted from bot.py, deps faked) ──
def _load_ask_command(ns):
    tree = ast.parse(BOT.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "ask_command")
    fn.decorator_list = []
    mod = ast.Module(body=[fn], type_ignores=[])
    exec(compile(mod, str(BOT), "exec"), ns)
    return ns["ask_command"]


class FakeFollowup:
    def __init__(self):
        self.sent = []
        self.msg = FakeMsg()

    async def send(self, *a, **kw):
        self.sent.append((a, kw))
        return self.msg if kw.get("wait") else None


def _ns(agent, created):
    class Rot(st.StatusRotator):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    async def safe_defer(i):
        return True

    class Embed:
        def __init__(self, **kw):
            self.kw = kw

        def set_author(self, **kw):
            pass

        def set_footer(self, **kw):
            pass

    return {
        "safe_defer": safe_defer, "ask_agent": agent, "PARK_NAMES": PARKS,
        "lines_for_question": st.lines_for_question, "render_status": st.render,
        "StatusRotator": Rot, "logger": logging.getLogger("t"), "ANTHROPIC_API_KEY": None,
        "discord": types.SimpleNamespace(Embed=Embed, Interaction=object),
        "AskFeedbackView": lambda *a: "VIEW", "print": lambda *a, **k: None,
    }


def _interaction():
    user = types.SimpleNamespace(id=1, display_avatar=types.SimpleNamespace(url="u"))
    user.__str__ = lambda self: "u"
    return types.SimpleNamespace(user=user, followup=FakeFollowup())


def test_answer_replaces_status_and_stops_rotator():
    created = []

    async def agent(*a, **k):
        await asyncio.sleep(0.01)
        return "the answer"

    async def go():
        cmd = _load_ask_command(_ns(agent, created))
        inter = _interaction()
        await cmd(inter, "Is Epcot busy?")
        assert _no_leaks() == []
        return inter
    inter = asyncio.run(go())
    first = inter.followup.sent[0]
    assert first[1]["wait"] is True and first[0][0].split(" ", 1)[0] in st.SPINNER
    last = inter.followup.msg.edits[-1]
    assert last["content"] is None and last["embed"].kw["description"] == "the answer"
    assert len(inter.followup.sent) == 1  # no extra message
    assert created and created[0]._task is None


@pytest.mark.parametrize("exc", [RuntimeError("boom"), asyncio.TimeoutError()])
def test_error_or_timeout_stops_rotator(exc):
    created = []

    async def agent(*a, **k):
        await asyncio.sleep(0.01)
        raise exc

    async def go():
        cmd = _load_ask_command(_ns(agent, created))
        inter = _interaction()
        await cmd(inter, "what to pack")
        n = len(inter.followup.msg.edits)
        await asyncio.sleep(0.05)
        assert len(inter.followup.msg.edits) == n
        assert _no_leaks() == []
        return inter
    inter = asyncio.run(go())
    assert created[0]._task is None
    assert "Sorry" in inter.followup.msg.edits[-1]["content"]
    assert inter.followup.sent[-1][1].get("ephemeral") is True


def test_status_send_failure_does_not_break_answer():
    async def agent(*a, **k):
        return "ok"

    async def go():
        ns = _ns(agent, [])
        cmd = _load_ask_command(ns)
        inter = _interaction()

        async def bad_send(*a, **kw):
            if kw.get("wait"):
                raise HTTPErr(429, retry_after=1)
            inter.followup.sent.append((a, kw))
        inter.followup.send = bad_send
        await cmd(inter, "hi")
        return inter
    inter = asyncio.run(go())
    assert inter.followup.sent[-1][1]["embed"].kw["description"] == "ok"
