"""Rotating status message for /ask.

While ask_agent works, edit one Discord message every ~4s (3-5s jitter) with a
spinner glyph and a playful line. Lines live in status_lines.json (stdlib json,
no new dependency). Park lines are mixed in when the question names a park.

This module does not import discord: HTTP errors are recognised by their
``status`` / ``retry_after`` attributes, so tests run with plain fakes.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import re
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

STATUS_LINES_PATH = Path(__file__).resolve().parent / "status_lines.json"

SPINNER = ("🎢", "🎡", "🎠", "🏰")

FALLBACK_GENERIC: Tuple[str, ...] = (
    "Crunching the numbers…",
    "Digging deep into the wait time files…",
    "Checking the crowd calendar…",
    "Almost there…",
)

MIN_INTERVAL = 3.0
MAX_INTERVAL = 5.0
MAX_BACKOFF = 60.0


def _clean(lines) -> List[str]:
    if not isinstance(lines, list):
        return []
    return [s.strip() for s in lines if isinstance(s, str) and s.strip()]


def load_status_lines(path: Path | str | None = None) -> Dict[str, object]:
    """Return {"generic": [...], "parks": {code: [...]}}; never raises."""
    p = Path(path) if path is not None else STATUS_LINES_PATH
    fallback = {"generic": list(FALLBACK_GENERIC), "parks": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top level is not an object")
        generic = _clean(raw.get("generic"))
        if not generic:
            raise ValueError("no usable 'generic' lines")
        parks_raw = raw.get("parks", {})
        if parks_raw is None:
            parks_raw = {}
        if not isinstance(parks_raw, dict):
            raise ValueError("'parks' is not an object")
        parks = {str(k).upper(): _clean(v) for k, v in parks_raw.items()}
        return {"generic": generic, "parks": {k: v for k, v in parks.items() if v}}
    except Exception as e:  # missing, unreadable, bad JSON, bad shape
        logger.warning("status lines unavailable (%s: %s: %s); using built-in generic lines",
                       p, type(e).__name__, e)
        return fallback


def detect_park(question: str, park_names: Mapping[str, Tuple[str, str]]) -> Optional[str]:
    """Park code for the earliest alias (longest on ties) found as whole word(s) in question."""
    q = (question or "").lower()
    best = None  # (position, -len) -> earliest mention wins, longest alias on ties
    for alias, (code, _name) in park_names.items():
        m = re.search(r"(?<![\w'-])" + re.escape(alias.lower()) + r"(?![\w-])", q)
        if m:
            key = (m.start(), -len(alias))
            if best is None or key < best[0]:
                best = (key, code)
    if best:
        return best[1]
    return None


def build_rotation(generic: List[str], park_lines: Optional[List[str]] = None,
                   rng: Optional[random.Random] = None) -> List[str]:
    """One cycle of lines. Park lines alternate with generic ones (park first)."""
    rng = rng or random.Random()
    g = list(generic) or list(FALLBACK_GENERIC)
    rng.shuffle(g)
    if not park_lines:
        return g
    p = list(park_lines)
    rng.shuffle(p)
    out: List[str] = []
    for i in range(max(len(p), len(g))):
        if i < len(p):
            out.append(p[i])
        if i < len(g):
            out.append(g[i])
    return out


def lines_for_question(question: str, park_names: Mapping[str, Tuple[str, str]],
                       data: Optional[Dict[str, object]] = None,
                       rng: Optional[random.Random] = None) -> List[str]:
    data = data if data is not None else load_status_lines()
    code = detect_park(question, park_names)
    park_lines = data.get("parks", {}).get(code) if code else None
    return build_rotation(data["generic"], park_lines, rng)


def render(frame: int, line: str) -> str:
    return f"{SPINNER[frame % len(SPINNER)]} {line}"


class StatusRotator:
    """Edits ``message`` with rotating lines until stop() is called.

    Usage::
        rot = StatusRotator(msg, lines); rot.start()
        try: answer = await work()
        finally: await rot.stop()
    """

    def __init__(self, message, lines: Iterable[str], *, rng: Optional[random.Random] = None,
                 sleep: Callable = asyncio.sleep, start_frame: int = 1):
        self.message = message
        self.lines = list(lines) or list(FALLBACK_GENERIC)
        self.rng = rng or random.Random()
        self._sleep = sleep
        self._frame = start_frame
        self._task: Optional[asyncio.Task] = None
        self.edits = 0
        self.backoffs: List[float] = []

    def interval(self) -> float:
        return self.rng.uniform(MIN_INTERVAL, MAX_INTERVAL)

    def start(self) -> "StatusRotator":
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ask-status-rotator")
        return self

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # _run swallows errors; belt and braces
            logger.warning("status rotator ended with error", exc_info=True)

    async def _run(self) -> None:
        cycle = itertools.cycle(self.lines)
        next(cycle)  # line 0 was the initial message content
        backoff = 0.0
        while True:
            await self._sleep(max(self.interval(), backoff))
            line = next(cycle)
            try:
                await self.message.edit(content=render(self._frame, line))
                self._frame += 1
                self.edits += 1
                backoff = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status = getattr(e, "status", None)
                if status == 404:  # message gone (NotFound): nothing left to update
                    logger.info("status message gone; stopping rotator")
                    return
                retry_after = getattr(e, "retry_after", None)
                if status == 429 and isinstance(retry_after, (int, float)) and retry_after > 0:
                    backoff = min(float(retry_after), MAX_BACKOFF)
                else:
                    backoff = min(max(backoff * 2, MAX_INTERVAL * 2), MAX_BACKOFF)
                self.backoffs.append(backoff)
                logger.warning("status edit failed (%s %s); backing off %.1fs",
                               type(e).__name__, status, backoff)
