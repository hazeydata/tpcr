# Prediction Accuracy Improvement Roadmap

**Version:** 1.0 DRAFT
**Date:** 2026-03-21
**Author:** Barney (Chief of Pipeline)
**Status:** PROPOSED — Awaiting Fred's review
**Baseline:** Overall MAE ~6-7 minutes (entity-level predicted_actual vs observed actual)

---

## The Situation

We have a working pipeline producing 64M predictions across 400+ entities at 12 parks, with MAE around 6-7 minutes. The model is XGBoost (Julia-trained, geo-decay weighted), predicting `predicted_actual` (estimated real wait time) at 5-minute intervals. Features include `mins_since_6am`, `mins_since_open`, `date_group_id_encoded`, `season_encoded`, `season_year_encoded`, and optionally `posted_time`. TPCR-ACCURACY-001 adds `recent_level_28d` to the V4 baseline: by default a year-over-year ratio (the entity's own trailing 28-day mean actual_time, 7-day lag, divided by the mean over the same window 364 days earlier; leak-free), with the raw trailing mean available as `recent_level_kind="mean"`; forecasts feed it only to target days within 14 days of the issue date, NaN beyond. Defined in docs/PIPELINE_V4_DESIGN.md Step 7. s08 also maps a season_year label a model never trained on to its latest earlier seen same-season label (Step 8).

The bias correction disaster of March 17 taught us something important: **post-processing hacks that modify predictions after the model runs are fragile and dangerous.** The path to better accuracy is through better models, better features, and better training data — not through ad-hoc adjustments layered on top.

This document proposes a phased, experimental approach to systematically reduce MAE from ~6-7 to a target of ~4-5 minutes, with full measurement infrastructure so we know what's working.

---

## Current Pipeline Architecture (What We Have)

```
S3 Data → ETL → Parquet Fact Tables
                    ↓
            Dimension Tables (dates, seasons, park hours, entity metadata)
                    ↓
            Matched Pairs (DuckDB: entity × date × time_slot → features + target)
                    ↓
            XGBoost Training (Julia, per-entity, geo-decay weights)
                    ↓
            Per-Entity Models (~412 models in /models/{entity_code}/)
                    ↓
            Forecast (vectorized, 288 slots/day × 365 days × 400+ entities)
                    ↓
            WTI Calculation (entity avg → park WTI, + quantile mapping)
                    ↓
            Archive + Accuracy Evaluation
```

### Model Hierarchy (Current)
1. **model_actuals** (best): Trained on ACTUAL observations only, 5 features
2. **model_v2**: Trained on POSTED observations, 7 features (includes posted_time)
3. **model_scope_scale**: Pooled group model by attraction category (for EU/new entities)
4. **aggregate**: Median posted × fallback ratio (no ML)
5. **fallback_ratio**: Default posted × 0.678 (last resort)

### Known Issues
- **Quantile mapping** in `calculate_wti_simple.py` remaps forecast WTI to match historical distributions — may be adding error on well-calibrated parks
- **Bias correction** (KILLED) — was modifying `all_forecasts.parquet` in-place
- **WTI-level adaptive bias** (disabled Feb 28) — was double-correcting because `season_year` already captures trends
- **Model compression**: XGBoost compresses predictions toward the mean (narrow range vs actuals)

---

## Phase 0: Clean Baseline (TODAY)

**Goal:** Establish a trustworthy accuracy measurement before changing anything.

### Actions
1. **Restore `all_forecasts.parquet` from `.pre_bias_correction` backup** — remove March 17 contamination
2. **Regenerate WTI** from clean forecasts
3. **Quantile mapping evaluation**: Run accuracy eval WITH and WITHOUT quantile mapping on the same day's data. If QM is adding error, disable it. If it's helping, keep it but document the magnitude.
4. **Archive baseline**: Record MAE, bias, and per-park breakdown for the first clean day (March 22). This is the number everything gets compared against.
5. **Freeze the accuracy evaluator**: No changes to `evaluate_forecast_accuracy.py` during the experiment window. The measuring stick must be stable.

### Deliverables
- `docs/ACCURACY_BASELINE_2026_03_22.md` — the reference point
- Decision on quantile mapping (on/off)
- Clean pipeline running without any post-processing hacks

---

## Phase 1: Better Training Data (Week 1-2)

**Hypothesis:** The model's accuracy ceiling is set by training data quality. Garbage in, garbage out.

### 1A. Synthetic Actuals Integration

**What:** The pipeline already generates synthetic actuals (POSTED → converted via conversion model), but training currently uses raw POSTED or ACTUAL only. Integrate synthetic actuals into training.

**Why:** Most entities have 10-100× more POSTED observations than ACTUAL. The conversion model transforms these to "what actual probably was." If the conversion model is decent, this massively expands training data for underserved entities while keeping the target variable consistent (predicted_actual).

**How:**
- Modify matched pairs builder to include synthetic actuals as a third source
- Weight: ACTUAL × 3.5, synthetic × 1.0 (same as WTI weighting)
- Measure: Compare MAE of model trained on actuals-only vs actuals+synthetic

**Risk:** If conversion model is bad, synthetic actuals inject noise. Mitigated by the 3.5× weight for real actuals.

### 1B. Training Data Freshness

**What:** The geo-decay weighting already down-weights old data, but the half-life may not be optimal.

**Why:** Theme parks change — new attractions open, crowd patterns shift with new ticketing systems, seasonal patterns evolve. Data from 2019 may actively hurt predictions for 2026.

**How:**
- Experiment with shorter geo-decay half-lives (current unknown — check metadata)
- Test hard cutoff: train only on data from last 18 months vs all-time
- Measure: Compare MAE across half-life values

### 1C. Outlier Filtering

**What:** Remove training pairs where actual wait time is clearly anomalous (ride broke down and reopened to a surge, special events with artificial queues, data collection errors).

**Why:** XGBoost is robust to outliers in features but not in the target variable. A few "180 minute actual wait" observations from ride breakdowns can distort predictions for normal operations.

**How:**
- Per-entity IQR filtering on target variable (drop > 3× IQR from median)
- Or: Winsorize at P1/P99 per entity instead of dropping
- Measure: Compare MAE with and without filtering

---

## Phase 2: Better Features (Week 2-4)

**Hypothesis:** The current feature set misses important signals. Adding the right features lets the model capture patterns it currently can't.

### 2A. Day-of-Week Feature

**What:** Add explicit day-of-week (0-6) as a feature.

**Why:** `date_group_id` captures holiday/event patterns, but a simple Tuesday vs Saturday distinction is buried in it. Most parks have dramatically different crowd patterns on weekdays vs weekends, and the model has to learn this indirectly.

**How:** Add `day_of_week` to matched pairs and feature list. Retrain. Compare.

### 2B. Days-Until-Holiday / Days-Since-Holiday

**What:** Numeric features measuring proximity to major holidays (Christmas, Easter, July 4, Thanksgiving, spring break windows).

**Why:** Crowd buildup and decay around holidays follows predictable curves. "3 days before Christmas" is much busier than "3 days after Christmas," but `date_group_id` treats them as discrete buckets rather than a continuous ramp.

**How:** Build a holiday calendar, compute distance features. Add to training.

### 2C. Recent Actuals as Features (Autoregressive Signal)

**What:** For short-horizon forecasts (1-7 days out), include the entity's actual wait times from the most recent observed day as features.

**Why:** Yesterday's actual crowd level at Space Mountain is the single best predictor of tomorrow's crowd level. The current model has zero autoregressive signal — it treats every day as independent.

**How:**
- For entities with yesterday's data: add `actual_yesterday_avg`, `actual_yesterday_peak`
- For entities without: use park-level average from yesterday
- Only applies to short-horizon forecasts (not 90+ days out)
- Requires separate model or feature branch for "near" vs "far" forecasts

**Risk:** Creates dependency on data freshness. If scraper goes down, features go stale. Mitigated by falling back to non-AR features for stale data.

### 2D. Weather Features (Future Phase)

**What:** Temperature, rain probability, severe weather for each park-date.

**Why:** A rainy day at Magic Kingdom drops crowds 20-40%. This is the biggest single-day variance driver that the model currently can't see.

**How:** Integrate weather API (historical for training, forecast for prediction). Requires new data source in pipeline.

**Risk:** Weather forecasts degrade past 7 days. Only useful for short-horizon predictions. Medium effort.

---

## Phase 3: Better Models (Week 4-8)

**Hypothesis:** XGBoost's point predictions have structural limitations. Better model architectures can capture patterns XGBoost misses.

### 3A. Prediction Intervals (NGBoost or Quantile Regression)

**What:** Instead of predicting a single number, predict a distribution (P10, P50, P90).

**Why:** "Space Mountain will be 25 minutes" is less useful than "Space Mountain will be 15-35 minutes, most likely 25." Uncertainty quantification lets us: (a) flag low-confidence predictions, (b) give users meaningful ranges, (c) understand where the model is struggling.

**How:**
- NGBoost: Drop-in replacement for XGBoost that predicts distributions
- Or: Train three XGBoost models (P10, P50, P90) using quantile loss
- P50 becomes the point prediction; interval width measures confidence

### 3B. Entity Clustering + Transfer Learning

**What:** Group similar entities (by attraction type, park, capacity) and share model components.

**Why:** Some entities have <50 ACTUAL observations. Per-entity models for these are noisy. Scope-and-scale group models exist but are crude. A hierarchical approach (global features + entity-specific residuals) would let low-data entities benefit from high-data siblings.

**How:**
- Train a global "rides like this" model on pooled data
- Train per-entity residual models on entity-specific deviations
- Final prediction = global + residual

### 3C. Temporal Models (LSTM / Transformer)

**What:** Replace or augment XGBoost with a sequence model that treats each day's wait time curve as a time series.

**Why:** XGBoost treats each (entity, date, time_slot) as independent. But wait times within a day follow a curve — morning ramp, midday peak, evening decline. A temporal model can learn this shape and predict the full day's curve at once.

**How:** Major architecture change. Prototype on top-10 entities first. Compare against XGBoost baseline.

**Risk:** High effort, may not beat well-tuned XGBoost. Only pursue after Phase 1-2 gains are measured.

---

## Phase 4: Better Evaluation (Ongoing)

### 4A. Stratified Accuracy Reporting

**What:** Break MAE down by meaningful segments, not just overall.

**Dimensions:**
- **By forecast horizon**: 1-day MAE vs 7-day vs 30-day vs 90-day (accuracy should degrade with distance)
- **By park**: Which parks are we worst at?
- **By crowd level**: MAE on busy days vs quiet days (models often underpredict busy days)
- **By time of day**: Morning ramp vs peak vs evening
- **By entity type**: Headliner rides vs family rides vs shows
- **By data richness**: Entities with 1000+ observations vs entities with <100

This tells us WHERE to focus, not just whether the overall number moved.

### 4B. Model Competition Framework

**What:** Run multiple model versions in parallel and compare accuracy on the same data.

**Why:** You can't improve what you can't measure against alternatives. Every change in Phase 1-3 needs a controlled comparison.

**How:**
- Shadow forecasts: run new model alongside production, compare next-day accuracy
- The model competition framework spec already exists (`MODEL_COMPETITION_DESIGN.md`)
- Prediction ledger stores raw predictions per challenger for retroactive analysis

### 4C. Automated Accuracy Regression Detection

**What:** If MAE on any park exceeds 2× the 30-day average for 3 consecutive days, alert Fred.

**Why:** The March 17 bias correction disaster went undetected for 4 days. Automated guardrails prevent silent degradation.

**How:** Add to `pipeline_accuracy_drift.py` or create new standing order for Gazoo.

---

## What to Change TODAY

If I had to pick three actions to start right now that would move the needle fastest:

1. **Clean baseline (Phase 0):** Restore forecasts, evaluate quantile mapping, record the reference MAE. You can't improve what you haven't measured cleanly.

2. **Stratified accuracy reporting (Phase 4A):** Before building anything new, understand WHERE the 6-7 MAE comes from. If 80% of the error is on 3 parks during spring break, that's a completely different problem than evenly distributed error. Run the evaluator by park, by horizon, by crowd level.

3. **Day-of-week feature (Phase 2A):** This is the highest-ROI single change. It's trivial to implement (one new column in matched pairs + feature list), zero risk, and captures the biggest systematic pattern the model currently misses. If weekday vs weekend is worth even 0.5 MAE points, that's meaningful.

---

## What NOT to Do

- **No more post-processing of predictions.** No bias correction, no manual adjustments to parquet files. If the model is wrong, fix the model.
- **No wholesale model replacement.** XGBoost is working. Improve it before replacing it.
- **No premature optimization on rare entities.** Focus on the top-50 entities by traffic (they drive WTI) before worrying about a seasonal show with 20 observations.
- **No feature engineering without measurement.** Every new feature gets a shadow run and a before/after MAE comparison.

---

## Success Criteria

| Phase | Target | Measure |
|-------|--------|---------|
| Phase 0 | Clean baseline established | Documented MAE for March 22 |
| Phase 1 | MAE < 6.0 | 14-day rolling average |
| Phase 2 | MAE < 5.0 | 14-day rolling average |
| Phase 3 | MAE < 4.5 with intervals | P50 MAE + calibrated P10-P90 |
| Phase 4 | No silent regression | 3-day alert threshold active |

---

## Implementation Strategy

**Every change is experimental.** The workflow for each improvement:

1. **Hypothesis:** "Adding day-of-week will reduce MAE by ~0.5"
2. **Branch:** Create a branch, implement the change
3. **Shadow run:** Generate forecasts with the change, save as challenger
4. **Wait:** Accumulate 7-14 days of actuals
5. **Compare:** Challenger MAE vs baseline MAE, stratified by park/horizon
6. **Decide:** If better, merge to production. If worse or neutral, document and discard.
7. **Update baseline:** New production model becomes the new baseline

This is how a PhD data scientist operates: controlled experiments with measurement, not "let's try this and see what happens."

---

*Barney — Chief of Pipeline, Slate Rock & Gravel Co. 🪨*
