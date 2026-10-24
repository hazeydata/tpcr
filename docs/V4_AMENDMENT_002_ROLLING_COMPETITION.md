# V4 Amendment 002: Rolling Competition Framework

**Version:** 1.0
**Date:** 2026-03-26
**Authors:** Barney (architect) + Fred (decision-maker)
**Status:** APPROVED by Fred — 2026-03-26
**Amends:** `PIPELINE_V4_DESIGN.md` Phase E (Competition Framework)
**Superseded in part (TPCR-SHADOW-REVIVE-001 Phase 4):** nothing posts to Discord any more, and challengers are no longer auto-retired or auto-flagged after 7 days. A challenger is flagged only by the streak + bootstrap + gain rule, and promotion is always manual. Current behavior: `SHADOW_COMPETITION.md`.

---

## Summary

Replace the sequential competition model (one challenger at a time, 7-day test, evaluate, repeat) with a **rolling daily competition** where a new challenger is added every day and all active challengers shadow-run in parallel. After 7 days of shadow data, each challenger is evaluated for promotion or retirement.

This supersedes the Phase E description in `PIPELINE_V4_DESIGN.md` while preserving the core principle: **challengers earn their way into production with data, not intuition.**

---

## The Problem with Sequential Testing

The original Phase E design called for deploying one challenger at a time and shadow-running it for 7-14 days before evaluating. This is safe but slow:

- 10 hypotheses × 7 days each = **70 days** to test everything
- Only one signal at a time — no ability to compare challengers against each other
- Encourages "big bet" challengers that change multiple things at once (harder to interpret)
- Dead time between experiments while results are reviewed

## The Rolling Competition Model

### Core Concept

Add a new challenger to the shadow pool every day. All active challengers run in parallel. After each challenger accumulates 7 days of shadow data, it's automatically evaluated. Winners get flagged for promotion review. Losers get retired.

```
Day 1:  baseline + challenger_A
Day 2:  baseline + challenger_A + challenger_B
Day 3:  baseline + challenger_A + challenger_B + challenger_C
...
Day 7:  baseline + A + B + C + D + E + F + G  ← A has 7 days, auto-evaluate
Day 8:  baseline + [A promoted or retired] + B + C + D + E + F + G + H
Day 9:  baseline + B + C + D + E + F + G + H + I  ← B has 7 days, auto-evaluate
...
```

At steady state: **7-10 challengers running simultaneously**, each testing a single hypothesis, with one evaluation happening every day.

### Why This Works

1. **Challengers don't interact.** Each one independently generates predictions from its own model weights against the same actuals. Running them in parallel doesn't contaminate results.

2. **Compute cost is trivial.** Each challenger forecast takes ~2 minutes of Python/XGBoost on wilma-server. Ten challengers = 20 minutes added to a pipeline with 23 hours of daily idle time. No API credits. No LLM calls. Pure CPU.

3. **Single-hypothesis challengers are easier to interpret.** When `xgb-dow` (day-of-week feature) wins, you know exactly what caused the improvement. When `xgb-kitchen-sink` (5 new features + different hyperparams) wins, you learn nothing about which change helped.

4. **Daily evaluation cadence matches daily pipeline cadence.** The infrastructure already archives predictions daily and compares against actuals. Adding more models to the comparison loop is incremental.

---

## Architecture

### Challenger Registry

A JSON config file listing all active and retired challengers:

```
pipeline/competition/challenger_registry.json
```

```json
{
  "challengers": [
    {
      "name": "xgb-highLR",
      "hypothesis": "Higher learning rate (eta=0.2 vs 0.1) converges better with current data volume",
      "model_path": "models/{entity}/model_xgb-highLR.json",
      "added_date": "2026-03-26",
      "status": "shadow",
      "shadow_days": 0
    },
    {
      "name": "xgb-dow",
      "hypothesis": "Day-of-week as 6th feature captures weekend/weekday crowd patterns",
      "model_path": "models/{entity}/model_xgb-dow.json",
      "added_date": "2026-03-27",
      "status": "shadow",
      "shadow_days": 0
    }
  ],
  "retired": [],
  "promoted": [],
  "settings": {
    "min_shadow_days": 7,
    "max_active_challengers": 10,
    "auto_retire_if_worse_by": 2.0,
    "promotion_requires_approval": true
  }
}
```

### Daily Shadow Run (New Step — runs after Step 10)

**Trigger:** Dino cron on Mac Mini, fires after pipeline completes (~7:00 AM ET)
**Execution:** SSH to wilma-server, run shadow forecast loop
**For each active challenger:**
1. Load challenger model weights
2. Generate predictions for all entities (same feature set, same operating calendar)
3. Save to `forecasts/shadow/{challenger_name}/forecast_YYYY-MM-DD.parquet`
4. Compare yesterday's shadow predictions against actual observations
5. Update challenger's `shadow_days` count and running accuracy metrics

**Output:** Daily shadow comparison report posted to #wti-pipeline

### Evaluation Rules

After a challenger accumulates `min_shadow_days` (7) of shadow data:

1. **Auto-retire** if overall MAE is worse than baseline by more than `auto_retire_if_worse_by` (2.0 min). No human review needed — it's clearly worse.

2. **Flag for promotion** if overall MAE beats baseline AND:
   - Wins on at least 3 of 4 WDW parks
   - Doesn't degrade worst-20 entities by more than 10%
   - Entity-level win rate > 55% (more entities improved than degraded)

3. **Extend to 14 days** if results are borderline (within 0.5 min of baseline either way). More data resolves ambiguity.

4. **Promotion requires Fred + Barney approval.** The system flags candidates; humans decide. No auto-promotion.

### Promotion Process

When a challenger is approved for promotion:

1. Challenger model files become the new baseline: `model_{challenger}.json` → `model_baseline.json`
2. Old baseline is archived: `model_baseline.json` → `model_baseline_pre_{challenger}.json`
3. The improvement is documented in SESSION_LOG with entity-level evidence
4. The promoted hypothesis becomes part of the baseline description
5. New challengers can now test against the improved baseline

### The Challenger Queue

Fred and Barney maintain a prioritized queue of hypotheses to test. Dino pulls from the queue daily (or as directed). Each entry is a single hypothesis:

**Initial queue (suggested order):**

| Priority | Challenger Name | Hypothesis | What Changes |
|----------|----------------|------------|--------------|
| 1 | `xgb-highLR` | Higher learning rate converges better | eta: 0.1 → 0.2 |
| 2 | `xgb-dow` | Day-of-week feature captures patterns | Add feature #6: day_of_week |
| 3 | `xgb-deeper` | More depth captures interactions | max_depth: 10 → 12 |
| 4 | `xgb-recent` | Recent data matters more | geo_decay_halflife: 730 → 365 |
| 5 | `xgb-seasonal` | Holiday proximity helps | Add feature: days_to_nearest_holiday |
| 6 | `xgb-narrow` | Less depth reduces overfitting | max_depth: 10 → 6 |
| 7 | `xgb-moretrees` | More trees with lower LR | n_estimators: 2000 → 5000, eta: 0.05 |
| 8 | `xgb-subsample` | Less subsampling for stability | subsample: 0.8 → 0.6 |
| 9 | `xgb-hour-bucket` | Hour-of-day buckets vs minutes | Replace mins_since_6am with hour_bucket |
| 10 | `xgb-park-feature` | Park identity as feature | Add feature: park_code_encoded |

Each tests ONE thing. Results are interpretable. The queue is replenished as we learn what works.

---

## Excluded Parks

The following park codes are excluded from all pipeline processing per Fred directive (Session 22):

- **BB** — Blizzard Beach (WDW water park)
- **TL** — Typhoon Lagoon (WDW water park)
- **VB** — Volcano Bay (Universal water park)

Water parks are a fundamentally different product with intermittent schedules and weather-dependent operations. WTI doesn't translate meaningfully for their format. They are filtered at ETL ingestion (`step_02_etl.py`) so no observations enter the fact tables. No models trained, no forecasts generated, no tweets published.

---

## Cost Model

| Component | Cost | Notes |
|-----------|------|-------|
| Shadow forecasts | ~2 min CPU per challenger | Pure Python/XGBoost on wilma-server |
| 10 challengers/day | ~20 min total | Added to 55-min pipeline |
| Storage | ~50MB/day per challenger | Parquet forecasts, archived daily |
| API credits | $0 | No LLM calls in forecast loop |
| Dino orchestration | Included in Claude Max | SSH trigger + monitoring |

**Total incremental cost: ~20 minutes of CPU time and 500MB of disk per day.** Negligible.

---

## Implementation Plan

### Phase 1: Infrastructure (Dino — 2-3 hours)
1. Create `challenger_registry.json` schema and initial file
2. Build `shadow_forecast.py` — loops through active challengers, generates predictions
3. Build `shadow_evaluate.py` — compares yesterday's shadow vs actuals, updates registry
4. Build `shadow_report.py` — generates daily comparison report for Discord
5. Add Dino cron entry: `0 7 * * * shadow_run.sh` (after pipeline completes)

### Phase 2: First Challenger (already in progress)
1. Fix `xgb-highLR` incomplete forecasts (Step A from competition briefing)
2. Register in challenger registry
3. Start shadow run — Day 1 of 7

### Phase 3: Daily Queue (starts after Phase 2 verified)
1. Add one new challenger per day from the queue
2. Train challenger models on wilma-server (Dino triggers via SSH)
3. Register in challenger registry
4. Shadow runs accumulate automatically

### Phase 4: First Promotion Decision (Day 8+)
1. `xgb-highLR` hits 7 days of shadow data
2. Dino posts entity-level evaluation to #wti-pipeline
3. Barney reviews, recommends to Fred
4. Fred approves or rejects
5. If approved: promote, archive old baseline, update docs

---

## What This Replaces

The Phase E section of `PIPELINE_V4_DESIGN.md` described a sequential competition:

> **Phase E: Competition Framework (Week 2+)**
> 1. First challenger: Add `day_of_week` as 6th feature. Train as `model_day_of_week_v1.json`. Shadow-run alongside baseline for 7-14 days.
> 2. Quantile mapping challenger: enters as named challenger. Earns its way in by beating pure baseline MAE.
> 3. Promote or discard: Based on data, not intuition.

This amendment preserves the principles (named challengers, shadow runs, data-driven promotion) but changes the execution from sequential to parallel rolling. The "7-14 days of shadow data" requirement remains — it's now per-challenger, with up to 10 running simultaneously.

---

## Success Criteria

| Metric | Target |
|--------|--------|
| Shadow infrastructure running | Dino cron fires daily, all challengers produce predictions |
| First evaluation | `xgb-highLR` evaluated at Day 7 with entity-level breakdown |
| Steady state | 7-10 challengers active at any time |
| First promotion | At least one challenger promoted within 30 days |
| Baseline MAE improvement | Measurable drop from 6.85 within 60 days |

---

*Barney — Chief of Pipeline, Slate Rock & Gravel Co. 🪨*
