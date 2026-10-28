# V4 Amendment 001: Content Pipeline (Step 14) + Quality Gate

**Version:** 1.0
**Date:** 2026-03-25
**Authors:** Barney (architect) + Fred (decision-maker)
**Status:** APPROVED by Fred 2026-03-25
**Amends:** `PIPELINE_V4_DESIGN.md` v1.0 APPROVED (2026-03-21)

---

## Summary of Changes

1. **Add Step 14 (`s14_content.py`)** — Content generation for Twitter WTI tweets
2. **Add Quality Gate** — Pre-publish validation that prevents bad data from going public
3. **Add posting crons** — Separate from pipeline, handles timing and Twitter API
4. **Retire The Quarry** — Remove all Quarry references from the governing spec
5. **Retire legacy scripts** — `scripts/daily_accuracy_report.py`, `scripts/run_daily_pipeline.sh` formally marked as dead code to archive

---

## Motivation

HazeyData's public presence depends on accurate, timely WTI content on Twitter. The current approach — ad hoc scripts reading from various pipeline output paths — led to a March 24 incident where a tweet was generated from stale v3 shadow data instead of the current v4 pipeline output. A separate incident showed AK at WTI 9.2 while the other three WDW parks were in the 20s, which looked clearly wrong to anyone who saw it.

The fix is to make content generation a formal pipeline step that reads exclusively from v4 outputs and includes a quality gate before anything goes public.

---

## Design

### Architecture

```
V4 Pipeline (6 AM ET)
  ├── Steps 1-12: existing pipeline (unchanged)
  ├── Step 13: report (existing — Discord pipeline report)
  └── Step 14: content (NEW)
        ├── Extract predicted WTI for tomorrow (WDW 4 parks)
        ├── Extract observed WTI for yesterday (WDW 4 parks)
        ├── Run quality gate on both
        └── Write content JSONs with status: "ready" or "held"
                    ↓
Posting Crons (separate from pipeline):
  ├── 4:00 PM ET: render predicted visual → post tweet → save tweet ID
  └── 8:30 AM ET (next day): render observed visual → reply to prediction tweet
```

### Step 14: Content Generation (`s14_content.py`)

**Runs as:** Part of the daily v4 pipeline, after Step 13
**Reads from:** `wti/wti.parquet` (the single source of truth from Step 9)
**Writes to:** `content/` directory in pipeline output

**Responsibilities:**

1. Read `wti/wti.parquet` where `source='forecast'` for tomorrow's date, filtered to WDW park codes (MK, EP, HS, AK)
2. Read `wti/wti.parquet` where `source='historical'` for yesterday's date, same 4 parks
3. Run the quality gate (see below) on both datasets
4. Write `content/predicted_{YYYY-MM-DD}.json` with status field
5. Write `content/observed_{YYYY-MM-DD}.json` with status field
6. If any quality check fails: set `status: "held"`, include failure reasons, post warning to #wti-pipeline
7. If all checks pass: set `status: "ready"`
8. Never touch `content/tweet_state.json` — that's managed by the posting cron

**Does NOT:** Post tweets, render videos, interact with Twitter API, or manage tweet threading. Those are the posting cron's job.

### Content JSON Contract

```json
{
  "type": "predicted",
  "status": "ready",
  "held_reasons": [],
  "target_date": "2026-03-26",
  "generated_at": "2026-03-25T06:55:00",
  "generated_by": "s14_content v1.0",
  "property": "WDW",
  "parks": [
    {
      "park_code": "MK",
      "park_name": "Magic Kingdom",
      "wti": 21.9
    },
    {
      "park_code": "EP",
      "park_name": "EPCOT",
      "wti": 29.4
    },
    {
      "park_code": "HS",
      "park_name": "Hollywood Studios",
      "wti": 20.7
    },
    {
      "park_code": "AK",
      "park_name": "Animal Kingdom",
      "wti": 24.6
    }
  ]
}
```

When held:

```json
{
  "type": "predicted",
  "status": "held",
  "held_reasons": [
    "PEER_OUTLIER: AK wti 9.2 is 62% below peer mean 24.0 (threshold: 60%)"
  ],
  "target_date": "2026-03-25",
  ...
}
```

### Tweet State

```json
{
  "last_predicted": {
    "tweet_id": "1486...",
    "target_date": "2026-03-26",
    "posted_at": "2026-03-25T16:00:12Z"
  }
}
```

Managed exclusively by the posting cron. Step 14 never reads or writes this file. Implementation details (file path, format, error handling) are left to the implementer — Wilma determines the simplest reliable approach for storing and retrieving tweet IDs for reply threading.

---

## Quality Gate

The quality gate runs on every content JSON before it's marked "ready." All checks must pass. If any check fails, the content is marked "held" with the specific failure reason(s).

### Check 1: Completeness

All 4 WDW parks (MK, EP, HS, AK) must be present with non-null WTI values.

**Failure:** `INCOMPLETE: Missing parks: [AK]`

### Check 2: Absolute Bounds

Each park's WTI must fall within `[1.0, 70.0]`. Values outside this range are almost certainly data errors. (Historical WDW WTI range is approximately 3–55.)

**Failure:** `OUT_OF_BOUNDS: MK wti -2.3 outside [1.0, 70.0]`

### Check 3: Peer Outlier (Cross-Park Sanity)

No single park's WTI should deviate more than 60% from the mean of the other parks on the same date. This catches the AK-at-9-while-others-at-24 scenario.

Calculation: For each park, compute `peer_mean = mean(other 3 parks' WTI)`. If `abs(park_wti - peer_mean) / peer_mean > 0.60`, flag it.

**Failure:** `PEER_OUTLIER: AK wti 9.2 is 62% below peer mean 24.0 (threshold: 60%)`

**Note:** This check is intentionally aggressive. Real scenarios where one WDW park is dramatically different from the others do exist (e.g., AK closing early on a party night) but are rare. It's better to hold a legitimate outlier for manual review than to publish a data error. The manual release mechanism handles true outliers.

### Check 4: Day-over-Day Stability

For predicted content only: compare tomorrow's predicted WTI against yesterday's predicted WTI for each park. If any park's WTI changed by more than 15 points between consecutive predictions, flag it.

Calculation: Read `content/predicted_{yesterday}.json` if it exists. For each park, if `abs(today_wti - yesterday_wti) > 15`, flag it.

**Failure:** `DAY_JUMP: MK predicted wti jumped 22.1 → 45.3 (+23.2, threshold: 15)`

### Check 5: Staleness

The `wti/wti.parquet` file must have been modified today (within the current pipeline run). If the pipeline failed before Step 9 and Step 14 is reading yesterday's stale WTI data, don't publish it.

**Failure:** `STALE_DATA: wti.parquet last modified 2026-03-24, expected 2026-03-25`

### Held Content Resolution

**Default behavior:** If quality gate fails, content is marked `"status": "held"` and a warning posts to #wti-pipeline. The posting cron skips held content. No tweet goes out.

**Manual release:** Fred can review held content and approve it for posting via a Discord command or by manually editing the status field in the JSON to `"released"`. The posting cron treats `"released"` the same as `"ready"`.

**Logging:** Every quality gate run — pass or fail — is logged in the pipeline log with the specific check results. This creates an audit trail.

---

## Posting Crons

These are separate from the v4 pipeline. They read content JSONs and tweet_state.json, render Remotion visuals, and interact with the Twitter API.

> **Superseded for the tweets themselves** (TPCR-TWEET-V2, TPCR-TWEET-RESTYLE-CUTOVER): `operations/scripts/wti_predicted_tweet.py` and
> `wti_observed_tweet.py` now post the v2 card MP4 with alt text (locked design, tpcr-meta#12), not a Remotion render. During the
> Oct 28-30 2026 overlap a reply to an old-format original still uses the old Remotion reply (`operations/scripts/tweet_v1_legacy.py`).
> Tweet text and current behaviour: `operations/tweet_v2/README.md` ("Tweet text (new format)"). The steps and taglines below describe the pre-v2 crons.

### Predicted Tweet Cron (~4:00 PM ET)

1. Read `content/predicted_{tomorrow}.json`
2. If `status` is not `"ready"` or `"released"`, skip — log and exit
3. Render Remotion composition with prediction visual styling
4. Post tweet with rendered visual + caption text
5. Save tweet ID to `content/tweet_state.json`
6. Log success/failure to Discord

### Observed Tweet Cron (~8:30 AM ET, after pipeline)

1. Read `content/observed_{yesterday}.json`
2. If `status` is not `"ready"` or `"released"`, skip — log and exit
3. Read `content/tweet_state.json` to get the prediction tweet ID to reply to
4. Render Remotion composition with observed visual styling (different background to distinguish from predictions)
5. Post tweet as reply to the prediction tweet ID
6. Log success/failure to Discord

### Visual Styling

- **Predicted tweets:** One visual style/background. Tagline communicates these are forecasts (e.g., "Tomorrow's WTI Forecast — Walt Disney World")
- **Observed tweets:** Different visual style/background. Tagline communicates these are actuals (e.g., "Yesterday's WTI (Observed) — Walt Disney World")
- The visual contrast must be immediately obvious so followers can tell prediction from observation at a glance
- Specific design is Pebbles' domain — this spec defines the data contract, not the visual design

### Remotion Integration

The existing Remotion project at `~/clawd-anthropic/remotion-experiments/remotion-tpcr/` is the rendering engine. The data extraction script (`scripts/extract_daily_wti.py` or equivalent) must read exclusively from the content JSON files produced by Step 14 — never directly from parquet files or DuckDB. This ensures the quality gate is always in the path.

---

## Scope

**V1 (this amendment):** WDW only (4 parks: MK, EP, HS, AK). Predicted + observed tweet thread pattern.

**Future:** Multi-property expansion (Universal, Tokyo Disney, etc.) follows the same pattern — add park codes to the extraction filter, add property-specific Remotion compositions, potentially separate Twitter accounts per property.

---

## Retired: The Quarry

The Quarry analytics dashboard (`docs/the-quarry.html`) is retired as of this amendment. All references to The Quarry in the V4 Design spec, SESSION_LOG, and related documents should be updated to reflect this.

The Quarry's role — internal analytics visibility — is replaced by:
- **Tier 1:** Daily pipeline reports in #wti-pipeline (s13_report.py) — already working
- **Tier 2:** On-demand deep dives via Discord (entity_deep_dive.py) — already working
- **Public visibility:** Twitter content pipeline (this amendment)

The `docs/analytics-data/` directory and `scripts/generate_analytics_json.py` are no longer maintained or required by the pipeline. These should be archived.

---

## Retired: Legacy Accuracy Scripts

The following scripts in `scripts/` are formally dead code and should be moved to `scripts/archive/`:

- `scripts/daily_accuracy_report.py` — Superseded by `pipeline/steps/s10_accuracy.py`
- `scripts/run_daily_pipeline.sh` — Superseded by `pipeline/run.py`

These were modified by Wilma on 2026-03-24 under the mistaken belief they were part of the v4 pipeline. They are not. The v4 pipeline's accuracy system (Step 10) runs every day and produces accuracy metrics as part of the daily report (Step 13). Any modifications to legacy scripts have no effect on production.

---

## Directory Structure Addition

Add to the V4 directory structure:

```
pipeline/
├── steps/
│   ├── ... (existing s01-s13)
│   └── s14_content.py              # Content generation for Twitter (NEW)
```

Output namespace:

```
{output_base}/
├── content/                         # NEW
│   ├── predicted_YYYY-MM-DD.json   # Tomorrow's WTI prediction content
│   ├── observed_YYYY-MM-DD.json    # Yesterday's WTI observed content
│   └── tweet_state.json            # Tweet IDs for reply threading
```

---

## Implementation Plan

### Phase 1: Step 14 implementation

1. Write `pipeline/steps/s14_content.py` with extraction + quality gate
2. Register Step 14 in `pipeline/run.py` orchestrator
3. Test with current pipeline data — verify JSON output is correct
4. Verify quality gate catches the known-bad AK scenario

### Phase 2: Posting cron setup

1. Update Remotion data extraction to read from content JSONs (not parquets)
2. Create/update posting scripts for predicted + observed tweet patterns
3. Set up cron entries: ~4 PM predicted, ~8:30 AM observed
4. Test end-to-end with dry-run (render but don't post)

### Phase 3: Go live

1. Post first predicted tweet manually, verify it looks right
2. Enable automated posting
3. Monitor for 3-5 days, verify reply threading works
4. Verify quality gate holds content when it should

---

## Success Criteria

| Phase | Target | Measure |
|-------|--------|---------|
| 1 | Step 14 generating correct JSONs | Content matches `wti/wti.parquet` values exactly |
| 1 | Quality gate catches bad data | Synthetic test with AK=9 / others=24 produces "held" |
| 2 | Remotion reads from content JSONs only | No direct parquet/DuckDB reads in posting scripts |
| 3 | Tweet thread pattern works | Observed reply appears under predicted tweet |
| 3 | Quality gate prevents bad tweets | Zero incorrect WTI values published in first 2 weeks |

---

*Barney — Chief of Pipeline, Slate Rock & Gravel Co. 🪨*
