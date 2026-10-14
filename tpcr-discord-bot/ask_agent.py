import os
"""
Ask Agent — AI-powered natural language query engine for TPCR Discord bot.

Uses Anthropic Claude with DuckDB tool access to answer
theme park wait time questions from Discord users.
"""

import asyncio
import json
import duckdb
import anthropic
from datetime import datetime, date
from pathlib import Path
from typing import Optional

DUCKDB_PATH = os.path.expanduser("~/hazeydata/pipeline") + "/tpcr_live.duckdb"

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

## Database: DuckDB at /Users/fredhazelton/hazeydata/pipeline/tpcr_live.duckdb (READ-ONLY)

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

- WTI: `SELECT * FROM read_parquet('/Users/fredhazelton/hazeydata/pipeline/wti/wti.parquet') WHERE park_code = 'EP' AND park_date = '2026-10-01'`
- Forecasts: `SELECT * FROM read_parquet('/Users/fredhazelton/hazeydata/pipeline/curves/forecast_parquet/all_forecasts.parquet') WHERE entity_code LIKE 'EP%' AND park_date = '2026-10-01'`
- Fact tables (historical waits): `SELECT * FROM read_parquet('/Users/fredhazelton/hazeydata/pipeline/fact_tables/parquet/*.parquet') WHERE entity_code = 'MK01'`

Always try the DuckDB table first (faster), then fall back to parquet if no results.

### Parquet Fallback (use when DuckDB tables return no data)

CRITICAL: If a query against a DuckDB table returns no results, ALWAYS retry using the
corresponding parquet file with read_parquet(). The parquet files often contain a fuller
date range (365+ days of forecasts) than the DuckDB tables.

- WTI: `SELECT * FROM read_parquet('/Users/fredhazelton/hazeydata/pipeline/wti/wti.parquet') WHERE park_code = 'EP' AND park_date = '2026-10-01'`
- Forecasts: `SELECT * FROM read_parquet('/Users/fredhazelton/hazeydata/pipeline/curves/forecast_parquet/all_forecasts.parquet') WHERE entity_code LIKE 'EP%' AND park_date = '2026-10-01'`
- Fact tables (historical waits): `SELECT * FROM read_parquet('/Users/fredhazelton/hazeydata/pipeline/fact_tables/parquet/*.parquet') WHERE entity_code = 'MK01'`

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
USAGE_FILE = Path(__file__).resolve().parent / "ask_usage.json"
QUESTION_LOG = Path(__file__).resolve().parent / "ask_questions.jsonl"


def _load_usage() -> dict:
    if USAGE_FILE.exists():
        return json.loads(USAGE_FILE.read_text())
    return {}


def _save_usage(data: dict):
    USAGE_FILE.write_text(json.dumps(data, indent=2))


def track_usage(user_id: str) -> tuple[int, int]:
    """Track usage and return (used_this_month, limit). Returns (-1, -1) if no limit."""
    data = _load_usage()
    month_key = date.today().strftime("%Y-%m")

    if month_key not in data:
        data[month_key] = {}

    user_key = str(user_id)
    count = data[month_key].get(user_key, 0) + 1
    data[month_key][user_key] = count
    _save_usage(data)

    return count, -1  # No limit for now (free launch)


def log_question(user_id: str, username: str, question: str, answer: str, duration_ms: int, model: str = "claude-sonnet-4-6"):
    """Append question + answer to JSONL log for analysis."""
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "user_id": str(user_id),
        "username": username,
        "question": question,
        "answer": answer[:2000],
        "duration_ms": duration_ms,
        "model": model,
    }
    with open(QUESTION_LOG, "a") as f:
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
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
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
            return "I hit a temporary glitch querying the data — please try your question again in a moment. If it keeps happening, let us know in #feedback!"

    return "Our database is momentarily busy — try your question again in a minute! The data refreshes constantly and things should clear up quickly. 🎢"


async def ask_agent(question: str, user_id: str, api_key: str, username: str = "unknown") -> str:
    """
    Process a natural language question about theme park data.
    Uses Claude with async DuckDB tool access.

    All blocking work (Anthropic API, DuckDB queries, file I/O) is run
    off the event loop so the Discord heartbeat is never starved.
    """
    await asyncio.to_thread(track_usage, user_id)

    import time as _time
    _start = _time.monotonic()

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=60.0, max_retries=1)

    today = date.today()
    system_prompt = SCHEMA_CONTEXT.format(today=today.isoformat(), today_dow=today.strftime("%A"))

    tools = [
        {
            "name": "run_query",
            "description": "Execute a read-only SQL query against the TPCR DuckDB database. Use SELECT statements only. Returns results as formatted text.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SQL SELECT query to execute"
                    }
                },
                "required": ["sql"]
            }
        }
    ]

    messages = [{"role": "user", "content": question}]

    model_used = "claude-sonnet-4-6"
    try:
        answer = await _claude_loop(client, system_prompt, tools, messages)
    except (anthropic.APIError, anthropic.APIConnectionError, anthropic.APITimeoutError, asyncio.TimeoutError) as e:
        print(f"⚠️ /ask: Anthropic failed ({type(e).__name__}: {str(e)[:160]}) — falling back to Qwen")
        model_used = QWEN_LABEL
        try:
            answer = await asyncio.to_thread(_qwen_loop, system_prompt, question)
        except Exception as qe:
            print(f"❌ /ask: Qwen fallback failed too: {qe}")
            answer = None

    if answer is not None:
        if len(answer) > 1900:
            answer = answer[:1900] + "..."
        duration_ms = int((_time.monotonic() - _start) * 1000)
        print(f"✅ /ask answered by {model_used} in {duration_ms} ms")
        await asyncio.to_thread(log_question, user_id, username, question, answer, duration_ms, model_used)
        if _is_bad_response(answer):
            _trigger_self_heal(question, user_id, username, answer)
        return answer

    fallback = "I wasn't able to fully answer that question. Try rephrasing or asking something more specific!"
    duration_ms = int((_time.monotonic() - _start) * 1000)
    await asyncio.to_thread(log_question, user_id, username, question, fallback, duration_ms, model_used)
    _trigger_self_heal(question, user_id, username, fallback)
    return fallback


QWEN_URL = os.environ.get("TPCR_QWEN_URL", "http://127.0.0.1:8080/v1/chat/completions")
QWEN_MODEL = os.environ.get("TPCR_QWEN_MODEL", "/Users/fredhazelton/models/Qwen3.8-27B-8bit")
QWEN_LABEL = "qwen-local"
_QWEN_TOOLS = [{"type": "function", "function": {
    "name": "run_query",
    "description": "Execute a read-only SQL query against the TPCR DuckDB database. Use SELECT statements only. Returns results as formatted text.",
    "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "SQL SELECT query to execute"}}, "required": ["sql"]}}}]


def _qwen_loop(system_prompt: str, question: str) -> Optional[str]:
    """Same prompt + run_query tool flow as Claude, against local mlx_lm (OpenAI-compatible). Blocking; call via to_thread."""
    import re
    import requests
    msgs = [{"role": "system", "content": system_prompt}, {"role": "user", "content": question}]
    for _ in range(5):
        r = requests.post(QWEN_URL, json={
            "model": QWEN_MODEL, "messages": msgs, "tools": _QWEN_TOOLS,
            "max_tokens": 700, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
        }, timeout=300)
        r.raise_for_status()
        m = r.json()["choices"][0]["message"]
        content = m.get("content") or ""
        calls = m.get("tool_calls") or []
        if not calls:
            for blk in re.findall(r"<tool_call>(.*?)</tool_call>", content, re.S):
                try:
                    j = json.loads(blk)
                    calls.append({"id": f"c{len(msgs)}", "function": {"name": j["name"], "arguments": json.dumps(j["arguments"])}})
                except Exception:
                    pass
        if not calls:
            text = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
            return text or None
        msgs.append({"role": "assistant", "content": content, "tool_calls": [
            {"id": c.get("id", "x"), "type": "function", "function": c["function"]} for c in calls]})
        for c in calls:
            try:
                sql = json.loads(c["function"]["arguments"]).get("sql", "")
            except Exception:
                sql = ""
            su = sql.strip().upper()
            result = run_duckdb_query(sql) if (su.startswith("SELECT") or su.startswith("WITH")) else "Error: Only SELECT queries are allowed."
            msgs.append({"role": "tool", "tool_call_id": c.get("id", "x"), "content": result})
    return None


async def _claude_loop(client, system_prompt, tools, messages) -> Optional[str]:
    # Allow up to 5 tool-use rounds
    for _ in range(5):
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        # Check if we need to handle tool use
        if response.stop_reason == "tool_use":
            # Process all tool calls
            tool_results = []
            assistant_content = response.content

            for block in response.content:
                if block.type == "tool_use":
                    sql = block.input.get("sql", "")

                    # Safety: reject non-SELECT queries
                    sql_upper = sql.strip().upper()
                    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
                        result = "Error: Only SELECT queries are allowed."
                    else:
                        result = await asyncio.to_thread(run_duckdb_query, sql)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})
        else:
            # Final text response
            text_parts = [b.text for b in response.content if hasattr(b, "text")]
            return "\n".join(text_parts)
    return None
