# Live "Now" Card — Design & Build Spec

> **Goal:** A beautiful, shareable image card showing current wait times with historical context. Designed for Discord (primary) and web (secondary). Every screenshot = free marketing.

## The Viral Loop
User runs `/now mk` → gets beautiful image → screenshots → posts to Reddit/X → drives traffic

## Card Layout (MK Example)

```
┌──────────────────────────────────────────┐
│  🏰 MAGIC KINGDOM — Right Now            │
│  Wednesday Feb 19 · 2:30 PM ET           │
│                                           │
│  ┌───────────────────────────────┐       │
│  │        [GAUGE CHART]          │       │
│  │   Semicircle, blue→pink/red   │       │
│  │   Needle at current position  │       │
│  │      "Moderately Busy"        │       │
│  │    Historical range below     │       │
│  └───────────────────────────────┘       │
│                                           │
│  🎢 HEADLINERS               NOW  │ AVG  │
│  ──────────────────────────────────────  │
│  TRON Lightcycle / Run       95↑  │  55  │
│  Seven Dwarfs Mine Train     80↑  │  62  │
│  Space Mountain              65↑  │  40  │
│  Big Thunder Mountain        45   │  40  │
│  Haunted Mansion             35   │  32  │
│  Peter Pan's Flight          50↑  │  30  │
│  Jungle Cruise               30↓  │  35  │
│  Pirates of the Caribbean    25   │  22  │
│                                           │
│  📈 42% above average for a Wednesday    │
│  ⚡ Spring Break: expect 15-30% higher   │
│                                           │
│  hazeydata.ai               powered by AI │
└──────────────────────────────────────────┘
```

### Visual Elements
- **Gauge:** Reuse stream dashboard gauge (Benedictus palette: blue→pink→red)
  - Source: `docs/stream/stream-dashboard.html` lines 1492-1670
  - Range: historical min/max WTI for this park on this day-type
  - Needle: current average wait across headliners
- **↑/↓ arrows:** Green ↓ when below avg, red ↑ when above avg, no arrow if within ±10%
- **AVG column:** Historical median for this entity + day-type + hour (from model_aggregates.parquet: wait_median for the entity, today's date_group_id and the 15-min clock slot)
- **Context line:** "X% above/below average for a [day-of-week]"
- **Alert line (conditional):** Spring break / holiday detection from date_group_id

### Color Scheme
- Dark background (#0d1117 or similar)
- Benedictus palette for gauge
- White text, muted gray for secondary
- Brand accent on hazeydata.ai

## Data Sources

### 1. Live Wait Times
- **Source:** queue-times.com API (already wired in `src/get_wait_times_from_queue_times.py`)
- **Endpoint:** `https://queue-times.com/parks/{park_id}/queue_times.json`
- **Fields needed:** entity_code, wait_time_minutes, last_updated
- **No API key required** (public API)

### 2. Historical "Typical" Values
- **Source:** `aggregates/model_aggregates.parquet` (rebuilt daily by S4). `posted_aggregates.parquet` is a dead v2 file and must not be used.
- **Schema (used columns):** entity_code, date_group_id, time_slot (15-min clock slot, 0-95 = hour*4 + minute//15), hour_of_day, wait_median, sample_count
- **Lookup:** entity_code + today's date_group_id + current 15-min slot → wait_median. Cells with fewer than 5 observations are absent (show no AVG, never 0).
- **This IS the "AVG" column** — what's typical for this ride, on this type of day, at this hour

### 3. Entity Names
- **Source:** `/mnt/data/pipeline/dimension_tables/dimentity.csv`
- **Columns:** code, name, fastpass_booth (exclude fastpass_booth=TRUE)

### 4. Date Group Classification
- **Source:** `/mnt/data/pipeline/dimension_tables/dimdate.csv`
- **Lookup:** today's date → date_group_id (e.g., "SPRING_BREAK_MAR_WEEK2_WED")

### 5. Park Hours
- **Source:** `/mnt/data/pipeline/dimension_tables/dimparkhours.csv`
- **Lookup:** park + today → opening_time, closing_time (for "park is currently open/closed")

### 6. WTI (for gauge)
- **Source:** `/mnt/data/pipeline/wti/wti.parquet`
- **Lookup:** park + today → today's WTI score (if available)
- **Historical range:** min/max WTI for this park + date_group pattern

## MK Headliner Entity Codes

| Code | Name |
|------|------|
| MK191 | TRON Lightcycle / Run |
| MK141 | Seven Dwarfs Mine Train |
| MK01 | Space Mountain |
| MK03 | Big Thunder Mountain Railroad |
| MK23 | The Haunted Mansion |
| MK05 | Peter Pan's Flight |
| MK13 | Jungle Cruise |
| MK16 | Pirates of the Caribbean |
| MK04 | Splash Mountain |
| MK06 | The Many Adventures of Winnie the Pooh |

## Queue-Times Park IDs

| Park | queue-times ID |
|------|---------------|
| MK | 7 |
| EP | 5 |
| HS | 6 |
| AK | 8 |
| DL | 16 |
| CA | 17 |
| UF | 64 |
| UH | 65 |

(See `src/get_wait_times_from_queue_times.py` for full mapping)

## Discord Command

```
/now [park]     — Live wait times card for park (default: MK)
/now mk         — Magic Kingdom right now
/now ep         — EPCOT right now
```

### Implementation
1. Fetch live waits from queue-times API
2. Look up historical medians for context
3. Render HTML template to PNG (Playwright/Puppeteer)
4. Return as Discord embed with image attachment
5. Cache for 5 minutes (don't hammer the API)

### Web Version
- Same HTML template, hosted at `hazeydata.ai/now/mk`
- Auto-refreshes every 5 minutes
- Meta tags for link preview (when shared on social media)

## Rendering Approach

**HTML → PNG** using Playwright (already available in our Python env):
1. HTML template with embedded CSS (self-contained)
2. Render at 800x600 or similar
3. Screenshot to PNG
4. Send as Discord attachment

Template location: `tpcr-discord-bot/templates/now_card.html`

## File Structure

```
tpcr-discord-bot/
  bot.py                  — Discord bot (update with /now command)
  templates/
    now_card.html         — Card HTML template
  renderers/
    now_card_renderer.py  — Fetch data + render to PNG
  data/
    headliners.json       — Headliner entity lists per park
```

## Priority
- **Phase 1:** Static card with live waits + historical avg (MVP)
- **Phase 2:** Gauge chart integration
- **Phase 3:** Alert system ("TRON just hit 120 min — top 3% all-time!")
- **Phase 4:** Web version with auto-refresh
