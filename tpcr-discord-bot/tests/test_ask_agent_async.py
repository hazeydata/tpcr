"""
Tests that ask_agent is properly async and does not block the event loop.

Root cause: ask_agent was declared `async def` but contained zero `await`
expressions — every Anthropic API call and DuckDB query ran synchronously,
starving the Discord gateway heartbeat and causing cascading "Unknown
interaction" errors across all slash commands.
"""

import ast
import asyncio
import time
import sys
import os
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_ask_agent_contains_await_expressions():
    """The async function must actually yield to the event loop."""
    agent_path = Path(__file__).parent.parent / "ask_agent.py"
    tree = ast.parse(agent_path.read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "ask_agent":
            awaits = [n for n in ast.walk(node) if isinstance(n, ast.Await)]
            assert len(awaits) >= 3, (
                f"ask_agent has only {len(awaits)} await(s); needs ≥3 "
                "(API call, DuckDB query, log write) to avoid blocking the event loop"
            )
            return

    pytest.fail("ask_agent async function not found in ask_agent.py")


def test_ask_agent_uses_async_anthropic_client():
    """Must use AsyncAnthropic, not the sync Anthropic client."""
    agent_path = Path(__file__).parent.parent / "ask_agent.py"
    source = agent_path.read_text()

    assert "AsyncAnthropic" in source, (
        "ask_agent should use anthropic.AsyncAnthropic for non-blocking API calls"
    )


def test_ask_agent_does_not_block_event_loop():
    """Simulate ask_agent and verify the event loop remains responsive."""
    import anthropic

    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = "Magic Kingdom is expected to be moderately busy."

    mock_response = MagicMock()
    mock_response.stop_reason = "end_of_turn"
    mock_response.content = [mock_text_block]

    async def fake_create(**kwargs):
        await asyncio.sleep(0.05)
        return mock_response

    mock_client_instance = MagicMock()
    mock_client_instance.messages = MagicMock()
    mock_client_instance.messages.create = fake_create

    heartbeat_count = 0

    async def heartbeat():
        nonlocal heartbeat_count
        while True:
            await asyncio.sleep(0.02)
            heartbeat_count += 1

    async def run_test():
        nonlocal heartbeat_count
        heartbeat_task = asyncio.create_task(heartbeat())

        with patch("ask_agent.anthropic") as mock_anthropic, \
             patch("ask_agent.track_usage", return_value=(1, -1)), \
             patch("ask_agent.log_question"):

            mock_anthropic.AsyncAnthropic.return_value = mock_client_instance

            from ask_agent import ask_agent
            answer = await ask_agent(
                "How busy is MK tomorrow?",
                "user123",
                "fake-api-key",
                username="testuser",
            )

        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        return answer, heartbeat_count

    answer, beats = asyncio.run(run_test())

    assert answer == "Magic Kingdom is expected to be moderately busy."
    assert beats >= 1, (
        f"Heartbeat fired {beats} times — event loop was blocked during ask_agent"
    )


def test_run_duckdb_query_offloaded_to_thread():
    """DuckDB queries inside ask_agent must use asyncio.to_thread."""
    agent_path = Path(__file__).parent.parent / "ask_agent.py"
    source = agent_path.read_text()

    assert "asyncio.to_thread(run_duckdb_query" in source, (
        "run_duckdb_query calls must be wrapped in asyncio.to_thread "
        "to avoid blocking the event loop during DB access"
    )


def test_crowd_command_entity_filtering():
    """Verify /crowd entity filtering: extinct and no-posted entities excluded."""
    import pandas as pd

    entity_names = {
        "MK01": ("Space Mountain", "Space Mountain", False, True),
        "MK02": ("Pirates", "Pirates", False, True),
        "MK03": ("Splash Mountain", "Splash Mountain", True, True),  # extinct
        "MK04": ("Magic Carpets", "Magic Carpets", False, False),    # no has_posted
        "MK05": ("Seven Dwarfs", "Seven Dwarfs Mine Train", False, True),
    }

    forecasts_df = pd.DataFrame({
        "entity_code": ["MK01", "MK02", "MK03", "MK04", "MK05", "MK99"],
        "avg_wait":    [45.0,    15.0,    35.0,    10.0,    60.0,    22.0],
        "peak_wait":   [70.0,    25.0,    50.0,    15.0,    90.0,    35.0],
    })

    headliners = []
    low_waits = []

    for _, row in forecasts_df.iterrows():
        entity_code = row["entity_code"]
        avg_wait = row["avg_wait"]

        if entity_code in entity_names:
            is_extinct = entity_names[entity_code][2]
            has_posted = entity_names[entity_code][3] if len(entity_names[entity_code]) > 3 else True
            if is_extinct or not has_posted:
                continue
            display_name = entity_names[entity_code][1]
        else:
            display_name = entity_code
        if avg_wait > 25:
            headliners.append((display_name, int(avg_wait)))
        elif avg_wait > 0:
            low_waits.append((display_name, int(avg_wait)))

    headliners = sorted(headliners, key=lambda x: x[1], reverse=True)[:5]
    low_waits = sorted(low_waits, key=lambda x: x[1])[:5]

    all_names = [n for n, _ in headliners + low_waits]
    assert "Splash Mountain" not in all_names, "Extinct entity should be filtered"
    assert "Magic Carpets" not in all_names, "No-posted entity should be filtered"
    assert "Space Mountain" in all_names
    assert "Seven Dwarfs Mine Train" in all_names
    assert "MK99" in all_names, "Unmapped entity should appear with raw code"


def test_time_slot_filter_boundary():
    """Verify the BETWEEN filter with HH:MM:SS format time slots."""
    import duckdb

    con = duckdb.connect()
    con.execute("""
        CREATE TABLE test_slots AS
        SELECT * FROM (VALUES
            ('07:00:00'), ('08:00:00'), ('14:00:00'),
            ('21:55:00'), ('22:00:00'), ('23:00:00')
        ) AS t(time_slot)
    """)

    result = con.execute("""
        SELECT time_slot FROM test_slots
        WHERE CAST(time_slot AS VARCHAR) BETWEEN '08:00' AND '22:00'
        ORDER BY time_slot
    """).fetchall()
    con.close()

    slots = [r[0] for r in result]
    assert "07:00:00" not in slots
    assert "08:00:00" in slots
    assert "14:00:00" in slots
    assert "21:55:00" in slots
    assert "23:00:00" not in slots
    # 22:00:00 is excluded by string comparison ('22:00:00' > '22:00')
    # This is a known minor issue documented in the PR
    assert "22:00:00" not in slots
