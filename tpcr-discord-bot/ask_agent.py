"""
Ask Agent — AI-powered natural language query engine for TPCR Discord bot.

Uses a local OpenAI-compatible Qwen server (mlx_lm) with DuckDB tool access to answer
theme park wait time questions from Discord users.
"""

import asyncio
import json
import logging
import os
import re
import duckdb
import httpx
from datetime import datetime, date
from pathlib import Path
from typing import Optional

# Host paths. The defaults are the original (Linux) host values; other hosts
# override them with env vars, normally set in ~/.env, which bot.py loads with
# python-dotenv. bot.py imports this module *before* it calls load_dotenv, so the
# env vars are read at call time (see _duckdb_path/_usage_file/_question_log),
# never at import time.
DUCKDB_PATH = "/mnt/data/pipeline/tpcr_live.duckdb"
DUCKDB_PATH_ENV = "TPCR_DUCKDB_PATH"
USAGE_FILE_ENV = "TPCR_ASK_USAGE_FILE"
QUESTION_LOG_ENV = "TPCR_ASK_QUESTION_LOG"

# Failure patterns that trigger immediate self-healing
_FAILURE_PATTERNS = [
    "data is updating right now",
    "try again in a minute",
    "database is temporarily busy",
    "i wasn't able to fully answer",
    "try rephrasing or asking something more specific",
    "query error:",
    "unable to access",
    "no data available",
]


def _is_bad_response(answer: str) -> bool:
    """Check if an answer matches a known failure pattern."""
    answer_lower = answer.lower()
    return any(p in answer_lower for p in _FAILURE_PATTERNS)


_SELF_HEAL_LOCK = None  # Timestamp of last self-heal trigger


def _trigger_self_heal(question: str, user_id: str, username: str, answer: str):
    """Fire the ask_response_monitor in the background to diagnose and fix.
    
    Rate-limited: only fires once per 60 seconds to prevent cascading retries.
    """
    global _SELF_HEAL_LOCK
    import subprocess
    import os
    import time as _t

    # Rate limit: don't fire more than once per 60 seconds
    now = _t.time()
    if _SELF_HEAL_LOCK and (now - _SELF_HEAL_LOCK) < 60:
        return  # Already triggered recently, skip
    _SELF_HEAL_LOCK = now

    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "ask_response_monitor.py"
    )
    venv_python = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".venv", "bin", "python"
    )
    try:
        # Pass current environment so subprocess has API keys, bot tokens, etc.
        env = os.environ.copy()
        subprocess.Popen(
            [venv_python, script, "--fix", "--since-minutes", "5", "--json"],
            stdout=open("/tmp/ask_self_heal.log", "a"),
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            start_new_session=True,  # Detach from bot process
        )
    except Exception:
        pass  # Never let self-heal crash the bot

# Schema context for the AI agent
SCHEMA_CONTEXT = """You are a theme park data analyst for the Theme Park Crowd Report (TPCR).
You answer questions about theme park crowds, wait times, and visit planning using real forecast data.

## Database: DuckDB at /mnt/data/pipeline/tpcr_live.duckdb (READ-ONLY)

### Tables

**wti** — Wait Time Index (park-level daily crowd metric, scale ~1-60+)
- park_code: VARCHAR (AK, CA, DL, EP, EU, HS, IA, MK, TDL, TDS, UF, UH)
- park_date: DATE
- time_slot: VARCHAR ('daily' for full-day WTI)
- wti: DOUBLE (lower = less crowded)
- source: VARCHAR ('forecast' or 'actual')

**forecasts** — Ride-level wait time predictions (5-min intervals)
- entity_code: VARCHAR (e.g., 'MK01', 'EP05')
- park_date: DATE
- time_slot: VARCHAR (HH:MM:SS format, e.g., '14:00:00')
- predicted_actual: DOUBLE (predicted actual wait in minutes)
- predicted_posted: DOUBLE (predicted posted wait in minutes)
- prediction_method: VARCHAR

**entities** — Ride/attraction metadata
- entity_code: VARCHAR
- entity_name: VARCHAR
- short_name: VARCHAR
- park_code: VARCHAR
- category: VARCHAR
- has_wait_times: BOOLEAN
- wait_time_type: VARCHAR ('STANDBY', etc.)
- is_extinct: BOOLEAN

**live_waits** — Real-time wait observations (last ~7 days)
- entity_code: VARCHAR
- observed_at: TIMESTAMPTZ
- wait_time_type: VARCHAR
- wait_time_minutes: INTEGER
- park_date: DATE

### Parquet Fallback (use when DuckDB tables return no data)

CRITICAL: If a query against a DuckDB table returns no results, ALWAYS retry using the
corresponding parquet file with read_parquet(). The parquet files often contain a fuller
date range (365+ days of forecasts) than the DuckDB tables.

- WTI: `SELECT * FROM read_parquet('/mnt/data/pipeline/wti/wti.parquet') WHERE park_code = 'EP' AND park_date = '2026-10-01'`
- Forecasts: `SELECT * FROM read_parquet('/mnt/data/pipeline/curves/forecast_parquet/all_forecasts.parquet') WHERE entity_code LIKE 'EP%' AND park_date = '2026-10-01'`
- Fact tables (historical waits): `SELECT * FROM read_parquet('/mnt/data/pipeline/fact_tables/parquet/*.parquet') WHERE entity_code = 'MK01'`

Always try the DuckDB table first (faster), then fall back to parquet if no results.

## Park Code Reference
- MK = Magic Kingdom (Walt Disney World)
- EP = EPCOT (Walt Disney World)
- HS = Hollywood Studios (Walt Disney World)
- AK = Animal Kingdom (Walt Disney World)
- DL = Disneyland (California)
- CA = Disney California Adventure
- UF = Universal Studios Florida (Universal Orlando Resort)
- IA = Islands of Adventure (Universal Orlando Resort)
- EU = Epic Universe (Universal Orlando Resort — opened 2025)
- UH = Universal Studios Hollywood (separate resort in California)

NOTE: "Universal Orlando" or "Universal Orlando Resort" means ALL THREE parks: UF + IA + EU. Always include EU when asked about Universal Orlando.
- TDL = Tokyo Disneyland
- TDS = Tokyo DisneySea

## WTI Interpretation
- ≤12: Well below average — best day to visit
- 13-18: Below average — great day to visit
- 19-25: Typical day — moderate waits
- 26-34: Above average — plan strategically
- 35-42: Well above average — arrive early
- 43-50: Very high — consider another day
- 50+: Extreme — plan carefully or reschedule

## Guidelines
- Today's date is {today} ({today_dow}).
- CRITICAL: When displaying dates, ALWAYS use dayname() or strftime(park_date, '%A') in your SQL to get the correct day-of-week name from the database. NEVER guess day names — LLMs frequently get them wrong for future dates.
- Use the `run_query` tool to query the database. Always use read-only queries (SELECT only).
- When comparing dates, show WTI values and explain what they mean.
- For "best day to visit" questions, query WTI and sort by lowest.
- For ride-specific questions, join forecasts with entities.
- Keep answers concise and helpful — these are Discord messages (max ~1800 chars).
- Use emoji sparingly for readability.
- If you don't have data for something, say so honestly.
- Don't fabricate data. Only report what the database returns.
- CRITICAL — EXTINCT RIDES: Some rides in the database are permanently closed. If a user asks about one, tell them
  it's permanently closed and suggest the replacement (if any). Key closures:
  • Splash Mountain (MK) — closed Jan 2023, replaced by Tiana's Bayou Adventure (opened Jun 2024)
  • Splash Mountain (DL) — closed May 2023, replaced by Tiana's Bayou Adventure (opened 2024)
  • Stitch's Great Escape (MK) — closed 2020
  • Universe of Energy / Ellen's Energy Adventure (EP) — closed 2017, replaced by Guardians: Cosmic Rewind
  • The Great Movie Ride (HS) — closed 2017, replaced by Runaway Railway
  • Poseidon's Fury (IA) — closed 2023
  • Shrek 4-D (UF) — closed 2022
  • Dragon Challenge (IA) — closed 2017, replaced by Hagrid's Adventure
  Historical data still exists for these rides but should NOT be used for future visit planning.
- CRITICAL — RIDE NAMES: ONLY mention rides from the reference list below. NEVER guess ride names from general knowledge.
  Disney and Universal are DIFFERENT companies with completely different rides.
  If you're unsure whether a ride is at a specific park, query the entities table or just don't mention it by name.

## Ride Reference (ONLY use these names when recommending rides)
- MK (Magic Kingdom): 7 Dwarfs Train, Space Mountain, TRON, Big Thunder Mtn, Haunted Mansion, Pirates of Caribbean, Jungle Cruise, Peter Pan's Flight, Buzz Lightyear, it's a small world, Tiana's Adventure, Under the Sea, Dumbo, PeopleMover, Mad Tea Party, Splash Mountain
- EP (EPCOT): Guardians: Cosmic Rewind, Test Track, Frozen Ever After, Remy's Adventure, Soarin', Spaceship Earth, Living w/ Land, Mission: SPACE, Journey of Water
- HS (Hollywood Studios): Rise of Resistance, Millennium Falcon, Slinky Dog, Rock Coaster, Tower of Terror, Runaway Railway, Toy Story Mania!, Star Tours
- AK (Animal Kingdom): Flight of Passage, Na'vi River, Expedition Everest, Kilimanjaro Safaris, Kali River Rapids, Zootopia
- DL (Disneyland): Matterhorn, Indiana Jones Adv, Space Mountain, Big Thunder Mtn, Rise of Resistance, Millennium Falcon, Haunted Mansion, Pirates of Caribbean, Runaway Railway, Tiana's Adventure
- CA (California Adventure): Radiator Racers, Incredicoaster, Guardians BREAKOUT, WEB SLINGERS, Soarin', Grizzly River Run, Toy Story Mania!, Goofy's Sky School
- UF (Universal Studios Florida): Gringotts, Mummy, MEN IN BLACK, Transformers, E.T. Adventure, Simpsons Ride, Despicable Me, Minion Blast, Race Through NY, Fast & Furious, Hogwarts Exp-KGX
- IA (Islands of Adventure): Hagrid's Adventure, VelociCoaster, Incredible Hulk, Forbidden Journey, Spider-Man, JP River Adventure, Ripsaw Falls, Reign of Kong, Doom's Fearfall, Bilge-Rat Barges
- EU (Epic Universe): Battle at the Ministry, Mine-Cart Madness, Mario Kart, Hiccup's Wing Gliders, Stardust Racers, Monsters Unchained, Curse of Werewolf, Yoshi's Adventure, Constellation Carousel, Le Cirque Arcanus, Untrainable Dragon
- UH (Universal Hollywood): Mario Kart, Jurassic World, Forbidden Journey, Studio Tour, Mummy, Kung Fu Panda, Transformers, Secret Life of Pets, Simpsons, Despicable Me
- TDL (Tokyo Disneyland): Beauty and the Beast, Pooh's Hunny Hunt, Splash Mountain, Big Thunder Mountain, Haunted Mansion, Monsters Inc., Peter Pan's Flight, Buzz Lightyear's Astro Blasters, Star Tours, Space Mountain
- TDS (Tokyo DisneySea): Journey to the Center of the Earth, Soaring, Tower of Terror, Toy Story Mania!, Indiana Jones Adventure, Raging Spirits, Frozen Journey, Peter Pan's Adventure, 20000 Leagues Under the Sea
- NEVER show raw SQL to users. Just show the results naturally.
- Round wait times to whole numbers.
- This is a one-shot interaction — don't ask follow-up questions like "Would you like me to check X?"
  Instead, proactively include that info (e.g., if they ask about a busy day, also show nearby less-busy alternatives).
- If a query returns an error about locks or connectivity, just say "Data is updating right now — try again in a minute!" Don't expose database internals or error messages to users.
- Never mention databases, SQL, DuckDB, queries, or technical infrastructure in your response.
- If asked about data sources, say: "Our data comes from TouringPlans.com (historical wait times), Queue-Times.com (live waits), and official park sources (hours & events)."
- You CANNOT create visuals, charts, graphs, or images. Don't mention that you can't — just provide the data in text form naturally.
- If a user asks you to build a full touring plan / itinerary (ride order, time-by-time schedule, optimized plan),
  give general advice (best times, what to prioritize) but recommend TouringPlans.com for detailed custom plans:
  "For a full optimized touring plan, check out [TouringPlans.com](https://touringplans.com) — they're the best in the business for that!"
  You can still answer specific questions like "what's the best time to ride X" or "which rides have the shortest waits."
"""

# Usage tracking (simple JSON file)
USAGE_FILE = Path("/home/wilma/theme-park-crowd-report/tpcr-discord-bot/ask_usage.json")
QUESTION_LOG = Path("/home/wilma/theme-park-crowd-report/tpcr-discord-bot/ask_questions.jsonl")


def _env_path(var: str, default):
    value = os.environ.get(var, "").strip()
    return os.path.expanduser(value) if value else default


def _duckdb_path() -> str:
    return str(_env_path(DUCKDB_PATH_ENV, DUCKDB_PATH))


def _usage_file() -> Path:
    return Path(_env_path(USAGE_FILE_ENV, USAGE_FILE))


def _question_log() -> Path:
    return Path(_env_path(QUESTION_LOG_ENV, QUESTION_LOG))


def _load_usage() -> dict:
    path = _usage_file()
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save_usage(data: dict):
    path = _usage_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def track_usage(user_id: str) -> tuple[int, int]:
    """Track usage and return (used_this_month, limit). Returns (-1, -1) if no limit.

    Never raises: a usage-file problem is logged and must not break /ask.
    """
    try:
        return _track_usage(user_id)
    except Exception as e:
        logger.error("ask usage tracking failed (%s=%s): %s: %s",
                     USAGE_FILE_ENV, _usage_file(), type(e).__name__, e)
        return 0, -1


def _track_usage(user_id: str) -> tuple[int, int]:
    data = _load_usage()
    month_key = date.today().strftime("%Y-%m")

    if month_key not in data:
        data[month_key] = {}

    user_key = str(user_id)
    count = data[month_key].get(user_key, 0) + 1
    data[month_key][user_key] = count
    _save_usage(data)

    return count, -1  # No limit for now (free launch)


def log_question(user_id: str, username: str, question: str, answer: str, duration_ms: int):
    """Append question + answer to JSONL log for analysis.

    Never raises: a question-log problem is logged and must not break /ask.
    """
    try:
        _log_question(user_id, username, question, answer, duration_ms)
    except Exception as e:
        logger.error("ask question log failed (%s=%s): %s: %s",
                     QUESTION_LOG_ENV, _question_log(), type(e).__name__, e)


def _log_question(user_id: str, username: str, question: str, answer: str, duration_ms: int):
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "user_id": str(user_id),
        "username": username,
        "question": question,
        "answer": answer[:2000],
        "duration_ms": duration_ms,
    }
    path = _question_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_duckdb_query(sql: str, max_retries: int = 8) -> str:
    """Execute a read-only DuckDB query and return results as string.
    
    Retries aggressively on lock/busy errors since the scraper write
    to tpcr_live.duckdb typically finishes in under 1 second.
    """
    import time

    for attempt in range(max_retries):
        con = None
        try:
            con = duckdb.connect(_duckdb_path(), read_only=True)
            result = con.execute(sql).fetchall()
            columns = [desc[0] for desc in con.description]
            con.close()

            if not result:
                return "No results found."

            # Format as readable text (not full table — keep it compact)
            lines = [" | ".join(columns)]
            lines.append("-" * len(lines[0]))
            for row in result[:50]:  # Cap at 50 rows
                lines.append(" | ".join(str(v) for v in row))

            if len(result) > 50:
                lines.append(f"... ({len(result)} total rows, showing first 50)")

            return "\n".join(lines)
        except Exception as e:
            if con:
                try:
                    con.close()
                except Exception:
                    pass
            error_str = str(e).lower()
            # Retry on any lock, busy, or IO errors (scraper write collisions)
            is_lock_error = any(kw in error_str for kw in ("lock", "busy", "io error", "could not set", "blocked"))
            if is_lock_error and attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))  # Back off: 0.5s, 1s, 1.5s, 2s, ...
                continue
            if attempt < max_retries - 1:
                # Even for non-lock errors, retry once with a brief pause
                time.sleep(1)
                continue
            logger.warning("run_query failed after %d attempts: %s: %s",
                           attempt + 1, type(e).__name__, e)
            return QUERY_GLITCH_MSG

    return QUERY_BUSY_MSG


QUERY_GLITCH_MSG = "I hit a temporary glitch querying the data — please try your question again in a moment. If it keeps happening, let us know in #feedback!"
QUERY_BUSY_MSG = "Our database is momentarily busy — try your question again in a minute! The data refreshes constantly and things should clear up quickly. 🎢"


def tool_result_status(result: str) -> str:
    """Classify a run_query tool result as 'ok' or 'error' for logging."""
    if result in (QUERY_GLITCH_MSG, QUERY_BUSY_MSG) or result.startswith("Error:"):
        return "error"
    return "ok"


logger = logging.getLogger("tpcr-bot")

QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "http://localhost:8080")
QWEN_MODEL = os.getenv("QWEN_MODEL", "Qwen3.8-27B-8bit")
QWEN_TIMEOUT = float(os.getenv("QWEN_TIMEOUT", "300"))

CONTEXT_LIMIT = 32768  # server context window (tokens)
MAX_TOKENS = 1500  # reserved for the completion
_CHARS_PER_TOKEN = 3  # conservative: real text averages ~4
_MSG_OVERHEAD = 8  # per-message role/format tokens
_TOOLS_RESERVE = 200  # tool schema is sent with every request
_TRUNC_NOTE = "\n...[truncated]"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_query",
            "description": "Execute a read-only SQL query against the TPCR DuckDB database. Use SELECT statements only. Returns results as formatted text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL SELECT query to execute"}
                },
                "required": ["sql"],
            },
        },
    }
]


class QwenHTTPError(Exception):
    """Non-2xx response from the Qwen server."""

    def __init__(self, status: int, body: str = ""):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (len/3 chars per token, rounded up)."""
    return -(-len(text or "") // _CHARS_PER_TOKEN)


def _msg_text(msg: dict) -> str:
    text = msg.get("content") or ""
    if msg.get("tool_calls"):
        text += json.dumps(msg["tool_calls"])
    return text


def estimate_messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(_msg_text(m)) + _MSG_OVERHEAD for m in messages)


def _truncate(text: str, max_tokens: int) -> str:
    max_chars = max(0, max_tokens * _CHARS_PER_TOKEN - len(_TRUNC_NOTE))
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNC_NOTE


def trim_messages(
    messages: list[dict],
    max_tokens: int = MAX_TOKENS,
    limit: int = CONTEXT_LIMIT,
    with_tools: bool = True,
) -> list[dict]:
    """Return a copy of messages that fits in limit once max_tokens is reserved.

    messages[0] is the system prompt; the latest user message is always kept.
    Oldest history/tool messages are dropped first, then the biggest remaining
    pieces (tool results, then the system prompt) are truncated.
    """
    budget = limit - max_tokens - (_TOOLS_RESERVE if with_tools else 0)
    msgs = [dict(m) for m in messages]
    if estimate_messages_tokens(msgs) <= budget:
        return msgs

    def total() -> int:
        return estimate_messages_tokens(msgs)

    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=None)
    keep = {i for i in (0 if msgs[0].get("role") == "system" else None, last_user) if i is not None}
    keep_ids = {id(msgs[i]) for i in keep}

    # Drop oldest history/tool messages first (an assistant tool_calls message
    # goes together with its tool results so no orphans are left behind)
    k = 0
    while total() > budget and k < len(msgs):
        if id(msgs[k]) in keep_ids:
            k += 1
            continue
        end = k + 1
        if msgs[k].get("tool_calls"):
            while end < len(msgs) and msgs[end].get("role") == "tool" and id(msgs[end]) not in keep_ids:
                end += 1
        del msgs[k:end]
    while msgs and msgs[0].get("role") == "tool":  # orphaned tool result
        msgs.pop(0)

    # Still too big: truncate tool results, then the user prompt, then the system prompt
    def shrink(m: dict):
        over = total() - budget
        if over > 0:
            content = m.get("content") or ""
            m["content"] = _truncate(content, max(50, estimate_tokens(content) - over))

    for m in msgs:
        if m.get("role") == "tool":
            shrink(m)
    for role in ("user", "system"):
        for m in reversed(msgs):
            if m.get("role") == role:
                shrink(m)
                break
    return msgs


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks (and an unterminated trailing one)."""
    text = _THINK_RE.sub("", text or "")
    text = re.sub(r"<think>.*\Z", "", text, flags=re.DOTALL)
    return text.replace("</think>", "").strip()


async def _post_chat(payload: dict) -> dict:
    """POST to the OpenAI-compatible chat completions endpoint."""
    url = f"{QWEN_BASE_URL.rstrip('/')}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=QWEN_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
    if resp.status_code >= 400:
        raise QwenHTTPError(resp.status_code, resp.text)
    return resp.json()


async def _chat(messages: list[dict], use_tools: bool) -> dict:
    payload = {
        "model": QWEN_MODEL,
        "messages": trim_messages(messages, MAX_TOKENS, CONTEXT_LIMIT, with_tools=use_tools),
        "max_tokens": MAX_TOKENS,
    }
    if use_tools:
        payload["tools"] = TOOLS
    return await _post_chat(payload)


async def ask_agent(question: str, user_id: str, api_key: str | None = None, username: str = "unknown") -> str:
    """
    Process a natural language question about theme park data.
    Uses a local Qwen server (OpenAI-compatible) with async DuckDB tool access.

    api_key is accepted for backwards compatibility and ignored.
    All blocking work (DuckDB queries, file I/O) is run off the event loop so
    the Discord heartbeat is never starved.
    """
    await asyncio.to_thread(track_usage, user_id)

    import time as _time
    _start = _time.monotonic()

    today = date.today()
    system_prompt = SCHEMA_CONTEXT.format(today=today.isoformat(), today_dow=today.strftime("%A"))

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    use_tools = True

    # Allow up to 5 tool-use rounds
    for _ in range(5):
        try:
            data = await _chat(messages, use_tools)
        except QwenHTTPError as e:
            # Server may not support tools: retry once without them
            if use_tools and (400 <= e.status < 500 or "tool" in e.body.lower()):
                logger.warning("Qwen rejected tools request (%s); retrying without tools", e)
                use_tools = False
                data = await _chat(messages, use_tools)
            else:
                raise

        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls") if use_tools else None

        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                fn = call.get("function", {})
                tool_name = fn.get("name") or "?"
                logger.info("ask tool_call name=%s id=%s", tool_name, call.get("id", ""))
                try:
                    args = fn.get("arguments") or "{}"
                    args = json.loads(args) if isinstance(args, str) else args
                    sql = args.get("sql", "")
                except (ValueError, AttributeError):
                    sql = ""

                # Safety: reject non-SELECT queries
                sql_upper = sql.strip().upper()
                if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
                    result = "Error: Only SELECT queries are allowed."
                else:
                    result = await asyncio.to_thread(run_duckdb_query, sql)

                logger.info("ask tool_result name=%s status=%s", tool_name, tool_result_status(result))

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result,
                })
        else:
            # Final text response
            answer = strip_think(message.get("content") or "")

            # Trim if too long for Discord
            if len(answer) > 1900:
                answer = answer[:1900] + "..."

            duration_ms = int((_time.monotonic() - _start) * 1000)
            await asyncio.to_thread(log_question, user_id, username, question, answer, duration_ms)

            if _is_bad_response(answer):
                _trigger_self_heal(question, user_id, username, answer)

            return answer

    fallback = "I wasn't able to fully answer that question. Try rephrasing or asking something more specific!"
    duration_ms = int((_time.monotonic() - _start) * 1000)
    await asyncio.to_thread(log_question, user_id, username, question, fallback, duration_ms)

    _trigger_self_heal(question, user_id, username, fallback)

    return fallback
