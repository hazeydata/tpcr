# WTI / TPCR — Project Background

**Vision, history, strategic decisions.** Changes rarely. Read on cold-start or during strategic pivots.

---

## Product vision

Theme Park Crowd Report (TPCR) is a daily ML pipeline that produces Wait Time Index (WTI) predictions for theme parks. Output published to @DisneyStatsWhiz on Twitter/X and hosted at hazeydata.ai. Free, community-oriented service; organic growth; stunning shareable visuals; feedback-driven improvement.

## Tech stack

- **Storage:** DuckDB (primary data store). Parquet for forecasts/facts.
- **Models:** XGBoost, per-entity. Baseline features: mins_since_6am, mins_since_open, date_group_id_encoded, season_encoded, season_year_encoded, day_of_week, plus `recent_level_28d` (TPCR-ACCURACY-001, see docs/PIPELINE_V4_DESIGN.md Step 7): by default (`recent_level_kind="yoy_ratio"`) the entity's own trailing 28-day mean actual_time divided by the mean over the same window 364 days earlier, lag 7, leak-free; `"mean"` uses the raw trailing mean; forecast-fed only up to 14 days after the issue date. At forecast time a season_year label the model never trained on uses the code of its latest earlier seen same-season label (`season_year_unseen_fallback`, off by default: `season_year_unseen_fallback=False`).
- **Pipeline:** Python, 13 steps, runs 6 AM ET daily via cron on wilma-server.
- **Tweet rendering:** Remotion.
- **Infra:** GitHub Actions, Clawdbot/OpenClaw, systemd.

## Forecast scope

- 730 days out (~March 2028)
- ~46M predictions/day across ~59K WTI park-dates
- Operating calendar: 11.8M rows
- ~420 active models covering 271 entities (rest on fallback)

## Key architectural decisions

**Pipeline V4 (S6–S7, March 2026).** Full redesign from V3. Baseline is pure: data → XGBoost → predictions → WTI aggregation. No bias correction, no quantile mapping in the baseline. Post-processing ideas earn their way in via the competition framework.

**Competition framework (Amendment 002).** 7–10 challengers run simultaneously, each testing a single hypothesis. Rolling daily competition (not weekly). Auto-evaluation at Day 7, human-approved promotion. Manual challenger queue exhausted before auto-generation is enabled. Auto-retirement threshold: challenger MAE > baseline + 2.0.

**Rolling Shadow Evaluation (S-early-April 2026).** Evaluation methodology centralized in `pipeline/competition/shadow_evaluate.py`. Must match `s10_accuracy.py` exactly (entity-weighted averaging, ACTUAL not POSTED, TIME_BUCKET midpoint rounding, synthetic actuals fallback).

**WDW Daily Recap (Amendment 003).** Blog posts analyzing predicted-vs-observed WTI gaps. 9 AM ET cron.

**Service Status v2 (Amendment 004).** Redesigned with correct deployment sequence to prevent false customer alerts.

**Tweet threading.** Observed tweets reply to the predicted tweet sharing the same `reference_date` (fixed; previously used stale `last_predicted` key).

**Water parks excluded.** BB, TL, VB excluded from ETL ingestion entirely — fundamentally different product.

**Priority Queue (PQ) terminology.** All Lightning Lane variants referred to as "Priority Queue (PQ)" across all systems/docs.

**The Quarry retired (S30).** Internal analytics dashboard retired — removed from governing docs.

## Known issues & patterns

**Tokyo calibration.** POSTED-only data with no ACTUAL coverage. Fix applies global POSTED→ACTUAL ratios to all Tokyo entities, with retraining planned.

**Stale-list bugs (recurring class).** Hardcoded name lists that mirror pipeline-writes become refactor landmines. Examples: #463 (`QUEUE_TIMES_PARK_MAP`), #464 (symptom), #467 (`trained_methods` set missing V4 method names). Stale-list audit sweep queued.

**"Close fast, verify never" (Wilma).** Always verify her work via GitHub directly, not summaries.

**Gazoo audit lag.** Two daily cycles (2 AM, 4 PM ET) mean "current state" can be 8-10 hours stale. Argument for heartbeat alarm (ticket #466).

**DuckDB read-blocking during pipeline runs.** Read-only queries during pipeline execution are NOT safe — read source code instead.

## Agent communication tiers

Fred (Tier 1, approvals) → Barney (Tier 2, Claude Desktop, strategy/architecture) → Dino (Tier 3, Claude Code on Mac Mini, operations execution) → wilma-server (Tier 4, Linux compute at 192.168.2.75).

## Strategic direction (Q2 2026)

- Challenger auto-generation (deferred until manual queue exhausted)
- Multi-property tweet expansion (Disneyland Resort, Universal Orlando) — planned since S21
- Customer service phases 2+ (bot error handling, feedback tracking, onboarding)
- Heartbeat alarm + duplicate-poster detection (TPCR #466)
