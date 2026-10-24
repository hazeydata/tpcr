# ⚠️ DEPRECATED — Superseded by Pipeline V4 Design

> **This document is historical.** The V3 "shadow pipeline" architecture described here has been
> fully implemented, iterated on, and superseded by the V4 design.
>
> **Current governing document:** [`docs/PIPELINE_V4_DESIGN.md`](PIPELINE_V4_DESIGN.md)
>
> V4 removed version numbers from all production paths, purified the baseline (no bias correction,
> no quantile mapping), and introduced the competition framework for model improvements.
> This file is retained for historical reference only.
>
> — Barney, 2026-03-22 (Session 6)

---

_Original V3 design document follows below for reference._

---

# Pipeline v3 — Shadow Pipeline Architecture

> **Status:** IMPLEMENTED — in production since ~March 2026
> **This document is the DESIGN doc. For current state, see pipeline_v3/ source code.**

> **Author**: Barney (Chief of Pipeline)
> **Goal**: Replace the current pipeline with a cleaner, more reliable, more accurate system
> **Approach**: Build in shadow mode, compare outputs, swap when validated

---

## Why

The current pipeline works, but it's fragile:

- **70+ hour staleness** went unnoticed (2026-03-07) because the heartbeat was misconfigured
- **OOM kills** from forecast_vectorized.py eating 49GB on a 62GB machine
- **Silent failures** — accuracy eval was broken for 2 weeks before anyone noticed
- **`import os` missing** — code shipped without testing
- **DuckDB WAL corruption** recurring with duct-tape fixes
- **72 Discord sync commits/day** making git history unreadable
- **26 scripts** in `scripts/` with overlapping responsibilities and duplicated logic (park_code bug class)
- **Julia + Python hybrid** adds operational complexity for marginal training speed gains

The pipeline grew organically. It needs an architect.

---

## Design Principles

1. **Accuracy first** — every design choice is evaluated by "does this improve MAE?"
2. **Fail loud** — no `2>/dev/null`, no `try/except: pass`, no non-fatal silencing of fatal problems
3. **Single source of truth** — one canonical module per concept (park codes, paths, config)
4. **Memory-aware** — designed for 62GB from the start, not retrofitted after OOMs
5. **Observable** — every step logs what it did, how long it took, what changed, and whether it matched expectations
6. **Testable** — can run any step in isolation with mock data
7. **Python-only** — eliminate Julia dependency. XGBoost Python is the same algorithm; Julia's speed advantage doesn't justify the operational cost

---

## Architecture Overview

```
pipeline_v3/
├── pipeline.py              # Single entry point. One file to run.
├── config.py                # All configuration in one place
├── steps/                   # One module per pipeline step
│   ├── __init__.py
│   ├── s01_sync.py          # S3 sync
│   ├── s02_etl.py           # ETL + CSV→Parquet
│   ├── s03_dimensions.py    # Dimension fetches + park hours + closures
│   ├── s04_aggregates.py    # Rebuild model_aggregates.parquet daily
│   ├── s05_conversion.py    # POSTED→ACTUAL conversion model (weekly)
│   ├── s06_synthetic.py     # Synthetic actuals generation
│   ├── s07_training.py      # Per-entity XGBoost training (Python, not Julia)
│   ├── s08_forecast.py      # Forecast generation (memory-safe by design)
│   ├── s09_wti.py           # WTI calculation
│   ├── s10_accuracy.py      # Accuracy evaluation
│   ├── s11_deploy.py        # Cloudflare deploy + API restart
│   └── s12_validate.py      # Post-run validation + completeness
├── models/                  # Model-related utilities
│   ├── xgboost_trainer.py   # Unified XGBoost training (replaces Julia)
│   ├── model_registry.py    # Track model versions, MAE, lineage
│   └── conversion.py        # POSTED→ACTUAL conversion with validation gate
├── core/                    # Shared infrastructure
│   ├── park_codes.py        # Canonical park code mappings (one source)
│   ├── db.py                # DuckDB connection management (no WAL corruption)
│   ├── paths.py             # All path constants
│   ├── logging.py           # Structured logging with step context
│   ├── metrics.py           # Pipeline metrics collection
│   └── validation.py        # Data validation primitives
├── tests/                   # Actual tests
│   ├── test_park_codes.py
│   ├── test_wti.py
│   ├── test_forecast.py
│   └── fixtures/            # Small parquet files for testing
└── shadow/                  # Shadow mode comparison tools
    ├── compare_wti.py       # Compare v3 WTI vs production WTI
    ├── compare_forecasts.py # Compare v3 forecasts vs production
    └── report.py            # Generate shadow comparison report
```

---

## Key Design Decisions

### 1. Single entry point

```bash
python pipeline_v3/pipeline.py                    # Full run
python pipeline_v3/pipeline.py --step training     # Single step
python pipeline_v3/pipeline.py --step forecast --park MK  # Single park
python pipeline_v3/pipeline.py --shadow            # Shadow mode (compare, don't deploy)
```

One file. No `run_daily_pipeline.sh` → `run_training_robust.sh` → `hybrid_pipeline_v2.py` → `julia-ml/train_actuals_v2.jl` chain. The shell orchestrator is replaced by Python with proper error handling.

### 2. Python-only training (drop Julia)

The current pipeline uses Julia XGBoost for training speed. But:
- Julia adds a runtime dependency, separate package management, and a language barrier for debugging
- The actual training time is ~30 min for 243 entities — not the bottleneck
- Python XGBoost produces identical models (same libxgboost underneath)
- The OOM issues in training were from data loading, not model fitting

v3 trains in Python with explicit memory management: load one park at a time, train entities, flush, next park. Same pattern Julia was doing, without the language boundary.

### 3. Memory-safe forecast by design

Current forecast loads all models + full time grid + pickles `agg_lookup` to 8 workers = OOM.

v3 approach:
- Process **one park at a time** (Option D from the OOM analysis)
- Each park: load that park's models, generate that park's grid, produce forecasts, write to parquet, release memory
- No multiprocessing — sequential is fine when each park takes <60s
- Total forecast time: ~10 min (13 parks × ~45s each)
- Peak memory: ~2-3GB (one park's worth)

### 4. DuckDB with explicit connection management

Current pipeline has WAL corruption because multiple processes/scripts open the same DuckDB file concurrently. v3 uses:
- Read-only connections for all query operations
- A single write step at the end that loads results into `tpcr_live.duckdb`
- All intermediate data stays in parquet (no DuckDB until final load)

### 5. Conversion model validation gate

Current pipeline retrains the POSTED→ACTUAL conversion model every Monday unconditionally. Bad Monday data = silently worse model.

v3 approach:
- Train candidate model on fresh data
- Evaluate candidate on holdout set
- Compare MAE against current production model
- Only deploy if candidate is better (or within tolerance)
- Keep previous model as automatic rollback

### 6. Structured logging with step context

Every step logs:
```json
{
  "step": "s07_training",
  "entity": "MK01",
  "action": "train_complete",
  "mae": 4.2,
  "n_samples": 12450,
  "duration_sec": 3.1,
  "memory_mb": 450,
  "timestamp": "2026-03-08T06:12:34Z"
}
```

No more parsing log files with regex to figure out what happened. Every step emits structured events that can be queried, aggregated, and alerted on.

### 7. Built-in shadow mode

```bash
python pipeline_v3/pipeline.py --shadow
```

Runs the full pipeline but writes outputs to `pipeline_v3_shadow/` instead of production paths. Then runs comparison:
- WTI: for each park-date, compare v3 WTI vs production WTI. Report MAE between them.
- Forecasts: for each entity-date, compare predicted_actual. Report correlation and drift.
- If v3 is strictly better on accuracy, recommend swap.

---

## Methodology Improvements (baked into v3)

### A. inverse_freq weighting (won the experiment, never deployed)
v3 default: `inverse_freq` weighting for real vs synthetic actuals.
MAE improvement: 6.96 vs 7.04 (current production).

### B. Per-entity synthetic quality scoring
For each entity, compute synthetic bias = mean(synthetic_actual - real_actual).
If |bias| > 3 min → train that entity on real_only.
Est. ~5% MAE improvement for ~40% of entities.

### C. Drop MAPE, use MAE + bias + sMAPE
MAPE is broken when actuals are near zero (91% nonsense number).
v3 reports: MAE, bias, RMSE, and optionally sMAPE.

### D. Quantile mapping with guardrails
Current quantile mapping can amplify errors (IA overprediction hypothesis).
v3 adds: maximum stretch factor per park. If mapping would change a value by >50%, cap it and flag for review.

---

## Transition Plan

### Phase 1: Build (1-2 weeks)
- Write `pipeline_v3/` in a branch
- Implement steps s01-s12
- Write tests for critical logic (WTI calculation, park codes, training)

### Phase 2: Shadow (1 week)
- Deploy on wilma-server alongside production pipeline
- Run daily in shadow mode: `python pipeline_v3/pipeline.py --shadow`
- Compare WTI and forecast outputs daily
- Iterate on any discrepancies

### Phase 3: Validate (1 week)
- Shadow outputs must match or beat production accuracy for 7 consecutive days
- All edge cases handled (EU new entities, BB ignored, Tokyo timezone, etc.)
- Wilma and Gazoo confirm operational readiness

### Phase 4: Swap
- Point `run_daily_pipeline.sh` at v3 (or replace cron entirely)
- Keep v2 scripts in `scripts_v2_archive/` for rollback
- Monitor for 1 week, then clean up

---

## What Barney Writes vs What Wilma Deploys

**Barney writes**: All code in `pipeline_v3/`. Every commit reviewed for methodology correctness.
**Wilma deploys**: Sets up cron, manages server paths, handles operational issues.
**Gazoo validates**: Compares v3 shadow outputs against production independently.

This is the clean separation: brain (Barney) → hands (Wilma) → auditor (Gazoo).

---

## Open Questions

1. **Do we keep Julia training as a fallback?** My recommendation: no. Python XGBoost is the same algorithm. But if Julia training is measurably faster for the full entity set, we could keep it as an option.

2. **Should v3 run in the same repo or a new one?** Same repo (`pipeline_v3/` directory) keeps everything together and makes shadow comparison easier. New repo is cleaner but harder to share data paths.

3. **Forecast horizon**: 365 days is the current stopgap. Does the product actually need >365? If yes, v3's per-park architecture can handle 730 days at ~4-5GB peak (vs 49GB currently).

4. **Real-time inference**: The current `test_live_inference.py` suggests live prediction capability. Should v3 include a lightweight inference server, or is batch-only sufficient?

---

*Barney — Chief of Pipeline. This is the pipeline I'd build if I were starting from scratch with everything I know about the current system's strengths and weaknesses. 🪨*
