# Theme Park Crowd Report — AI Agent Guide

## Critical: Read Before Writing Code

**Read `docs/PIPELINE_V4_DESIGN.md` first.** It defines the pipeline architecture, data flow, and quality gates.

## The #1 Rule

**All data access uses DuckDB + Parquet.** Never use CSV loops or `load_entity_data()`.

```python
import duckdb
con = duckdb.connect()
df = con.execute(f"""
    SELECT * FROM read_parquet('{output_base}/fact_tables/parquet/*.parquet')
    WHERE entity_code = 'MK01'
""").fetchdf()
```

## Project Overview

- **What:** Theme park wait time predictions (Disney, Universal)
- **Stack:** Python + DuckDB + XGBoost
- **Data:** Parquet fact tables in `/mnt/data/pipeline/fact_tables/parquet/`
- **Dimensions:** CSV in `/mnt/data/pipeline/dimension_tables/`
- **Models:** Per-entity XGBoost in `/mnt/data/pipeline/models/{entity_code}/`

## Key Files

- `pipeline/pipeline.py` — **Main pipeline entry point** (V4, runs daily at 6 AM ET)
- `pipeline/steps/s06b_training_data.py` — Rebuilds per-park actuals training data + extends encoding_mappings.json (before s07)
- `pipeline/steps/s07_training.py` — Per-entity XGBoost training (per-park geo-decay half-life: `geo_decay_halflife_days_per_park`)
- `pipeline/steps/s07a_calendar.py` — Refreshes closures, dimentity, operating calendar, encodings (fail-soft, before s08)
- `pipeline/backtest/decay_grid.py` — Half-life x min_training_year back-test (TPCR-BIAS-001 fix 4)
- `pipeline/steps/s14_content.py` — Content generation + quality gate for tweets
- `src/processors/encoding.py` — Label encoding
- `src/processors/posted_to_actual.py` — POSTED to ACTUAL conversion (DuckDB)
- `docs/PIPELINE_V4_DESIGN.md` — Governing pipeline spec
- `docs/MODELING_AND_WTI_METHODOLOGY.md` — Full methodology docs
- `SESSION_LOG.md` — Source of truth for current project state

## Memory Limits

Server: 62GB RAM. Chunk by park prefix if processing > 30M rows.
