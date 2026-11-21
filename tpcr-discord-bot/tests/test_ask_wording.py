"""/ask wording guard: computed normal bands, banned-phrase rewrite, seasonal-claim removal (no network)."""

import asyncio
import datetime as dt
import os
import re
import sys
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

import ask_agent as aa  # noqa: E402

QUESTION = "MK busier than normal the weekend before Thanksgiving isn't a shock. Do you think it stays like this"
TODAY = dt.date(2026, 11, 21)

FIXTURE = """Good news — it's already easing, and it stays mild the rest of the week. 📉

**This week's Magic Kingdom forecast (WTI):**
• Sat 11/21: 17.0
• Sun 11/22: 16.4
• Mon 11/23: 17.0
• Tue 11/24: 15.8 ← quietest day
• Wed 11/25: 16.6
• Thu 11/26: 17.7
• Fri 11/27: 17.4

For context, last weekend was the "busier than normal" you're thinking of — Saturday 11/14 hit 20.7, and even the weekdays ran 12–19. This week is a clear step down: every day sits in the 15.8–17.7 range, which is "below average — great day to visit" territory.

So no, it doesn't stay elevated — the bump has already faded, and it stays comfortable through Thursday (and Friday). Tuesday 11/24 looks like the sweet spot if you're picking a day. Just keep in mind the week after Thanksgiving (starting ~11/30) is when crowds typically spike again, so if you can visit this week, do it. 🎢"""

BANNED = ["below average", "above average", "typical day", "great day to visit", "best day to visit", "typically"]


def run(coro):
    return asyncio.run(coro)


def write_parquet(root, parks=("MK", "EP", "HS", "AK"), days=365):
    vals = [10.0, 14.7, 17.0, 19.4, 25.0]
    rows = []
    for k, p in enumerate(parks):
        for i in range(days):
            # shift the pattern per park so parks differ; MK (k=0) keeps exact 14.7/19.4 quartiles
            rows.append((p, dt.date(2022, 1, 1) + dt.timedelta(days=i), vals[(i // 73) % 5] + k, "historical"))
        for i in range(30):  # must be ignored
            rows.append((p, dt.date(2026, 12, 1) + dt.timedelta(days=i), 99.0, "forecast"))
            rows.append((p, dt.date(2026, 11, 21) + dt.timedelta(days=i), 99.0, "historical"))  # >= as_of
    df = pd.DataFrame(rows, columns=["park_code", "park_date", "wti", "source"])
    df["park_date"] = pd.to_datetime(df.park_date)
    os.makedirs(os.path.join(root, "wti"), exist_ok=True)
    df.to_parquet(os.path.join(root, "wti", "wti.parquet"))
    return os.path.join(root, "wti", "wti.parquet")


@pytest.fixture
def parquet(tmp_path):
    return write_parquet(str(tmp_path))


@pytest.fixture(autouse=True)
def no_side_effects():
    aa._BANDS_CACHE.clear()
    with patch("ask_agent.track_usage", return_value=(1, -1)), \
         patch("ask_agent.log_question"), \
         patch("ask_agent._trigger_self_heal"):
        yield


MK = {"MK": (14.7, 19.4)}


# ---------------------------------------------------------------- bands

def test_normal_bands_mk(parquet):
    b = aa.normal_bands(TODAY)
    assert b["MK"] == (14.7, 19.4)


def test_parity_with_cards_boxes(parquet):
    sys.path.insert(0, os.path.join(HERE, "..", "..", "operations", "tweet_v2"))
    import cards
    ref = cards.boxes(parquet, TODAY.isoformat())
    b = aa.normal_bands(TODAY)
    for p in ("MK", "EP", "HS", "AK"):
        assert b[p] == pytest.approx(tuple(ref[p]))


def test_short_history_skipped_and_missing_parquet(tmp_path):
    write_parquet(str(tmp_path), parks=("MK",))
    df = pd.read_parquet(tmp_path / "wti" / "wti.parquet")
    short = df[(df.park_code == "MK")].head(100).assign(park_code="UF")
    pd.concat([df, short]).to_parquet(tmp_path / "wti" / "wti.parquet")
    b = aa.normal_bands(TODAY)
    assert "MK" in b and "UF" not in b
    os.remove(tmp_path / "wti" / "wti.parquet")
    aa._BANDS_CACHE.clear()
    assert aa.normal_bands(TODAY) == {}


def test_band_label_edges():
    assert aa.band_label(14.7, (14.7, 19.4)) == "normal"
    assert aa.band_label(19.4, (14.7, 19.4)) == "normal"
    assert aa.band_label(14.69, (14.7, 19.4)) == "below normal"
    assert aa.band_label(19.41, (14.7, 19.4)) == "above normal"


# ---------------------------------------------------------------- enforce_wording

def test_fixture_direct():
    out = aa.enforce_wording(FIXTURE, QUESTION, MK)
    assert '15.8–17.7 range, which is "normal" territory.' in out
    for d in ("Sun 11/22: 16.4 (normal)", "Tue 11/24: 15.8 (normal) ← quietest day", "Fri 11/27: 17.4 (normal)"):
        assert d in out
    assert "Saturday 11/14 hit 20.7" in out and "12–19" in out
    assert "📉" in out
    assert "🎢" not in out


def test_adversarial_inline():
    out = aa.enforce_wording("MK Tuesday 11/24 is 15.8, below average — great day to visit!", "q", MK)
    assert "normal" in out and "15.8" in out
    assert not any(b in out.lower() for b in BANNED)


def test_seasonal_removed():
    out = aa.enforce_wording("Crowds typically spike after Thanksgiving, so go now.\n**MK**\n• Tue 11/24: 15.8", "q", MK)
    assert "typically" not in out and "Thanksgiving" not in out
    assert "(normal)" in out


def test_above_and_below_bands():
    out = aa.enforce_wording("WTI forecast:\n• Sat 11/28: 22.3 — below average", QUESTION, MK)
    assert out == "WTI forecast:\n• Sat 11/28: 22.3 (above normal)"
    out = aa.enforce_wording("WTI:\n- Sun 11/29: 12.1, great day to visit", QUESTION, MK)
    assert "12.1 (below normal)" in out and "great day" not in out


def test_no_bands_deletes_without_labels():
    text = "**MK:**\n• Tue 11/24: 15.8 — below average\nThis is a great day to visit at 15.8."
    out = aa.enforce_wording(text, QUESTION, {})
    assert "(normal)" not in out and "normal" not in out
    assert not any(b in out.lower() for b in BANNED)
    assert re.findall(r"\d+\.\d+", out) == ["15.8", "15.8"]


def test_ride_wait_minutes_not_labelled():
    text = "Space Mountain forecast:\n• Tue 11/24: 45 min\n• Wed 11/25: 50 min\nThat's below average for Space Mountain."
    out = aa.enforce_wording(text, QUESTION, MK)
    assert "normal" not in out and "below average" not in out
    assert "• Tue 11/24: 45 min" in out and "• Wed 11/25: 50 min" in out


def test_ride_wait_with_wti_context_still_unlabelled():
    text = "Crowd is light.\n• Sat 11/21: 35 minutes\n• Sun 11/22: 40%\nThat's above average, 30–45 minutes."
    out = aa.enforce_wording(text, QUESTION, MK)
    assert "normal" not in out and "above average" not in out
    assert "35 minutes" in out and "40%" in out and "30–45 minutes" in out


def test_wti_line_needs_wti_context():
    out = aa.enforce_wording("• Tue 11/24: 15.8", QUESTION, MK)
    assert "normal" not in out


def test_comparative_average_wording():
    for phrase in ("quieter than average", "less crowded than usual", "busier than typical", "heavier than usual"):
        out = aa.enforce_wording(f"MK WTI: Tuesday 11/24 is 15.8, {phrase}.", "q", MK)
        assert phrase not in out and "normal" in out and "15.8" in out
    out = aa.enforce_wording("MK WTI: Tuesday 11/24 is 15.8, quieter than average.", "q", MK)
    assert out == "MK WTI: Tuesday 11/24 is 15.8, normal."
    out = aa.enforce_wording("MK WTI: Sat 11/28 is 15.8 and Sun 11/29 is 22.0, busier than usual.", "q", MK)
    assert "busier than usual" not in out and "15.8" in out and "22.0" in out


def test_comparative_than_normal_value_backed():
    out = aa.enforce_wording("MK WTI: Saturday hit 20.7, busier than normal.", "q", MK)
    assert out == "MK WTI: Saturday hit 20.7, busier than normal."
    out = aa.enforce_wording("MK WTI: Saturday hit 12.0, quieter than normal.", "q", MK)
    assert "quieter than normal" in out
    out = aa.enforce_wording("MK WTI: Tuesday 11/24 at 15.8 is quieter than normal.", "q", MK)
    assert "quieter than normal" not in out and "15.8" in out and "normal" in out
    out = aa.enforce_wording("MK WTI: Tuesday at 15.8 is busier than normal.", "q", MK)
    assert "busier than normal" not in out and "15.8" in out
    out = aa.enforce_wording("WTI: Saturday hit 20.7, busier than normal.", "q", {})
    assert "busier than normal" not in out and "20.7" in out


DATA = "MK WTI forecast:\n• Sat 11/21: 17.0\n• Fri 11/27: 17.4\n"


def test_forward_month_holiday_dropped():
    for sent in ("Expect crowds to pick up in December.", "Christmas week is when it gets busy.",
                 "Spring break crowds arrive later."):
        out = aa.enforce_wording(DATA + sent, "q", MK, TODAY)
        assert sent not in out and "17.4" in out and "17.0" in out
    out = aa.enforce_wording("Expect crowds to pick up in December.", "q", MK, TODAY)
    assert out == ""
    out = aa.enforce_wording("MK WTI is 17.0 today. Expect crowds to pick up in December.", "q", MK, TODAY)
    assert out == "MK WTI is 17.0 today."


def test_month_holiday_in_horizon_or_past_kept():
    for sent in ("Last December was busy.", "Back in December it hit 25.0.", "In December 2025 it hit 25.0.",
                 "Thanksgiving Day is Thu 11/26: 17.7 so plan around it.", "November has been mild."):
        out = aa.enforce_wording(DATA + sent, "q", MK, TODAY)
        assert sent in out, sent
    assert "Nov 22" in aa.enforce_wording(DATA + "Nov 22 looks fine.", "q", MK, TODAY)


# ---------------------------------------------------------------- end to end

class FrozenDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 21)


def _ask(text, parquet_present=True, claude=False):
    with patch("ask_agent.date", FrozenDate):
        if claude:
            with patch("ask_agent._claude_loop", AsyncMock(return_value=(text, 1))), \
                 patch.dict(os.environ, {"TPCR_ASK_PROVIDER": "anthropic"}):
                return run(aa.ask_agent(QUESTION, "u1", api_key="k"))
        chat = AsyncMock(return_value={"choices": [{"message": {"role": "assistant", "content": text}}]})
        with patch("ask_agent._chat", chat):
            ans = run(aa.ask_agent(QUESTION, "u1"))
        return ans, chat


def test_replay_fred_answer(parquet):
    ans, chat = _ask(FIXTURE)
    for d in ("Sun", "Mon", "Tue", "Wed", "Thu"):
        assert re.search(rf"• {d} 11/\d\d: \d+\.\d \(normal\)", ans)
    nums = lambda t: re.findall(r"\d+(?:\.\d+)?", t)
    before, after = nums(FIXTURE), nums(ans)
    assert re.findall(r"\d+\.\d+", ans) == re.findall(r"\d+\.\d+", FIXTURE)  # every WTI value kept, in order
    assert all(a in before for a in after)  # no new numbers
    for bad in ("below average", "great day to visit", "typically", "spike", "11/30", "Thanksgiving (starting"):
        assert bad not in ans
    out_path = os.environ.get("TPCR_REPLAY_OUT")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(ans)
    # (g) prompt carries the computed band, not the old table
    system = chat.call_args_list[0].args[0][0]["content"]
    assert "MK 14.7–19.4" in system
    assert "Below average — great day to visit" not in system


def test_claude_path_guarded(parquet):
    ans = _ask("**MK WTI**\n• Tue 11/24: 15.8 — below average, great day to visit", claude=True)
    assert ans == "**MK WTI**\n• Tue 11/24: 15.8 (normal)"


def test_no_parquet_end_to_end():
    ans, chat = _ask("MK Tuesday 11/24 is 15.8, below average — great day to visit!")
    assert "15.8" in ans and not any(b in ans.lower() for b in BANNED)
    system = chat.call_args_list[0].args[0][0]["content"]
    assert "Do not label crowd levels" in system
