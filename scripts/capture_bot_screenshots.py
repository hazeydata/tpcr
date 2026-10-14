#!/usr/bin/env python3
"""
Generate Discord-style embed screenshots for the website.
Renders HTML that mimics Discord embed styling, then captures with Playwright.

Pulls live data from DuckDB when available, falls back to hardcoded defaults.
Generates 5 screenshots: /today, /now, /crowd, /best-day, /ask
"""
import subprocess
import json
import sys
import os
from pathlib import Path
from datetime import datetime, date

# Get the repo root
REPO = Path(__file__).parent.parent
ASSETS = Path("/home/wilma/hazeydata.ai/assets")

DUCKDB_PATH = "/mnt/data/pipeline/tpcr_live.duckdb"

# Park display metadata
PARK_INFO = {
    "MK":  {"emoji": "🏰", "name": "Magic Kingdom", "group": "Walt Disney World"},
    "EP":  {"emoji": "🌐", "name": "EPCOT", "group": "Walt Disney World"},
    "HS":  {"emoji": "🎬", "name": "Hollywood Studios", "group": "Walt Disney World"},
    "AK":  {"emoji": "🦁", "name": "Animal Kingdom", "group": "Walt Disney World"},
    "DL":  {"emoji": "🎠", "name": "Disneyland", "group": "Disneyland Resort"},
    "CA":  {"emoji": "🎢", "name": "California Adventure", "group": "Disneyland Resort"},
    "UF":  {"emoji": "🦈", "name": "Universal Studios", "group": "Universal Orlando"},
    "IA":  {"emoji": "🏝️", "name": "Islands of Adventure", "group": "Universal Orlando"},
    "EU":  {"emoji": "🌟", "name": "Epic Universe", "group": "Universal Orlando"},
    "UH":  {"emoji": "🎬", "name": "Universal Hollywood", "group": "Universal Hollywood"},
    "TDL": {"emoji": "🗼", "name": "Tokyo Disneyland", "group": "Tokyo Disney Resort"},
    "TDS": {"emoji": "⛩️", "name": "Tokyo DisneySea", "group": "Tokyo Disney Resort"},
}

# Entities to exclude from ride lists (waypoints, LL/G+, extinct, shows, etc.)
EXCLUDE_PATTERNS = [
    "Waypoint", "LL/G+", "FP Booth", "FP", "Entrance",
    "Waypoint", "Single Rider",
]

# Known ride entity codes for MK (main attractions that show wait times)
MK_RIDE_CODES = {
    "MK01": "Space Mountain",
    "MK02": "Buzz Lightyear",
    "MK03": "Big Thunder Mtn",
    "MK05": "Peter Pan's Flight",
    "MK06": "Winnie the Pooh",
    "MK13": "Jungle Cruise",
    "MK15": "Magic Carpets",
    "MK16": "Pirates of Caribbean",
    "MK17": "Swiss Family Tree",
    "MK18": "Country Bears",
    "MK21": "Railroad Main St",
    "MK22": "Hall of Presidents",
    "MK23": "Haunted Mansion",
    "MK25": "Ariel's Grotto",
    "MK26": "Regal Carrousel",
    "MK27": "Dumbo",
    "MK28": "it's a small world",
    "MK29": "Mad Tea Party",
    "MK30": "PhilharMagic",
    "MK34": "Barnstormer",
    "MK39": "Astro Orbiter",
    "MK40": "Laugh Floor",
    "MK43": "Tom'land Speedway",
    "MK44": "PeopleMover",
    "MK45": "Carousel of Progress",
    "MK46": "Enchanted Tiki Rm",
    "MK141": "7 Dwarfs Mine Train",
    "MK142": "Under the Sea",
    "MK191": "TRON Lightcycle / Run",
    "MK210": "Tiana's Bayou Adventure",
}


def get_live_data():
    """Pull live data from DuckDB. Returns dict or None if unavailable."""
    try:
        import duckdb
        con = duckdb.connect(DUCKDB_PATH, read_only=True)

        # WTI for all parks today
        wti_rows = con.sql(
            "SELECT park_code, wti FROM wti WHERE park_date = CURRENT_DATE AND time_slot = 'daily'"
        ).fetchall()
        wti = {r[0]: round(r[1], 1) for r in wti_rows}

        # Entity names
        ent_rows = con.sql("SELECT entity_code, short_name FROM entities").fetchall()
        entities = {r[0]: r[1] for r in ent_rows}

        # Forecasts for MK today (avg predicted wait)
        fc_rows = con.sql(
            "SELECT entity_code, AVG(predicted_actual) as avg_wait FROM forecasts "
            "WHERE entity_code LIKE 'MK%' AND park_date = CURRENT_DATE "
            "GROUP BY entity_code ORDER BY avg_wait DESC"
        ).fetchall()
        forecasts = {r[0]: round(r[1]) for r in fc_rows}

        # Best-day data (7 day WTI for MK)
        bd_rows = con.sql(
            "SELECT park_date, wti FROM wti WHERE park_code = 'MK' "
            "AND park_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 7 ORDER BY park_date"
        ).fetchall()
        bestday = [(r[0], round(r[1], 1)) for r in bd_rows]

        # Live waits for MK (most recent per entity)
        lw_rows = con.sql(
            "SELECT entity_code, wait_time_minutes FROM live_waits "
            "WHERE entity_code LIKE 'MK%' AND observed_at > NOW() - INTERVAL '30 minutes'"
        ).fetchall()
        # Aggregate: take max wait per entity (multiple readings in 30 min window)
        live_waits = {}
        for code, wait in lw_rows:
            if code not in live_waits or wait > live_waits[code]:
                live_waits[code] = wait

        con.close()
        return {
            "wti": wti,
            "entities": entities,
            "forecasts": forecasts,
            "bestday": bestday,
            "live_waits": live_waits,
        }
    except Exception as e:
        print(f"⚠️  DuckDB unavailable ({e}), using fallback data")
        return None


def get_fallback_data():
    """Hardcoded fallback data when DuckDB is unavailable."""
    return {
        "wti": {
            "MK": 42, "EP": 31, "HS": 56, "AK": 31,
            "DL": 49, "CA": 35,
            "UF": 39, "IA": 44, "EU": 62,
            "UH": 34,
            "TDL": 52, "TDS": 47,
        },
        "entities": {k: v for k, v in MK_RIDE_CODES.items()},
        "forecasts": {
            "MK191": 65, "MK05": 43, "MK13": 42, "MK141": 41,
            "MK210": 35, "MK23": 34, "MK01": 32, "MK06": 28,
            "MK25": 24, "MK39": 23, "MK28": 18, "MK02": 15,
            "MK44": 10, "MK45": 7, "MK43": 9,
        },
        "bestday": [
            (date.today(), 42),
        ],
        "live_waits": {
            "MK141": 72, "MK191": 65, "MK05": 61,
            "MK01": 48, "MK03": 42, "MK13": 38,
            "MK23": 33, "MK16": 30,
            "MK28": 22, "MK02": 18, "MK25": 25,
            "MK39": 11, "MK43": 9, "MK45": 7, "MK44": 5,
        },
    }


def get_entity_name(code, data):
    """Get friendly entity name, preferring our curated names, falling back to DB."""
    if code in MK_RIDE_CODES:
        return MK_RIDE_CODES[code]
    if code in data.get("entities", {}):
        return data["entities"][code]
    return code


def is_ride_entity(code, name):
    """Filter out non-ride entities (waypoints, LL/G+, etc.)."""
    for pattern in EXCLUDE_PATTERNS:
        if pattern.lower() in name.lower():
            return False
    # Only include entities in our curated ride list
    return code in MK_RIDE_CODES


def wti_label(wti_val):
    """Return a crowd level label for a WTI value."""
    if wti_val <= 15:
        return "Very low"
    elif wti_val <= 30:
        return "Below avg"
    elif wti_val <= 50:
        return "Moderate"
    elif wti_val <= 70:
        return "High"
    else:
        return "Very high"


def create_today_html(data):
    """Create a Discord-style /today embed with live WTI data."""
    today = datetime.now().strftime("%b %-d")
    wti = data["wti"]

    # Group parks
    groups = {}
    for code, info in PARK_INFO.items():
        g = info["group"]
        if g not in groups:
            groups[g] = []
        w = wti.get(code, "—")
        groups[g].append((info["emoji"], info["name"], w))

    # Find busiest/quietest
    valid = {k: v for k, v in wti.items() if k in PARK_INFO and isinstance(v, (int, float))}
    if valid:
        busiest_code = max(valid, key=valid.get)
        quietest_code = min(valid, key=valid.get)
        busiest = f"{PARK_INFO[busiest_code]['name']} (WTI {valid[busiest_code]:.0f})"
        quietest = f"{PARK_INFO[quietest_code]['name']} (WTI {valid[quietest_code]:.0f})"
    else:
        busiest = "N/A"
        quietest = "N/A"

    # Build park lines HTML
    sections = []
    group_order = [
        "Walt Disney World", "Disneyland Resort", "Universal Orlando",
        "Universal Hollywood", "Tokyo Disney Resort"
    ]
    for g in group_order:
        if g not in groups:
            continue
        lines = []
        for emoji, name, w in groups[g]:
            w_display = f"{w:.0f}" if isinstance(w, float) else str(w)
            lines.append(f'    <div class="park-line">▸ {emoji} {name} — WTI <b>{w_display}</b></div>')
        sections.append(f'    <div class="section-header">{g}</div>\n' + "\n".join(lines))

    park_html = "\n    <hr class=\"separator\">\n".join(sections)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ margin: 0; padding: 20px; background: #313338; font-family: 'gg sans', 'Noto Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; }}
.embed {{ background: #2b2d31; border-left: 4px solid #0a2f8f; border-radius: 4px; padding: 12px 16px; max-width: 520px; color: #dbdee1; font-size: 14px; line-height: 1.5; }}
.title {{ font-size: 16px; font-weight: 700; color: #00a8fc; margin-bottom: 8px; }}
.section-header {{ font-weight: 700; color: #f2f3f5; margin-top: 12px; margin-bottom: 4px; font-size: 14px; }}
.park-line {{ color: #dbdee1; padding: 2px 0; font-size: 13px; }}
.separator {{ border: none; border-top: 1px solid #3f4147; margin: 8px 0; }}
.footer {{ color: #949ba4; font-size: 12px; margin-top: 10px; }}
.muted {{ color: #949ba4; }}
</style></head><body>
<div class="embed">
    <div class="title">📊 All Parks — {today}</div>
    
{park_html}
    
    <hr class="separator">
    <div class="park-line muted">📈 Busiest: {busiest} • Quietest: {quietest}</div>
    
    <div class="footer">Crowd forecasts • hazeydata.ai</div>
</div>
</body></html>"""


def create_now_html(data):
    """Create a Discord-style /now embed with live wait times."""
    wti = data["wti"]
    live_waits = data["live_waits"]
    mk_wti = wti.get("MK", 42)
    mk_wti_display = f"{mk_wti:.0f}" if isinstance(mk_wti, float) else str(mk_wti)

    # Build ride list from live waits, filtering to real rides
    rides = []
    for code, wait in live_waits.items():
        name = get_entity_name(code, data)
        if is_ride_entity(code, name) and wait and wait > 0:
            rides.append((name, wait))
    rides.sort(key=lambda x: -x[1])

    # Tier the rides
    extreme = [(n, w) for n, w in rides if w >= 60]
    long_ = [(n, w) for n, w in rides if 30 <= w < 60]
    moderate = [(n, w) for n, w in rides if 15 <= w < 30]
    short = [(n, w) for n, w in rides if w < 15]

    def tier_html(header, emoji, items):
        if not items:
            return ""
        lines = [f'    <div class="tier-header">{emoji} {header}</div>']
        for name, wait in items[:6]:  # Cap at 6 per tier
            lines.append(f'    <div class="ride-line">▸ {name} — {wait} min</div>')
        return "\n".join(lines)

    tiers = []
    t = tier_html("Extreme (60+ min)", "🔴", extreme)
    if t:
        tiers.append(t)
    t = tier_html("Long (30-60 min)", "🟠", long_)
    if t:
        tiers.append(t)
    t = tier_html("Moderate (15-30 min)", "🟡", moderate)
    if t:
        tiers.append(t)
    t = tier_html("Short (<15 min)", "🟢", short)
    if t:
        tiers.append(t)

    tiers_html = '\n    <hr class="separator">\n'.join(tiers)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ margin: 0; padding: 20px; background: #313338; font-family: 'gg sans', 'Noto Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; }}
.embed {{ background: #2b2d31; border-left: 4px solid #2ecc71; border-radius: 4px; padding: 12px 16px; max-width: 520px; color: #dbdee1; font-size: 14px; line-height: 1.5; }}
.title {{ font-size: 16px; font-weight: 700; color: #00a8fc; margin-bottom: 8px; }}
.tier-header {{ font-weight: 700; color: #f2f3f5; margin-top: 10px; margin-bottom: 4px; font-size: 13px; }}
.ride-line {{ color: #dbdee1; padding: 1px 0; font-size: 13px; }}
.separator {{ border: none; border-top: 1px solid #3f4147; margin: 6px 0; }}
.footer {{ color: #949ba4; font-size: 12px; margin-top: 10px; }}
.muted {{ color: #949ba4; }}
</style></head><body>
<div class="embed">
    <div class="title">🏰 Magic Kingdom — WTI {mk_wti_display}</div>
    
{tiers_html}
    
    <div class="footer">Updated every 5 min • crowd forecasts • hazeydata.ai</div>
</div>
</body></html>"""


def create_crowd_html(data):
    """Create a Discord-style /crowd embed with forecast data."""
    today_str = datetime.now().strftime("%b %-d")
    wti = data["wti"]
    forecasts = data["forecasts"]
    mk_wti = wti.get("MK", 42)
    mk_wti_display = f"{mk_wti:.0f}" if isinstance(mk_wti, float) else str(mk_wti)
    crowd_label = wti_label(mk_wti if isinstance(mk_wti, (int, float)) else 42)

    # Top headliners from forecasts
    headliners = []
    for code, wait in sorted(forecasts.items(), key=lambda x: -x[1]):
        name = get_entity_name(code, data)
        if is_ride_entity(code, name):
            headliners.append((name, wait))
        if len(headliners) >= 4:
            break

    # Low wait picks
    low_picks = []
    for code, wait in sorted(forecasts.items(), key=lambda x: x[1]):
        name = get_entity_name(code, data)
        if is_ride_entity(code, name) and wait <= 15:
            low_picks.append((name, wait))
        if len(low_picks) >= 3:
            break

    headliner_html = "\n".join(
        f'    <div class="detail">▸ {n} — {w} min</div>' for n, w in headliners
    )
    low_html = "\n".join(
        f'    <div class="detail">▸ {n} — {w} min</div>' for n, w in low_picks
    )

    # Determine crowd quality
    if mk_wti <= 30:
        quality = '<span class="good">Great day to visit!</span> Well below average wait times.'
    elif mk_wti <= 50:
        quality = '<span class="good">Good day to visit!</span> Below average wait times expected.'
    elif mk_wti <= 65:
        quality = 'Moderate crowds expected. Plan for some longer waits.'
    else:
        quality = '⚠️ High crowds expected. Use Lightning Lane for headliners.'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ margin: 0; padding: 20px; background: #313338; font-family: 'gg sans', 'Noto Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; }}
.embed {{ background: #2b2d31; border-left: 4px solid #0a2f8f; border-radius: 4px; padding: 12px 16px; max-width: 520px; color: #dbdee1; font-size: 14px; line-height: 1.5; }}
.title {{ font-size: 16px; font-weight: 700; color: #00a8fc; margin-bottom: 8px; }}
.detail {{ color: #dbdee1; padding: 2px 0; font-size: 13px; }}
.separator {{ border: none; border-top: 1px solid #3f4147; margin: 8px 0; }}
.section-header {{ font-weight: 700; color: #f2f3f5; margin-top: 8px; margin-bottom: 4px; font-size: 13px; }}
.footer {{ color: #949ba4; font-size: 12px; margin-top: 10px; }}
.muted {{ color: #949ba4; }}
.good {{ color: #2ecc71; font-weight: 600; }}
</style></head><body>
<div class="embed">
    <div class="title">🏰 Magic Kingdom — {today_str}</div>
    <div class="detail">{crowd_label} WTI. WTI {mk_wti_display} — expect {max(10, int(mk_wti) - 10)}-{int(mk_wti) + 10} min on headliners.</div>
    
    <hr class="separator">
    <div class="section-header">Today's Forecast</div>
    <div class="detail">▸ {quality}</div>
    
    <hr class="separator">
    <div class="section-header">Top Headliners</div>
{headliner_html}
    
    <hr class="separator">
    <div class="section-header">Low Wait Picks</div>
{low_html}
    
    <div class="footer">Crowd forecasts • hazeydata.ai</div>
</div>
</body></html>"""


def create_bestday_html(data):
    """Create a Discord-style /best-day embed showing 7-day WTI view."""
    bestday = data.get("bestday", [])

    # Find best day (lowest WTI)
    if bestday:
        best = min(bestday, key=lambda x: x[1])
        best_date = best[0]
    else:
        best_date = None

    def day_line(d, wti_val, is_best):
        day_name = d.strftime("%a %b %-d")
        wti_display = f"{wti_val:.0f}" if isinstance(wti_val, float) else str(wti_val)
        label = wti_label(wti_val)

        # Bar visualization (scale 0-100)
        bar_pct = min(100, max(5, int(wti_val * 1.2)))
        if wti_val <= 15:
            bar_color = "#2ecc71"
        elif wti_val <= 30:
            bar_color = "#27ae60"
        elif wti_val <= 50:
            bar_color = "#f39c12"
        elif wti_val <= 70:
            bar_color = "#e67e22"
        else:
            bar_color = "#e74c3c"

        star = " ⭐" if is_best else ""
        highlight = ' style="background: #3a3d43; border-radius: 4px; padding: 2px 6px; margin: 1px 0;"' if is_best else ""

        return f"""    <div class="day-row"{highlight}>
        <div class="day-name">{day_name}{star}</div>
        <div class="day-bar-wrap"><div class="day-bar" style="width: {bar_pct}%; background: {bar_color};"></div></div>
        <div class="day-wti">WTI {wti_display} <span class="label">({label})</span></div>
    </div>"""

    day_lines = "\n".join(
        day_line(d, w, d == best_date) for d, w in bestday
    ) if bestday else '    <div class="detail muted">No forecast data available</div>'

    best_label = best_date.strftime("%A, %b %-d") if best_date else "N/A"
    best_wti = f"{best[1]:.0f}" if bestday else "—"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ margin: 0; padding: 20px; background: #313338; font-family: 'gg sans', 'Noto Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; }}
.embed {{ background: #2b2d31; border-left: 4px solid #0a2f8f; border-radius: 4px; padding: 12px 16px; max-width: 520px; color: #dbdee1; font-size: 14px; line-height: 1.5; }}
.title {{ font-size: 16px; font-weight: 700; color: #00a8fc; margin-bottom: 8px; }}
.subtitle {{ color: #dbdee1; font-size: 13px; margin-bottom: 12px; }}
.day-row {{ display: flex; align-items: center; gap: 10px; padding: 3px 0; font-size: 13px; }}
.day-name {{ color: #f2f3f5; font-weight: 600; min-width: 100px; white-space: nowrap; }}
.day-bar-wrap {{ flex: 1; height: 8px; background: #1e1f22; border-radius: 4px; overflow: hidden; min-width: 80px; }}
.day-bar {{ height: 100%; border-radius: 4px; }}
.day-wti {{ color: #dbdee1; min-width: 140px; text-align: right; white-space: nowrap; }}
.label {{ color: #949ba4; font-size: 12px; }}
.separator {{ border: none; border-top: 1px solid #3f4147; margin: 10px 0; }}
.best-pick {{ color: #2ecc71; font-weight: 600; font-size: 14px; margin-top: 4px; }}
.footer {{ color: #949ba4; font-size: 12px; margin-top: 10px; }}
.detail {{ color: #dbdee1; font-size: 13px; }}
.muted {{ color: #949ba4; }}
</style></head><body>
<div class="embed">
    <div class="title">📅 Best Day — Magic Kingdom</div>
    <div class="subtitle">7-day crowd forecast (WTI = Wait Time Index)</div>
    
{day_lines}
    
    <hr class="separator">
    <div class="best-pick">⭐ Best day: {best_label} (WTI {best_wti})</div>
    <div class="detail muted">Lower WTI = shorter waits. Plan to arrive early for best experience.</div>
    
    <div class="footer">Crowd forecasts • hazeydata.ai</div>
</div>
</body></html>"""


def create_ask_html(data):
    """Create a Discord-style /ask Q&A embed."""
    wti = data["wti"]
    bestday = data.get("bestday", [])
    mk_wti = wti.get("MK", 42)

    # Build a realistic AI answer using live data
    if bestday:
        best = min(bestday, key=lambda x: x[1])
        best_day_name = best[0].strftime("%A")
        best_wti_val = f"{best[1]:.0f}"
        worst = max(bestday, key=lambda x: x[1])
        worst_day_name = worst[0].strftime("%A")
        worst_wti_val = f"{worst[1]:.0f}"
    else:
        best_day_name = "Tuesday"
        best_wti_val = "28"
        worst_day_name = "Saturday"
        worst_wti_val = "62"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ margin: 0; padding: 20px; background: #313338; font-family: 'gg sans', 'Noto Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; }}
.message {{ max-width: 560px; }}
.user-msg {{ display: flex; align-items: flex-start; gap: 12px; margin-bottom: 16px; }}
.avatar {{ width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 18px; color: white; font-weight: 700; }}
.user-avatar {{ background: #5865f2; }}
.bot-avatar {{ background: #0a2f8f; }}
.msg-content {{ flex: 1; }}
.username {{ font-weight: 600; font-size: 14px; margin-bottom: 2px; }}
.user-name {{ color: #8db5e0; }}
.bot-name {{ color: #7289da; }}
.bot-tag {{ background: #5865f2; color: white; font-size: 10px; padding: 1px 5px; border-radius: 3px; margin-left: 6px; font-weight: 500; vertical-align: middle; }}
.msg-text {{ color: #dbdee1; font-size: 14px; line-height: 1.5; }}
.slash-cmd {{ color: #00a8fc; background: rgba(0, 168, 252, 0.1); padding: 0 2px; border-radius: 3px; }}
.embed {{ background: #2b2d31; border-left: 4px solid #0a2f8f; border-radius: 4px; padding: 12px 16px; margin-top: 8px; color: #dbdee1; font-size: 14px; line-height: 1.6; }}
.embed-title {{ font-size: 15px; font-weight: 700; color: #00a8fc; margin-bottom: 8px; }}
.separator {{ border: none; border-top: 1px solid #3f4147; margin: 8px 0; }}
.ai-text {{ color: #dbdee1; font-size: 13px; line-height: 1.6; }}
.highlight {{ color: #2ecc71; font-weight: 600; }}
.muted {{ color: #949ba4; font-size: 12px; }}
.buttons {{ display: flex; gap: 8px; margin-top: 12px; }}
.btn {{ background: #4f545c; color: #dbdee1; border: none; padding: 4px 16px; border-radius: 3px; font-size: 13px; font-family: inherit; cursor: pointer; display: flex; align-items: center; gap: 6px; }}
.btn:hover {{ background: #5d6269; }}
.footer {{ color: #949ba4; font-size: 12px; margin-top: 10px; }}
.timestamp {{ color: #949ba4; font-size: 12px; margin-left: 8px; font-weight: 400; }}
</style></head><body>
<div class="message">
    <!-- User message -->
    <div class="user-msg">
        <div class="avatar user-avatar">F</div>
        <div class="msg-content">
            <div class="username user-name">fred_at_disney<span class="timestamp">Today at {datetime.now().strftime("%-I:%M %p")}</span></div>
            <div class="msg-text"><span class="slash-cmd">/ask</span> What's the best day to visit Magic Kingdom this week?</div>
        </div>
    </div>
    
    <!-- Bot response -->
    <div class="user-msg">
        <div class="avatar bot-avatar">🎢</div>
        <div class="msg-content">
            <div class="username bot-name">TPCR Bot<span class="bot-tag">BOT</span><span class="timestamp">Today at {datetime.now().strftime("%-I:%M %p")}</span></div>
            <div class="embed">
                <div class="embed-title">🤖 Ask — Magic Kingdom This Week</div>
                
                <div class="ai-text">
                    Based on our crowd forecasts, <span class="highlight">{best_day_name}</span> is your best bet this week with a <span class="highlight">WTI of {best_wti_val}</span> — that means shorter queues across the board.
                </div>
                
                <hr class="separator">
                
                <div class="ai-text">
                    📊 <b>Quick breakdown:</b><br>
                    ▸ Best: <span class="highlight">{best_day_name} (WTI {best_wti_val})</span> — expect 15-25 min headliner waits<br>
                    ▸ Worst: {worst_day_name} (WTI {worst_wti_val}) — could see 60+ min peaks<br>
                    ▸ Today: WTI {mk_wti:.0f} — {wti_label(mk_wti).lower()} crowds
                </div>
                
                <hr class="separator">
                
                <div class="ai-text">
                    💡 <b>Tips for {best_day_name}:</b><br>
                    ▸ Arrive at rope drop for TRON & 7 Dwarfs<br>
                    ▸ Hit Tiana's Bayou Adventure before noon<br>
                    ▸ Save Space Mountain for evening when waits dip
                </div>
                
                <div class="footer">Powered by crowd forecasts • hazeydata.ai</div>
                
                <div class="buttons">
                    <button class="btn">👍 Helpful</button>
                    <button class="btn">👎 Not helpful</button>
                </div>
            </div>
        </div>
    </div>
</div>
</body></html>"""


def capture_screenshot(html_content: str, output_path: str, width: int = 580, selector: str = ".embed"):
    """Render HTML and capture screenshot with Playwright."""
    from playwright.sync_api import sync_playwright

    tmp_html = "/tmp/embed_screenshot.html"
    with open(tmp_html, "w") as f:
        f.write(html_content)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 1200})
        page.goto(f"file://{tmp_html}")
        page.wait_for_timeout(500)

        # Get the target element's bounding box for tight crop
        el = page.query_selector(selector)
        if el:
            box = el.bounding_box()
            padding = 20
            page.screenshot(
                path=output_path,
                clip={
                    "x": max(0, box["x"] - padding),
                    "y": max(0, box["y"] - padding),
                    "width": box["width"] + padding * 2,
                    "height": box["height"] + padding * 2
                }
            )
        else:
            page.screenshot(path=output_path, full_page=True)

        browser.close()
    print(f"  ✅ Saved: {output_path}")


if __name__ == "__main__":
    os.makedirs(ASSETS, exist_ok=True)

    # Pull live data (with fallback)
    print("📡 Fetching live data from DuckDB...")
    data = get_live_data()
    if data is None:
        print("📋 Using fallback hardcoded data")
        data = get_fallback_data()
    else:
        print("  ✅ Live data loaded")

    print("\nGenerating /today screenshot...")
    capture_screenshot(create_today_html(data), str(ASSETS / "screenshot-today.png"))

    print("Generating /now screenshot...")
    capture_screenshot(create_now_html(data), str(ASSETS / "screenshot-now.png"))

    print("Generating /crowd screenshot...")
    capture_screenshot(create_crowd_html(data), str(ASSETS / "screenshot-crowd.png"))

    print("Generating /best-day screenshot...")
    capture_screenshot(create_bestday_html(data), str(ASSETS / "screenshot-bestday.png"))

    print("Generating /ask screenshot...")
    capture_screenshot(create_ask_html(data), str(ASSETS / "screenshot-ask.png"), width=620, selector=".message")

    print(f"\n🎉 Done! All 5 screenshots saved to {ASSETS}")

    # Auto-commit to hazeydata.ai repo
    site_repo = ASSETS.parent
    try:
        subprocess.run(["git", "add", "assets/screenshot-*.png"], cwd=site_repo, check=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=site_repo)
        if result.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update bot screenshots with live data"], cwd=site_repo, check=True)
            # Prefer token helper (W6) — falls back to plain push if helper missing
            helper = Path.home() / "bin" / "git-push-with-token.sh"
            if helper.is_file():
                subprocess.run(["bash", str(helper), str(site_repo), "origin", "HEAD"], check=True)
            else:
                subprocess.run(["git", "push"], cwd=site_repo, check=True)
            print("📤 Pushed updated screenshots to hazeydata.ai")
        else:
            print("📌 No screenshot changes to push")
    except Exception as e:
        print(f"⚠️ Git push failed: {e}")
