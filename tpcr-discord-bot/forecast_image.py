"""
iOS Weather-style crowd forecast image generator.
- 7-day: horizontal bar chart (weather app style)
- 30-day: calendar heat map grid (weeks as columns)
2x resolution for crisp Discord embeds.
"""

from PIL import Image, ImageDraw, ImageFont
from datetime import date, timedelta
from io import BytesIO
from collections import defaultdict

from ask_agent import label_suffix

# Benedictus color palette — median-anchored, data-driven
# p1=8 (deep blue) → median=22 (lavender/white) → p99=50 (deep red)
# Blue compressed (good days are rare), pink expanded (where decisions happen)
BENEDICTUS_STOPS = [
    (8,   (10, 47, 143)),     # p1: Deep blue — shortest waits
    (15,  (60, 120, 210)),    # ~p16: Blue — notably light
    (22,  (210, 200, 220)),   # median: Lavender-white — normal Disney
    (28,  (255, 177, 201)),   # ~p47: Light pink — busier
    (34,  (245, 120, 160)),   # ~p60: Medium pink — busy
    (40,  (235, 66, 123)),    # ~p76: Hot pink — very busy
    (50,  (166, 0, 56)),      # p99: Deep red — avoid if you can
    (65,  (80, 0, 30)),       # Extreme: near-black crimson
]

# Discord dark theme
BG_COLOR = (43, 45, 49)
TEXT_COLOR = (255, 255, 255)
TEXT_DIM = (148, 155, 164)
DIVIDER_COLOR = (55, 57, 63)

# Scale for 2x resolution
SCALE = 2

# Fonts
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def wti_to_color(wti: float) -> tuple:
    """Interpolate Benedictus color for a WTI value."""
    if wti <= BENEDICTUS_STOPS[0][0]:
        return BENEDICTUS_STOPS[0][1]
    if wti >= BENEDICTUS_STOPS[-1][0]:
        return BENEDICTUS_STOPS[-1][1]
    for i in range(len(BENEDICTUS_STOPS) - 1):
        w0, c0 = BENEDICTUS_STOPS[i]
        w1, c1 = BENEDICTUS_STOPS[i + 1]
        if w0 <= wti <= w1:
            t = (wti - w0) / (w1 - w0) if w1 != w0 else 0
            return tuple(int(c0[j] + t * (c1[j] - c0[j])) for j in range(3))
    return BENEDICTUS_STOPS[-1][1]


def wti_label(wti: float, park_code: str = "", as_of=None) -> str:
    """Suffix like " (normal)" from the park's /ask normal band, or "" when no band (or no park_code)."""
    return label_suffix(wti, park_code, as_of)


def draw_rounded_rect(draw, xy, radius, fill):
    x0, y0, x1, y1 = xy
    radius = min(radius, (y1 - y0) // 2, (x1 - x0) // 2)
    if radius <= 0:
        draw.rectangle(xy, fill=fill)
        return
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.pieslice([x0, y0, x0 + 2*radius, y0 + 2*radius], 180, 270, fill=fill)
    draw.pieslice([x1 - 2*radius, y0, x1, y0 + 2*radius], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2*radius, x0 + 2*radius, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2*radius, y1 - 2*radius, x1, y1], 0, 90, fill=fill)


def draw_gradient_bar(draw, x0, y0, x1, y1, wti_low, wti_high, global_min, global_max, radius):
    bar_width = x1 - x0
    if global_max == global_min:
        px_start, px_end = x0, x1
    else:
        px_start = x0 + int((wti_low - global_min) / (global_max - global_min) * bar_width)
        px_end = x0 + int((wti_high - global_min) / (global_max - global_min) * bar_width)
    min_bar = 30 * SCALE
    if px_end - px_start < min_bar:
        mid = (px_start + px_end) // 2
        px_start, px_end = mid - min_bar // 2, mid + min_bar // 2
    
    # Background pill (empty track)
    draw_rounded_rect(draw, (x0, y0, x1, y1), radius, fill=(60, 63, 69))
    
    # Draw colored gradient as a clean pill shape
    # First, fill the gradient region with vertical color lines (full height)
    for px in range(max(px_start, x0), min(px_end, x1)):
        wti_val = global_min + (px - x0) / bar_width * (global_max - global_min) if global_max != global_min else (global_min + global_max) / 2
        draw.line([(px, y0), (px, y1)], fill=wti_to_color(wti_val))
    
    # Round the ends with pill caps that match the gradient colors
    cap_r = min(radius, (y1 - y0) // 2)
    if px_end - px_start >= 2 * cap_r and cap_r > 0:
        # Left cap: clear the square corner and draw rounded
        draw.rectangle([px_start, y0, px_start + cap_r, y1], fill=(60, 63, 69))
        draw.pieslice([px_start, y0, px_start + 2*cap_r, y1], 90, 270, fill=wti_to_color(wti_low))
        # Right cap: clear the square corner and draw rounded
        draw.rectangle([px_end - cap_r, y0, px_end, y1], fill=(60, 63, 69))
        draw.pieslice([px_end - 2*cap_r, y0, px_end, y1], 270, 90, fill=wti_to_color(wti_high))


def draw_avg_dot(draw, x0, x1, y_center, wti_avg, global_min, global_max):
    dot_r = 8 * SCALE
    bar_width = x1 - x0
    if global_max == global_min:
        px = (x0 + x1) // 2
    else:
        px = x0 + int((wti_avg - global_min) / (global_max - global_min) * bar_width)
    px = max(x0 + dot_r, min(px, x1 - dot_r))
    draw.ellipse([px - dot_r, y_center - dot_r, px + dot_r, y_center + dot_r], fill=(255, 255, 255))
    inner_r = dot_r - 3 * SCALE
    draw.ellipse([px - inner_r, y_center - inner_r, px + inner_r, y_center + inner_r], fill=wti_to_color(wti_avg))


# =========================================================================
# 7-DAY VIEW — horizontal bars (iOS weather style)
# =========================================================================
def generate_7day_image(park_name: str, days_data: list) -> BytesIO:
    n_rows = len(days_data)
    # Tighter rows for longer lists
    if n_rows > 14:
        row_h = 36 * SCALE
    elif n_rows > 7:
        row_h = 44 * SCALE
    else:
        row_h = 52 * SCALE
    pad_x = 24 * SCALE
    pad_y = 20 * SCALE
    header_h = 48 * SCALE
    label_h = 28 * SCALE
    bar_left = 220 * SCALE
    bar_right = 440 * SCALE
    bar_h = 14 * SCALE
    bar_r = 7 * SCALE
    img_w = 580 * SCALE
    
    img_height = pad_y + header_h + label_h + (row_h * n_rows) + pad_y
    img = Image.new("RGB", (img_w, img_height), BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    font = ImageFont.truetype(FONT_PATH, 18 * SCALE)
    font_sm = ImageFont.truetype(FONT_PATH, 15 * SCALE)
    font_bold = ImageFont.truetype(FONT_BOLD_PATH, 18 * SCALE)
    font_header = ImageFont.truetype(FONT_BOLD_PATH, 22 * SCALE)
    font_label = ImageFont.truetype(FONT_PATH, 12 * SCALE)
    
    all_lows = [d["wti_low"] for d in days_data]
    all_highs = [d["wti_high"] for d in days_data]
    global_min = min(all_lows) - 3
    global_max = max(all_highs) + 3
    
    y = pad_y
    draw.text((pad_x, y), park_name, fill=TEXT_COLOR, font=font_header)
    y += header_h
    draw.text((bar_left - 40 * SCALE, y), "Low", fill=TEXT_DIM, font=font_label)
    draw.text((bar_right + 12 * SCALE, y), "High", fill=TEXT_DIM, font=font_label)
    y += label_h
    
    today = date.today()
    for i, day in enumerate(days_data):
        ry = y + (i * row_h)
        text_y = ry + (row_h - 18 * SCALE) // 2
        if i > 0:
            draw.line([(pad_x, ry), (img_w - pad_x, ry)], fill=DIVIDER_COLOR, width=SCALE)
        
        d = day["date"]
        day_str = "Today" if d == today else d.strftime("%a")
        day_font = font_bold if d == today else font
        draw.text((pad_x, text_y), day_str, fill=TEXT_COLOR, font=day_font)
        draw.text((pad_x + 65 * SCALE, text_y + 2 * SCALE), d.strftime("%b %d"), fill=TEXT_DIM, font=font_sm)
        
        # Color dot
        dot_x, dot_y = pad_x + 140 * SCALE, ry + row_h // 2
        dot_r = 6 * SCALE
        draw.ellipse([dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r], fill=wti_to_color(day["wti_avg"]))
        
        draw.text((bar_left - 38 * SCALE, text_y), f"{day['wti_low']:.0f}", fill=TEXT_DIM, font=font_sm)
        
        by = ry + (row_h - bar_h) // 2
        draw_gradient_bar(draw, bar_left, by, bar_right, by + bar_h, day["wti_low"], day["wti_high"], global_min, global_max, bar_r)
        draw_avg_dot(draw, bar_left, bar_right, by + bar_h // 2, day["wti_avg"], global_min, global_max)
        
        draw.text((bar_right + 12 * SCALE, text_y), f"{day['wti_high']:.0f}", fill=TEXT_COLOR, font=font_sm)
    
    # Best day indicators — arrows on the right side
    # For 7-day: highlight top 1; for 8-30 day: highlight top 3
    n_best = 1 if n_rows <= 7 else 3
    sorted_by_avg = sorted(enumerate(days_data), key=lambda x: x[1]["wti_avg"])
    best_indices = set(idx for idx, _ in sorted_by_avg[:n_best])
    
    arrow_x = bar_right + 58 * SCALE
    font_arrow = ImageFont.truetype(FONT_BOLD_PATH, 13 * SCALE)
    
    for rank, (idx, day) in enumerate(sorted_by_avg[:n_best]):
        ry = y + (idx * row_h)
        text_y_arrow = ry + (row_h - 14 * SCALE) // 2
        
        # Draw a small "← Best" or "← #1" indicator
        if n_best == 1:
            label = "◀ Best"
            label_color = (45, 200, 120)  # Green
        else:
            label = f"◀ #{rank + 1}"
            label_color = (45, 200, 120) if rank == 0 else (100, 180, 140)
        
        draw.text((arrow_x, text_y_arrow), label, fill=label_color, font=font_arrow)
    
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# =========================================================================
# 30-DAY VIEW — calendar heat map grid (standard layout)
# Columns = Mon-Sun, Rows = weeks (reading top-to-bottom like a calendar)
# =========================================================================
def generate_calendar_image(park_name: str, days_data: list) -> BytesIO:
    """Calendar grid: columns = days of week, rows = weeks."""
    
    pad_x = 28 * SCALE
    pad_y = 20 * SCALE
    header_h = 48 * SCALE
    cell_size = 64 * SCALE
    cell_gap = 6 * SCALE
    day_header_h = 32 * SCALE  # day names row
    week_label_w = 0  # no row labels needed
    legend_h = 50 * SCALE
    corner_r = 8 * SCALE
    
    font_header = ImageFont.truetype(FONT_BOLD_PATH, 22 * SCALE)
    font_day_label = ImageFont.truetype(FONT_BOLD_PATH, 13 * SCALE)
    font_cell_num = ImageFont.truetype(FONT_BOLD_PATH, 16 * SCALE)
    font_cell_wti = ImageFont.truetype(FONT_PATH, 11 * SCALE)
    font_legend = ImageFont.truetype(FONT_PATH, 10 * SCALE)
    
    # Build lookup: date -> day data
    data_by_date = {d["date"]: d for d in days_data}
    
    # Find date range
    all_dates = sorted(d["date"] for d in days_data)
    first_date = all_dates[0]
    last_date = all_dates[-1]
    
    # Align to start of week (Monday)
    week_start = first_date - timedelta(days=first_date.weekday())
    
    # Build weeks (each week = Monday start date)
    weeks = []
    current = week_start
    while current <= last_date:
        weeks.append(current)
        current += timedelta(days=7)
    
    n_weeks = len(weeks)
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    
    # Image dimensions: 7 columns for days, n_weeks rows for weeks
    grid_w = 7 * (cell_size + cell_gap) - cell_gap
    img_w = pad_x * 2 + grid_w
    img_h = pad_y + header_h + day_header_h + n_weeks * (cell_size + cell_gap) + legend_h + pad_y
    
    img = Image.new("RGB", (img_w, img_h), BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    # Header
    draw.text((pad_x, pad_y), park_name, fill=TEXT_COLOR, font=font_header)
    
    # Day name column headers (Mon Tue Wed Thu Fri Sat Sun)
    grid_y = pad_y + header_h
    for col, day_name in enumerate(day_names):
        x = pad_x + col * (cell_size + cell_gap)
        bbox = draw.textbbox((0, 0), day_name, font=font_day_label)
        tw = bbox[2] - bbox[0]
        draw.text((x + (cell_size - tw) // 2, grid_y), day_name, fill=TEXT_DIM, font=font_day_label)
    
    grid_y += day_header_h
    
    # Find best day
    today = date.today()
    best_day = min(days_data, key=lambda d: d["wti_avg"])
    
    # Week rows
    prev_month = None
    for wi, ws in enumerate(weeks):
        ry = grid_y + wi * (cell_size + cell_gap)
        
        for col in range(7):
            cell_date = ws + timedelta(days=col)
            cx = pad_x + col * (cell_size + cell_gap)
            cy = ry
            
            if cell_date in data_by_date:
                d = data_by_date[cell_date]
                color = wti_to_color(d["wti_avg"])
                
                # Cell background
                draw_rounded_rect(draw, (cx, cy, cx + cell_size, cy + cell_size), corner_r, fill=color)
                
                # Text color based on background brightness
                brightness = color[0] * 0.299 + color[1] * 0.587 + color[2] * 0.114
                txt_color = (0, 0, 0) if brightness > 150 else (255, 255, 255)
                dim_color = (40, 40, 40) if brightness > 150 else (200, 200, 200)
                
                # Date number — show "Mon 17" on 1st of month or first cell, else just number
                if cell_date.day == 1:
                    num_str = cell_date.strftime("%b")
                    bbox = draw.textbbox((0, 0), num_str, font=font_cell_wti)
                    tw = bbox[2] - bbox[0]
                    draw.text((cx + (cell_size - tw) // 2, cy + 4 * SCALE), num_str, fill=dim_color, font=font_cell_wti)
                    
                    num_str = "1"
                    bbox = draw.textbbox((0, 0), num_str, font=font_cell_num)
                    tw = bbox[2] - bbox[0]
                    draw.text((cx + (cell_size - tw) // 2, cy + 16 * SCALE), num_str, fill=txt_color, font=font_cell_num)
                else:
                    num_str = str(cell_date.day)
                    bbox = draw.textbbox((0, 0), num_str, font=font_cell_num)
                    tw = bbox[2] - bbox[0]
                    draw.text((cx + (cell_size - tw) // 2, cy + 10 * SCALE), num_str, fill=txt_color, font=font_cell_num)
                
                # WTI number (bottom)
                wti_str = f"{d['wti_avg']:.0f}"
                bbox = draw.textbbox((0, 0), wti_str, font=font_cell_wti)
                tw = bbox[2] - bbox[0]
                draw.text((cx + (cell_size - tw) // 2, cy + 38 * SCALE), wti_str, fill=dim_color, font=font_cell_wti)
                
                # Star for best day
                if cell_date == best_day["date"]:
                    star_str = "★"
                    bbox = draw.textbbox((0, 0), star_str, font=font_cell_wti)
                    sw = bbox[2] - bbox[0]
                    draw.text((cx + cell_size - sw - 4 * SCALE, cy + 2 * SCALE), star_str, fill=txt_color, font=font_cell_wti)
                
                # Today indicator (white border)
                if cell_date == today:
                    for offset in range(2 * SCALE):
                        draw.rounded_rectangle(
                            [cx + offset, cy + offset, cx + cell_size - offset, cy + cell_size - offset],
                            radius=corner_r, outline=(255, 255, 255), width=1
                        )
            else:
                # Empty cell
                draw_rounded_rect(draw, (cx, cy, cx + cell_size, cy + cell_size), corner_r, fill=(50, 52, 56))
                # Show date number dimmed if it's a real date (just outside data range)
                if date(2020, 1, 1) <= cell_date <= date(2030, 12, 31):
                    num_str = str(cell_date.day)
                    bbox = draw.textbbox((0, 0), num_str, font=font_cell_num)
                    tw = bbox[2] - bbox[0]
                    draw.text((cx + (cell_size - tw) // 2, cy + 10 * SCALE), num_str, fill=(70, 73, 78), font=font_cell_num)
    
    # Legend bar at bottom
    legend_y = grid_y + n_weeks * (cell_size + cell_gap) + 10 * SCALE
    legend_x = pad_x
    legend_w = grid_w
    legend_bar_h = 12 * SCALE
    
    draw.text((legend_x, legend_y - 16 * SCALE), "Low wait times", fill=TEXT_DIM, font=font_legend)
    bbox = draw.textbbox((0, 0), "High wait times", font=font_legend)
    tw = bbox[2] - bbox[0]
    draw.text((legend_x + legend_w - tw, legend_y - 16 * SCALE), "High wait times", fill=TEXT_DIM, font=font_legend)
    
    for px in range(legend_w):
        wti = 10 + (px / legend_w) * 60
        color = wti_to_color(wti)
        draw.line([(legend_x + px, legend_y), (legend_x + px, legend_y + legend_bar_h)], fill=color)
    
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# =========================================================================
# YEAR VIEW — GitHub-style heatmap (rotated: rows = Mon-Sun, cols = weeks)
# Compact enough for 365 days while staying readable on Discord
# =========================================================================
def wti_to_color_scaled(wti: float, wti_min: float, wti_max: float) -> tuple:
    """Map WTI to Benedictus color using the park's own range.
    wti_min maps to the cool end, wti_max to the hot end."""
    if wti_max == wti_min:
        return BENEDICTUS_STOPS[len(BENEDICTUS_STOPS) // 2][1]
    # Normalize to 0-1 within park's range
    t = (wti - wti_min) / (wti_max - wti_min)
    t = max(0.0, min(1.0, t))
    # Map to absolute WTI range that uses full Benedictus gradient (8-65)
    mapped_wti = 8 + t * (65 - 8)
    return wti_to_color(mapped_wti)


def generate_year_image(park_name: str, days_data: list, park_code: str = "", as_of=None) -> BytesIO:
    """GitHub contributions-style heatmap: 7 rows (days) × ~52 cols (weeks).
    Colors scaled to the park's own WTI range for maximum contrast."""

    pad_x = 28 * SCALE
    pad_y = 20 * SCALE
    header_h = 56 * SCALE
    cell_size = 16 * SCALE
    cell_gap = 3 * SCALE
    day_label_w = 40 * SCALE
    month_header_h = 22 * SCALE
    legend_h = 50 * SCALE
    best_section_h = 80 * SCALE
    corner_r = 3 * SCALE

    font_header = ImageFont.truetype(FONT_BOLD_PATH, 22 * SCALE)
    font_sub = ImageFont.truetype(FONT_PATH, 13 * SCALE)
    font_day_label = ImageFont.truetype(FONT_PATH, 10 * SCALE)
    font_month = ImageFont.truetype(FONT_BOLD_PATH, 10 * SCALE)
    font_legend = ImageFont.truetype(FONT_PATH, 10 * SCALE)
    font_best = ImageFont.truetype(FONT_BOLD_PATH, 14 * SCALE)
    font_best_detail = ImageFont.truetype(FONT_PATH, 12 * SCALE)

    data_by_date = {d["date"]: d for d in days_data}
    all_dates = sorted(d["date"] for d in days_data)
    first_date = all_dates[0]
    last_date = all_dates[-1]

    # Align to Monday
    week_start = first_date - timedelta(days=first_date.weekday())

    # Build week columns
    weeks = []
    current = week_start
    while current <= last_date:
        weeks.append(current)
        current += timedelta(days=7)

    n_weeks = len(weeks)
    day_names_short = ["M", "", "W", "", "F", "", "S"]

    # Image dimensions
    grid_w = n_weeks * (cell_size + cell_gap) - cell_gap
    img_w = pad_x * 2 + day_label_w + grid_w
    img_h = pad_y + header_h + month_header_h + 7 * (cell_size + cell_gap) + legend_h + best_section_h + pad_y

    img = Image.new("RGB", (img_w, img_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header
    y = pad_y
    draw.text((pad_x, y), park_name, fill=TEXT_COLOR, font=font_header)
    y += 30 * SCALE

    # Subtitle with date range
    range_str = f"{first_date.strftime('%b %Y')} – {last_date.strftime('%b %Y')}"
    draw.text((pad_x, y), range_str, fill=TEXT_DIM, font=font_sub)
    y = pad_y + header_h

    # Month labels across the top
    grid_x0 = pad_x + day_label_w
    prev_month = None
    for wi, ws in enumerate(weeks):
        # Label the first week of each month
        mid_date = ws + timedelta(days=3)  # mid-week for labeling
        if mid_date.month != prev_month:
            cx = grid_x0 + wi * (cell_size + cell_gap)
            month_str = mid_date.strftime("%b")
            draw.text((cx, y), month_str, fill=TEXT_DIM, font=font_month)
            prev_month = mid_date.month

    y += month_header_h

    # Day labels (M, W, F on left)
    for row in range(7):
        if day_names_short[row]:
            ly = y + row * (cell_size + cell_gap) + (cell_size - 10 * SCALE) // 2
            draw.text((pad_x, ly), day_names_short[row], fill=TEXT_DIM, font=font_day_label)

    # Compute park-specific WTI range for color scaling
    all_avgs = [d["wti_avg"] for d in days_data]
    park_wti_min = min(all_avgs)
    park_wti_max = max(all_avgs)
    # Add small padding so extremes aren't right at the edge
    wti_pad = (park_wti_max - park_wti_min) * 0.05
    park_wti_min -= wti_pad
    park_wti_max += wti_pad

    # Heatmap cells
    today = date.today()
    best_day = min(days_data, key=lambda d: d["wti_avg"])
    # Top 5 best days
    sorted_days = sorted(days_data, key=lambda d: d["wti_avg"])
    top5 = sorted_days[:5]

    for wi, ws in enumerate(weeks):
        for row in range(7):
            cell_date = ws + timedelta(days=row)
            cx = grid_x0 + wi * (cell_size + cell_gap)
            cy = y + row * (cell_size + cell_gap)

            if cell_date in data_by_date:
                d = data_by_date[cell_date]
                color = wti_to_color_scaled(d["wti_avg"], park_wti_min, park_wti_max)
                draw_rounded_rect(draw, (cx, cy, cx + cell_size, cy + cell_size), corner_r, fill=color)

                # Today: white border
                if cell_date == today:
                    for offset in range(2 * SCALE):
                        draw.rounded_rectangle(
                            [cx + offset, cy + offset, cx + cell_size - offset, cy + cell_size - offset],
                            radius=corner_r, outline=(255, 255, 255), width=1
                        )
            else:
                draw_rounded_rect(draw, (cx, cy, cx + cell_size, cy + cell_size), corner_r, fill=(50, 52, 56))

    # Legend bar — scaled to park's actual WTI range with numbers
    legend_y = y + 7 * (cell_size + cell_gap) + 12 * SCALE
    legend_x = grid_x0
    legend_w = min(grid_w, 300 * SCALE)

    low_label = f"WTI {park_wti_min + wti_pad:.0f}"
    high_label = f"WTI {park_wti_max - wti_pad:.0f}"
    draw.text((legend_x, legend_y), low_label, fill=TEXT_DIM, font=font_legend)
    bbox_high = draw.textbbox((0, 0), high_label, font=font_legend)
    tw_high = bbox_high[2] - bbox_high[0]
    draw.text((legend_x + legend_w - tw_high, legend_y), high_label, fill=TEXT_DIM, font=font_legend)

    bar_y = legend_y + 16 * SCALE
    bar_h = 10 * SCALE
    for px in range(int(legend_w)):
        t = px / legend_w
        wti = park_wti_min + t * (park_wti_max - park_wti_min)
        color = wti_to_color_scaled(wti, park_wti_min, park_wti_max)
        draw.line([(legend_x + px, bar_y), (legend_x + px, bar_y + bar_h)], fill=color)

    # Best days section
    best_y = bar_y + bar_h + 20 * SCALE
    draw.text((pad_x, best_y), "🏆 Best days to visit", fill=TEXT_COLOR, font=font_best)
    best_y += 22 * SCALE

    for i, d in enumerate(top5):
        rank_color = (45, 200, 120) if i == 0 else (100, 180, 140)
        dot_color = wti_to_color_scaled(d["wti_avg"], park_wti_min, park_wti_max)

        bx = pad_x
        by = best_y + i * (18 * SCALE)

        # Color dot
        dot_r = 5 * SCALE
        draw.ellipse([bx, by + 2 * SCALE, bx + dot_r * 2, by + 2 * SCALE + dot_r * 2], fill=dot_color)

        day_str = d["date"].strftime("%a %b %d")
        wti_str = f"WTI {d['wti_avg']:.0f}"
        label_str = wti_label(d["wti_avg"], park_code, as_of)

        draw.text((bx + 16 * SCALE, by), f"{day_str}  —  {wti_str}{label_str}", fill=rank_color, font=font_best_detail)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# =========================================================================
# PUBLIC API
# =========================================================================
def generate_forecast_image(park_name: str, days_data: list, park_code: str = "", as_of=None) -> BytesIO:
    """Route to the right visualization based on data length.

    park_code enables the band label ("below normal" / "normal" / "above normal") on the
    year image; without it (or without a band) the label is omitted."""
    n = len(days_data)
    if n > 90:
        return generate_year_image(park_name, days_data, park_code, as_of)
    elif n > 14:
        return generate_calendar_image(park_name, days_data)
    else:
        return generate_7day_image(park_name, days_data)


if __name__ == "__main__":
    today = date.today()
    
    # 7-day test (varied data)
    sample_7 = [
        {"date": today + timedelta(days=i), 
         "wti_low": 15 + i * 2, 
         "wti_avg": 22 + i * 3,
         "wti_high": 30 + i * 4}
        for i in range(1, 8)
    ]
    buf = generate_7day_image("Magic Kingdom", sample_7)
    with open("/tmp/forecast_7day.png", "wb") as f:
        f.write(buf.read())
    print("Saved /tmp/forecast_7day.png")
    
    # 30-day test (weekly pattern)
    import math
    sample_30 = [
        {"date": today + timedelta(days=i),
         "wti_low": 15 + 8 * math.sin(i * 0.9),
         "wti_avg": 30 + 15 * math.sin(i * 0.9),
         "wti_high": 40 + 18 * math.sin(i * 0.9)}
        for i in range(1, 31)
    ]
    buf = generate_calendar_image("Magic Kingdom", sample_30)
    with open("/tmp/forecast_30day.png", "wb") as f:
        f.write(buf.read())
    print("Saved /tmp/forecast_30day.png")
