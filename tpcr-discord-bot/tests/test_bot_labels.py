"""Band-based WTI labels outside /ask: bot.py, daily_report.py, forecast_image.py (no network).

Bands come from ask_agent.normal_bands(); these tests never redefine the band formula.
The MK band written by write_parquet() is (14.7, 19.4); edges count as normal.
"""

import datetime as dt
import os
import re
import sys
import types
from pathlib import Path

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

for _mod in ("dotenv", "requests"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = types.ModuleType(_mod)
if not hasattr(sys.modules["dotenv"], "load_dotenv"):
    sys.modules["dotenv"].load_dotenv = lambda *a, **kw: None

import ask_agent as aa  # noqa: E402
import daily_report  # noqa: E402
import forecast_image  # noqa: E402

from .test_ask_qwen import bot  # noqa: E402,F401  (module-scoped bot import fixture)
from .test_ask_wording import TODAY, write_parquet  # noqa: E402

BANNED = re.compile(r"below avg|above avg|below average|above average|typical day|great day to visit|best day to visit", re.I)


@pytest.fixture
def parquet(tmp_path):
    aa._BANDS_CACHE.clear()
    write_parquet(str(tmp_path))
    yield tmp_path
    aa._BANDS_CACHE.clear()


# value -> label against MK band (14.7, 19.4)
CASES = [(12.0, " (below normal)"), (15.8, " (normal)"), (14.7, " (normal)"), (19.4, " (normal)"), (20.7, " (above normal)")]


@pytest.mark.parametrize("value,expected", CASES)
def test_bot_wti_label(bot, parquet, value, expected):
    assert bot.wti_label(value, "MK", TODAY) == expected


@pytest.mark.parametrize("value,expected", CASES)
def test_daily_report_wti_label(parquet, value, expected):
    assert daily_report.wti_label(value, "MK", TODAY) == expected


@pytest.mark.parametrize("value,expected", CASES)
def test_forecast_image_wti_label(parquet, value, expected):
    assert forecast_image.wti_label(value, "MK", TODAY) == expected


@pytest.mark.parametrize("value,tip", [(12.0, True), (15.8, True), (25.0, False)])
def test_crowd_footer_great_day_tip(bot, parquet, value, tip):
    assert ("Great day to enjoy" in bot.crowd_footer_tip(value, "MK", TODAY)) is tip


@pytest.mark.parametrize("value", [15.8, 25.0, 40.0, 60.0])
def test_crowd_footer_missing_band_no_tip(bot, tmp_path, value):
    aa._BANDS_CACHE.clear()
    assert bot.crowd_footer_tip(value, "MK", TODAY) == ""
    assert bot.crowd_footer_text(value, "MK", TODAY) == "Theme Park Crowd Report"


def test_crowd_footer_no_dangling_bullet(bot, parquet):
    assert bot.crowd_footer_text(25.0, "MK", TODAY) == "Theme Park Crowd Report"
    assert bot.crowd_footer_text(15.8, "MK", TODAY).endswith("  •  Theme Park Crowd Report")


def test_missing_data_omits_label(bot, tmp_path):
    # no parquet at all
    aa._BANDS_CACHE.clear()
    assert bot.wti_label(16.0, "MK", TODAY) == ""
    assert daily_report.wti_label(16.0, "MK", TODAY) == ""
    assert forecast_image.wti_label(16.0, "MK", TODAY) == ""


def test_unknown_park_and_no_park_code(bot, parquet):
    assert bot.wti_label(16.0, "ZZ", TODAY) == ""
    assert bot.wti_label(16.0, "", TODAY) == ""
    assert forecast_image.wti_label(16.0, "", TODAY) == ""


def test_short_history_omits_label(bot, tmp_path):
    aa._BANDS_CACHE.clear()
    write_parquet(str(tmp_path), parks=("MK",), days=100)
    assert bot.wti_label(16.0, "MK", TODAY) == ""


def _payload(get_wti):
    return daily_report.build_payload(TODAY, get_wti, None)["embeds"][0]["description"]


def test_daily_report_labels_in_text(parquet):
    desc = _payload(lambda code, d: {"MK": 12.0, "EP": 16.0, "HS": 30.0}.get(code))
    assert "**Magic Kingdom** — WTI **12** (below normal)\n" in desc
    # EP band is shifted +1 by write_parquet; 16 is inside it
    assert "**EPCOT** — WTI **16** (normal)\n" in desc
    assert "**Hollywood Studios** — WTI **30** (above normal)\n" in desc


def test_daily_report_without_bands_is_number_only(tmp_path):
    aa._BANDS_CACHE.clear()
    desc = _payload(lambda code, d: 20.0)
    assert "**Magic Kingdom** — WTI **20**\n" in desc
    assert "()" not in desc and "(*" not in desc and "**20** —" not in desc
    assert not BANNED.search(desc)


def test_daily_report_numbers_unchanged(parquet):
    with_bands = _payload(lambda code, d: 20.7)
    aa._BANDS_CACHE.clear()
    os.remove(parquet / "wti" / "wti.parquet")
    without = _payload(lambda code, d: 20.7)
    strip = lambda s: re.sub(r" \((?:below |above )?normal\)", "", s)  # noqa: E731
    assert strip(with_bands) == without


def test_forecast_image_year_label_text(parquet, monkeypatch):
    from PIL import ImageDraw
    drawn = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **k):
        drawn.append(text)
        return real(self, xy, text, *a, **k)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    start = dt.date(2026, 12, 1)
    days = [{"date": start + dt.timedelta(days=i), "wti_avg": 12.0 + (i % 10), "wti_low": 10, "wti_high": 30}
            for i in range(120)]
    try:
        forecast_image.generate_forecast_image("Magic Kingdom", days, "MK", TODAY)
        labelled = [t for t in drawn if "WTI" in t and "—" in t]
        drawn.clear()
        forecast_image.generate_forecast_image("Magic Kingdom", days)  # old signature: no label
        unlabelled = [t for t in drawn if "WTI" in t and "—" in t]
    except OSError:
        pytest.skip("fonts unavailable")
    assert labelled and unlabelled
    assert all(re.search(r"WTI \d+ \((below normal|normal|above normal)\)$", t) for t in labelled)
    assert all(re.search(r"WTI \d+$", t) for t in unlabelled)
    assert not any(BANNED.search(t) for t in labelled + unlabelled)


def test_no_banned_phrases_in_production_sources():
    """Old average-based labels must not remain in bot-package output modules.

    Allowed leftovers: ask_agent.py's "NEVER use ..." guard, /best-day command
    metadata, and "Best days to visit" image headings.
    """
    allowed = re.compile(
        r'name="best-day"|Find the best day to visit|'
        r"best day to visit Magic Kingdom|Best days to visit"
    )
    root = Path(HERE).parent
    for path in root.glob("*.py"):
        if path.name == "ask_agent.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if BANNED.search(line) and not allowed.search(line):
                pytest.fail(f"{path.name}:{i}: {line.strip()}")
