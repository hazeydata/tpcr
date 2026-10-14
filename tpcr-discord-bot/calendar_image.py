import os
"""
Monthly calendar image generator for 90+ day /best-day views.
Renders HTML→PNG via Playwright with Benedictus color palette.

Multi-column grid layout:
- 3 months (90 days): 3 columns × 1 row — compact single strip
- 12+ months (365 days): 3 columns × N rows

Each month is a card with:
- Day number in top-left corner
- WTI as the central prominent number
- Background color from Benedictus gradient
- Best day highlighted with a star/border
"""

import io
from datetime import date, timedelta
from collections import defaultdict
from playwright.sync_api import sync_playwright

# Benedictus color stops for gradient interpolation
# (wti_threshold, hex_color)
BENEDICTUS_STOPS = [
    (0,   "#050A1E"),   # Below minimum — near black
    (8,   "#0A2F8F"),   # Deep blue — shortest waits
    (12,  "#0A2F8F"),   # Deep blue
    (15,  "#3C78D2"),   # Medium blue
    (18,  "#3C78D2"),   # Medium blue
    (22,  "#D2C8DC"),   # Lavender — typical day
    (25,  "#D2C8DC"),   # Lavender
    (30,  "#FFB1C9"),   # Pink — above average
    (34,  "#FFB1C9"),   # Pink
    (38,  "#EB427B"),   # Rose — long waits
    (42,  "#EB427B"),   # Rose
    (46,  "#A60038"),   # Deep red — very long waits
    (50,  "#A60038"),   # Deep red
    (55,  "#50001E"),   # Darkest red — extreme
    (65,  "#50001E"),   # Darkest red
]


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _rgb_to_hex(r, g, b) -> str:
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def _interpolate_color(wti: float) -> str:
    """Smooth gradient interpolation between Benedictus stops."""
    stops = BENEDICTUS_STOPS
    if wti <= stops[0][0]:
        return stops[0][1]
    if wti >= stops[-1][0]:
        return stops[-1][1]
    for i in range(len(stops) - 1):
        w0, c0 = stops[i]
        w1, c1 = stops[i + 1]
        if w0 <= wti <= w1:
            t = (wti - w0) / (w1 - w0) if w1 != w0 else 0
            rgb0 = _hex_to_rgb(c0)
            rgb1 = _hex_to_rgb(c1)
            r = rgb0[0] + t * (rgb1[0] - rgb0[0])
            g = rgb0[1] + t * (rgb1[1] - rgb0[1])
            b = rgb0[2] + t * (rgb1[2] - rgb0[2])
            return _rgb_to_hex(r, g, b)
    return stops[-1][1]


def _interpolate_color_bright(wti: float) -> str:
    """Brighter version of the gradient for use as text on dark backgrounds.
    Lifts dark colors so they're readable against #1a2332 cells."""
    rgb = _hex_to_rgb(_interpolate_color(wti))
    # Convert to HSL-ish: boost luminance so minimum brightness is ~140
    brightness = rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114
    min_brightness = 140
    if brightness < min_brightness and brightness > 0:
        # Scale up RGB proportionally, clamping at 255
        factor = min_brightness / brightness
        r = min(255, rgb[0] * factor)
        g = min(255, rgb[1] * factor)
        b = min(255, rgb[2] * factor)
        return _rgb_to_hex(r, g, b)
    elif brightness == 0:
        # Pure black — give it a visible blue
        return "#6699DD"
    return _rgb_to_hex(*rgb)


def _text_color(wti: float) -> str:
    """Return appropriate text color based on background brightness."""
    rgb = _hex_to_rgb(_interpolate_color(wti))
    brightness = rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114
    return "#ffffff" if brightness < 140 else "#0a1628"


def _text_color_muted(wti: float) -> str:
    """Return muted version of text color for day numbers."""
    rgb = _hex_to_rgb(_interpolate_color(wti))
    brightness = rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114
    return "rgba(255,255,255,0.55)" if brightness < 140 else "rgba(10,22,40,0.45)"


def _build_html(park_name: str, days_data: list[dict]) -> str:
    """Build self-contained HTML for the calendar image with multi-column grid."""
    
    # Group days by (year, month)
    months = defaultdict(list)
    data_by_date = {}
    for d in days_data:
        dt = d["date"]
        months[(dt.year, dt.month)].append(d)
        data_by_date[dt] = d
    
    # Find best day (lowest wti_avg)
    best_day = min(days_data, key=lambda d: d["wti_avg"])
    best_date = best_day["date"]
    
    # Find top 5 for the summary
    sorted_days = sorted(days_data, key=lambda d: d["wti_avg"])
    top5 = sorted_days[:5]
    
    # Date range
    all_dates = sorted(d["date"] for d in days_data)
    first_date = all_dates[0]
    last_date = all_dates[-1]
    
    # Determine layout: number of columns based on month count
    num_months = len(months)
    num_cols = 3  # Always 3 columns for readability
    
    # Width scales: 3-col layout is wider
    container_width = 960  # px - good for 3 columns
    
    # Month names
    MONTH_NAMES = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    
    # Build month cards HTML
    month_cards_html = ""
    sorted_months = sorted(months.keys())
    
    for year, month in sorted_months:
        month_name = f"{MONTH_NAMES[month]} {year}"
        
        # Find first day of month and what weekday it starts on (Monday=0)
        first_of_month = date(year, month, 1)
        start_weekday = first_of_month.weekday()  # 0=Monday
        
        # Find last day of month
        if month == 12:
            last_of_month = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_of_month = date(year, month + 1, 1) - timedelta(days=1)
        
        days_in_month = last_of_month.day
        
        # Build cells
        cells_html = ""
        
        # Empty cells for days before the 1st
        for _ in range(start_weekday):
            cells_html += '<div class="cell empty"></div>\n'
        
        # Day cells
        for day_num in range(1, days_in_month + 1):
            current_date = date(year, month, day_num)
            
            if current_date in data_by_date:
                d = data_by_date[current_date]
                wti = d["wti_avg"]
                bg_color = _interpolate_color(wti)
                txt_color = _text_color(wti)
                muted_color = _text_color_muted(wti)
                is_best = current_date == best_date
                wti_display = f"{wti:.0f}"
                
                best_class = " best-day" if is_best else ""
                best_indicator = '<div class="best-star">★</div>' if is_best else ""
                
                cells_html += f'''<div class="cell has-data{best_class}" style="background:{bg_color}; color:{txt_color};">
                    <div class="day-num" style="color:{muted_color};">{day_num}</div>
                    <div class="wti-num">{wti_display}</div>
                    {best_indicator}
                </div>\n'''
            else:
                # Day exists in month but outside forecast range
                cells_html += f'''<div class="cell no-data">
                    <div class="day-num-empty">{day_num}</div>
                </div>\n'''
        
        # Trailing empty cells to fill the last row
        total_cells = start_weekday + days_in_month
        trailing = (7 - total_cells % 7) % 7
        for _ in range(trailing):
            cells_html += '<div class="cell empty"></div>\n'
        
        month_cards_html += f'''
        <div class="month-card">
            <div class="month-header">{month_name}</div>
            <div class="weekday-headers">
                <div>M</div><div>T</div><div>W</div><div>T</div><div>F</div><div>S</div><div>S</div>
            </div>
            <div class="calendar-grid">
                {cells_html}
            </div>
        </div>
        '''
    
    # Build top-5 best days section
    top5_html = ""
    for i, d in enumerate(top5):
        dt = d["date"]
        wti = d["wti_avg"]
        bg = _interpolate_color(wti)
        day_str = dt.strftime("%a %b %d")
        rank = i + 1
        top5_html += f'''
        <div class="best-row">
            <div class="best-rank">#{rank}</div>
            <div class="best-dot" style="background:{bg};"></div>
            <div class="best-date">{day_str}</div>
            <div class="best-wti">WTI {wti:.0f}</div>
        </div>'''
    
    # Build gradient stops for the legend bar CSS
    gradient_stops = []
    wti_range_for_legend = [8, 15, 22, 30, 38, 46, 55]
    for i, wti_val in enumerate(wti_range_for_legend):
        pct = (i / (len(wti_range_for_legend) - 1)) * 100
        color = _interpolate_color(wti_val)
        gradient_stops.append(f"{color} {pct:.0f}%")
    gradient_css = ", ".join(gradient_stops)
    
    html = f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    background: #0a1628;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    padding: 0;
    margin: 0;
    width: {container_width}px;
}}
.container {{
    background: #0a1628;
    border-radius: 16px;
    padding: 24px 20px 20px 20px;
    width: {container_width}px;
}}
.park-title {{
    color: #ffffff;
    font-size: 18px;
    font-weight: 700;
    margin-bottom: 2px;
    letter-spacing: -0.3px;
}}
.subtitle {{
    color: rgba(255,255,255,0.45);
    font-size: 11px;
    font-weight: 400;
    margin-bottom: 18px;
}}

/* === Multi-column month grid === */
.months-grid {{
    display: grid;
    grid-template-columns: repeat({num_cols}, 1fr);
    gap: 16px 20px;
    margin-bottom: 16px;
}}

.month-card {{
    /* Each month is a self-contained card */
}}
.month-header {{
    color: #ffffff;
    font-size: 12px;
    font-weight: 700;
    margin-bottom: 5px;
    letter-spacing: -0.2px;
}}
.weekday-headers {{
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 2px;
    margin-bottom: 2px;
}}
.weekday-headers div {{
    text-align: center;
    color: rgba(255,255,255,0.3);
    font-size: 8px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    padding: 1px 0;
}}
.calendar-grid {{
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 2px;
}}
.cell {{
    aspect-ratio: 1 / 0.82;
    border-radius: 4px;
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 34px;
}}
.cell.empty {{
    background: transparent;
}}
.cell.no-data {{
    background: rgba(255,255,255,0.04);
}}
.cell.has-data {{
    transition: none;
}}
.cell.best-day {{
    box-shadow: 0 0 0 2px #FFD700, 0 0 6px rgba(255,215,0,0.3);
    z-index: 2;
}}
.day-num {{
    position: absolute;
    top: 2px;
    left: 3px;
    font-size: 8px;
    font-weight: 600;
    line-height: 1;
}}
.day-num-empty {{
    position: absolute;
    top: 2px;
    left: 3px;
    font-size: 8px;
    font-weight: 600;
    line-height: 1;
    color: rgba(255,255,255,0.15);
}}
.wti-num {{
    font-size: 13px;
    font-weight: 700;
    line-height: 1;
    margin-top: 3px;
}}
.best-star {{
    position: absolute;
    top: 1px;
    right: 2px;
    font-size: 8px;
    color: #FFD700;
    text-shadow: 0 1px 2px rgba(0,0,0,0.4);
}}

/* Best days section - full width */
.best-section {{
    margin-top: 8px;
    padding: 12px 16px;
    background: rgba(255,255,255,0.04);
    border-radius: 10px;
}}
.best-title {{
    color: #ffffff;
    font-size: 12px;
    font-weight: 700;
    margin-bottom: 8px;
}}
.best-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 5px;
}}
.best-row:last-child {{
    margin-bottom: 0;
}}
.best-rank {{
    color: rgba(255,255,255,0.4);
    font-size: 10px;
    font-weight: 600;
    width: 20px;
}}
.best-dot {{
    width: 10px;
    height: 10px;
    border-radius: 3px;
    flex-shrink: 0;
}}
.best-date {{
    color: rgba(255,255,255,0.85);
    font-size: 11px;
    font-weight: 600;
    flex: 1;
}}
.best-wti {{
    color: rgba(255,255,255,0.5);
    font-size: 10px;
    font-weight: 600;
}}

/* Legend */
.legend {{
    margin-top: 12px;
    display: flex;
    align-items: center;
    gap: 10px;
}}
.legend-label {{
    color: rgba(255,255,255,0.35);
    font-size: 9px;
    font-weight: 500;
    white-space: nowrap;
}}
.legend-bar {{
    flex: 1;
    height: 6px;
    border-radius: 3px;
    background: linear-gradient(to right, {gradient_css});
}}

/* Footer */
.footer {{
    text-align: center;
    margin-top: 10px;
    color: rgba(255,255,255,0.2);
    font-size: 9px;
    font-weight: 500;
    letter-spacing: 0.5px;
}}
</style>
</head>
<body>
<div class="container" id="capture">
    <div class="park-title">{park_name}</div>
    <div class="subtitle">{first_date.strftime("%b %d, %Y")} – {last_date.strftime("%b %d, %Y")} · {len(days_data)} days</div>
    
    <div class="months-grid">
        {month_cards_html}
    </div>
    
    <div class="best-section">
        <div class="best-title">🏆 Best Days to Visit</div>
        {top5_html}
    </div>
    
    <div class="legend">
        <div class="legend-label">Low wait times</div>
        <div class="legend-bar"></div>
        <div class="legend-label">High wait times</div>
    </div>
    
    <div class="footer">themeparkcrowdreport.com</div>
</div>
</body>
</html>'''
    
    return html


def _build_html_v2(park_name: str, days_data: list[dict]) -> str:
    """Build HTML for v2 calendar: neutral dark cells, colored WTI text."""
    
    # Group days by (year, month)
    months = defaultdict(list)
    data_by_date = {}
    for d in days_data:
        dt = d["date"]
        months[(dt.year, dt.month)].append(d)
        data_by_date[dt] = d
    
    # Find best day (lowest wti_avg)
    best_day = min(days_data, key=lambda d: d["wti_avg"])
    best_date = best_day["date"]
    
    # Find top 5 for the summary
    sorted_days = sorted(days_data, key=lambda d: d["wti_avg"])
    top5 = sorted_days[:5]
    
    # Date range
    all_dates = sorted(d["date"] for d in days_data)
    first_date = all_dates[0]
    last_date = all_dates[-1]
    
    num_months = len(months)
    num_cols = 3
    container_width = 960
    
    MONTH_NAMES = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    
    # Build month cards HTML
    month_cards_html = ""
    sorted_months = sorted(months.keys())
    
    for year, month in sorted_months:
        month_name = f"{MONTH_NAMES[month]} {year}"
        
        first_of_month = date(year, month, 1)
        start_weekday = first_of_month.weekday()
        
        if month == 12:
            last_of_month = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_of_month = date(year, month + 1, 1) - timedelta(days=1)
        
        days_in_month = last_of_month.day
        
        cells_html = ""
        
        for _ in range(start_weekday):
            cells_html += '<div class="cell empty"></div>\n'
        
        for day_num in range(1, days_in_month + 1):
            current_date = date(year, month, day_num)
            
            if current_date in data_by_date:
                d = data_by_date[current_date]
                wti = d["wti_avg"]
                wti_color = _interpolate_color_bright(wti)  # gradient color for TEXT (brightened)
                is_best = current_date == best_date
                wti_display = f"{wti:.0f}"
                
                best_class = " best-day" if is_best else ""
                best_indicator = '<div class="best-star">★</div>' if is_best else ""
                
                cells_html += f'''<div class="cell has-data{best_class}">
                    <div class="day-num">{day_num}</div>
                    <div class="wti-num" style="color:{wti_color};">{wti_display}</div>
                    {best_indicator}
                </div>\n'''
            else:
                cells_html += f'''<div class="cell no-data">
                    <div class="day-num-empty">{day_num}</div>
                </div>\n'''
        
        total_cells = start_weekday + days_in_month
        trailing = (7 - total_cells % 7) % 7
        for _ in range(trailing):
            cells_html += '<div class="cell empty"></div>\n'
        
        month_cards_html += f'''
        <div class="month-card">
            <div class="month-header">{month_name}</div>
            <div class="weekday-headers">
                <div>M</div><div>T</div><div>W</div><div>T</div><div>F</div><div>S</div><div>S</div>
            </div>
            <div class="calendar-grid">
                {cells_html}
            </div>
        </div>
        '''
    
    # Build top-5 best days section
    top5_html = ""
    for i, d in enumerate(top5):
        dt = d["date"]
        wti = d["wti_avg"]
        wti_color = _interpolate_color_bright(wti)
        day_str = dt.strftime("%a %b %d")
        rank = i + 1
        top5_html += f'''
        <div class="best-row">
            <div class="best-rank">#{rank}</div>
            <div class="best-dot" style="background:{wti_color};"></div>
            <div class="best-date">{day_str}</div>
            <div class="best-wti" style="color:{wti_color};">WTI {wti:.0f}</div>
        </div>'''
    
    # Gradient legend
    gradient_stops = []
    wti_range_for_legend = [8, 15, 22, 30, 38, 46, 55]
    for i, wti_val in enumerate(wti_range_for_legend):
        pct = (i / (len(wti_range_for_legend) - 1)) * 100
        color = _interpolate_color_bright(wti_val)
        gradient_stops.append(f"{color} {pct:.0f}%")
    gradient_css = ", ".join(gradient_stops)
    
    html = f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    background: #0a1628;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    padding: 0;
    margin: 0;
    width: {container_width}px;
}}
.container {{
    background: #0a1628;
    border-radius: 16px;
    padding: 24px 20px 20px 20px;
    width: {container_width}px;
}}
.park-title {{
    color: #ffffff;
    font-size: 18px;
    font-weight: 700;
    margin-bottom: 2px;
    letter-spacing: -0.3px;
}}
.subtitle {{
    color: rgba(255,255,255,0.45);
    font-size: 11px;
    font-weight: 400;
    margin-bottom: 18px;
}}
.months-grid {{
    display: grid;
    grid-template-columns: repeat({num_cols}, 1fr);
    gap: 16px 20px;
    margin-bottom: 16px;
}}
.month-card {{}}
.month-header {{
    color: #ffffff;
    font-size: 12px;
    font-weight: 700;
    margin-bottom: 5px;
    letter-spacing: -0.2px;
}}
.weekday-headers {{
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 2px;
    margin-bottom: 2px;
}}
.weekday-headers div {{
    text-align: center;
    color: rgba(255,255,255,0.3);
    font-size: 8px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    padding: 1px 0;
}}
.calendar-grid {{
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 2px;
}}
.cell {{
    aspect-ratio: 1 / 0.82;
    border-radius: 4px;
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 34px;
}}
.cell.empty {{
    background: transparent;
}}
.cell.no-data {{
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.04);
}}
.cell.has-data {{
    background: #1a2332;
    border: 1px solid rgba(255,255,255,0.06);
}}
.cell.best-day {{
    box-shadow: 0 0 0 2px #FFD700, 0 0 6px rgba(255,215,0,0.3);
    border-color: transparent;
    z-index: 2;
}}
.day-num {{
    position: absolute;
    top: 2px;
    left: 3px;
    font-size: 8px;
    font-weight: 600;
    line-height: 1;
    color: rgba(255,255,255,0.35);
}}
.day-num-empty {{
    position: absolute;
    top: 2px;
    left: 3px;
    font-size: 8px;
    font-weight: 600;
    line-height: 1;
    color: rgba(255,255,255,0.12);
}}
.wti-num {{
    font-size: 14px;
    font-weight: 800;
    line-height: 1;
    margin-top: 3px;
    text-shadow: 0 0 8px rgba(255,255,255,0.08);
}}
.best-star {{
    position: absolute;
    top: 1px;
    right: 2px;
    font-size: 8px;
    color: #FFD700;
    text-shadow: 0 1px 2px rgba(0,0,0,0.4);
}}

/* Best days section */
.best-section {{
    margin-top: 8px;
    padding: 12px 16px;
    background: rgba(255,255,255,0.04);
    border-radius: 10px;
}}
.best-title {{
    color: #ffffff;
    font-size: 12px;
    font-weight: 700;
    margin-bottom: 8px;
}}
.best-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 5px;
}}
.best-row:last-child {{
    margin-bottom: 0;
}}
.best-rank {{
    color: rgba(255,255,255,0.4);
    font-size: 10px;
    font-weight: 600;
    width: 20px;
}}
.best-dot {{
    width: 10px;
    height: 10px;
    border-radius: 3px;
    flex-shrink: 0;
}}
.best-date {{
    color: rgba(255,255,255,0.85);
    font-size: 11px;
    font-weight: 600;
    flex: 1;
}}
.best-wti {{
    font-size: 10px;
    font-weight: 700;
}}

/* Legend */
.legend {{
    margin-top: 12px;
    display: flex;
    align-items: center;
    gap: 10px;
}}
.legend-label {{
    color: rgba(255,255,255,0.35);
    font-size: 9px;
    font-weight: 500;
    white-space: nowrap;
}}
.legend-bar {{
    flex: 1;
    height: 6px;
    border-radius: 3px;
    background: linear-gradient(to right, {gradient_css});
}}

/* Footer */
.footer {{
    text-align: center;
    margin-top: 10px;
    color: rgba(255,255,255,0.2);
    font-size: 9px;
    font-weight: 500;
    letter-spacing: 0.5px;
}}
</style>
</head>
<body>
<div class="container" id="capture">
    <div class="park-title">{park_name}</div>
    <div class="subtitle">{first_date.strftime("%b %d, %Y")} – {last_date.strftime("%b %d, %Y")} · {len(days_data)} days</div>
    
    <div class="months-grid">
        {month_cards_html}
    </div>
    
    <div class="best-section">
        <div class="best-title">🏆 Best Days to Visit</div>
        {top5_html}
    </div>
    
    <div class="legend">
        <div class="legend-label">Low wait times</div>
        <div class="legend-bar"></div>
        <div class="legend-label">High wait times</div>
    </div>
    
    <div class="footer">themeparkcrowdreport.com</div>
</div>
</body>
</html>'''
    
    return html


def generate_calendar_image_v2(park_name: str, days_data: list[dict]) -> io.BytesIO:
    """
    Generate v2 calendar PNG: neutral dark cells with colored WTI text.
    Same layout as v1, but the gradient color is applied to the WTI number
    text rather than the cell background. Looks less 'Crayola', more refined.
    
    Args:
        park_name: Display name of the park
        days_data: List of dicts with keys: date, wti_low, wti_avg, wti_high
        
    Returns:
        BytesIO containing the PNG image data
    """
    html = _build_html_v2(park_name, days_data)
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1000, "height": 800},
            device_scale_factor=2,
        )
        page.set_content(html, wait_until="networkidle")
        page.wait_for_timeout(1500)
        
        container = page.query_selector("#capture")
        png_bytes = container.screenshot(type="png")
        browser.close()
    
    buf = io.BytesIO(png_bytes)
    buf.seek(0)
    return buf


def generate_calendar_image(park_name: str, days_data: list[dict]) -> io.BytesIO:
    """
    Generate a monthly calendar PNG image for 90+ day views.
    Multi-column layout: 3 months = 3 cols, 12+ months = 3 cols × N rows.
    
    Args:
        park_name: Display name of the park (e.g. "Animal Kingdom")
        days_data: List of dicts with keys: date, wti_low, wti_avg, wti_high
        
    Returns:
        BytesIO containing the PNG image data
    """
    html = _build_html(park_name, days_data)
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1000, "height": 800},
            device_scale_factor=2,
        )
        page.set_content(html, wait_until="networkidle")
        
        # Wait for Inter font to load
        page.wait_for_timeout(1500)
        
        # Get the actual height of the content
        container = page.query_selector("#capture")
        box = container.bounding_box()
        
        # Screenshot just the container
        png_bytes = container.screenshot(type="png")
        browser.close()
    
    buf = io.BytesIO(png_bytes)
    buf.seek(0)
    return buf


# =========================================================================
# CLI test / sample generation
# =========================================================================
if __name__ == "__main__":
    import duckdb
    from datetime import date, timedelta
    import os
    
    def fetch_data(park_code, days):
        """Fetch WTI forecast data, trying live DB then parquet fallback."""
        today = date.today()
        end = today + timedelta(days=days)
        
        try:
            con = duckdb.connect(os.path.expanduser("~/hazeydata/pipeline") + "/tpcr_live.duckdb", read_only=True)
            rows = con.execute('''
                SELECT park_date, wti 
                FROM wti 
                WHERE park_code = ? 
                  AND park_date >= ? 
                  AND park_date <= ?
                  AND source = 'forecast'
                ORDER BY park_date
            ''', [park_code, today, end]).fetchall()
            con.close()
        except Exception:
            # Fallback to parquet
            con = duckdb.connect()
            con.execute("CREATE VIEW wti AS SELECT * FROM read_parquet('/Users/fredhazelton/hazeydata/pipeline/wti/wti.parquet')")
            rows = con.execute('''
                SELECT park_date, wti 
                FROM wti 
                WHERE park_code = ? 
                  AND park_date >= ? 
                  AND park_date <= ?
                  AND source = 'forecast'
                ORDER BY park_date
            ''', [park_code, today, end]).fetchall()
            con.close()
        
        days_data = []
        for park_date, wti in rows:
            if hasattr(park_date, 'date'):
                park_date = park_date.date()
            days_data.append({
                "date": park_date,
                "wti_low": wti * 0.8,
                "wti_avg": wti,
                "wti_high": wti * 1.2,
            })
        return days_data
    
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    os.makedirs(samples_dir, exist_ok=True)
    
    # --- v1 samples (original, AK) ---
    print("Fetching AK 90-day forecast data...")
    data_90 = fetch_data('AK', 90)
    print(f"Got {len(data_90)} days, WTI range: {min(d['wti_avg'] for d in data_90):.1f} - {max(d['wti_avg'] for d in data_90):.1f}")
    
    print("Generating 90-day calendar image (v1)...")
    buf = generate_calendar_image("Animal Kingdom", data_90)
    output_path = os.path.join(samples_dir, "calendar_90day_sample.png")
    with open(output_path, "wb") as f:
        f.write(buf.read())
    print(f"Saved {output_path} ({os.path.getsize(output_path)} bytes)")
    
    print("\nFetching AK 365-day forecast data...")
    data_365 = fetch_data('AK', 365)
    print(f"Got {len(data_365)} days, WTI range: {min(d['wti_avg'] for d in data_365):.1f} - {max(d['wti_avg'] for d in data_365):.1f}")
    
    print("Generating 365-day calendar image (v1)...")
    buf = generate_calendar_image("Animal Kingdom", data_365)
    output_path = os.path.join(samples_dir, "calendar_365day_sample.png")
    with open(output_path, "wb") as f:
        f.write(buf.read())
    print(f"Saved {output_path} ({os.path.getsize(output_path)} bytes)")
    
    # --- v2 samples (neutral cells, colored text — DL) ---
    print("\n" + "="*60)
    print("V2: Neutral cells, colored WTI text")
    print("="*60)
    
    print("\nFetching DL 90-day forecast data...")
    dl_90 = fetch_data('DL', 90)
    print(f"Got {len(dl_90)} days, WTI range: {min(d['wti_avg'] for d in dl_90):.1f} - {max(d['wti_avg'] for d in dl_90):.1f}")
    
    print("Generating 90-day calendar image (v2)...")
    buf = generate_calendar_image_v2("Disneyland", dl_90)
    output_path = os.path.join(samples_dir, "calendar_90day_v2_sample.png")
    with open(output_path, "wb") as f:
        f.write(buf.read())
    print(f"Saved {output_path} ({os.path.getsize(output_path)} bytes)")
    
    print("\nFetching DL 365-day forecast data...")
    dl_365 = fetch_data('DL', 365)
    print(f"Got {len(dl_365)} days, WTI range: {min(d['wti_avg'] for d in dl_365):.1f} - {max(d['wti_avg'] for d in dl_365):.1f}")
    
    print("Generating 365-day calendar image (v2)...")
    buf = generate_calendar_image_v2("Disneyland", dl_365)
    output_path = os.path.join(samples_dir, "calendar_365day_v2_sample.png")
    with open(output_path, "wb") as f:
        f.write(buf.read())
    print(f"Saved {output_path} ({os.path.getsize(output_path)} bytes)")
    
    print("\nDone! Compare v1 (colored backgrounds) vs v2 (neutral cells, colored text).")
