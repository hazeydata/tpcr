"""Pipeline steps — one module per stage.

Each step exports a `run(cfg, log)` function that:
1. Validates its inputs
2. Does its work
3. Validates its outputs
4. Returns a summary dict

Steps are executed in order by pipeline.py.
s06b_training_data rebuilds matched_pairs/actuals_training_v2 so s07 trains on
current data. s07a_calendar refreshes closures, dimentity, the operating calendar
and the encoding mappings before s08 forecasts; it fails soft (status degraded,
previous files kept).
s13_report is NOT in STEP_ORDER — it runs as a standalone post-run script
after the pipeline completes and the metrics JSON is fully written.
"""

# Step execution order (s13_report excluded — runs post-pipeline)
STEP_ORDER = [
    "s01_sync",
    "s02_etl",
    "s03_dimensions",
    "s04_aggregates",
    "s05_conversion",
    "s06_synthetic",
    "s06b_training_data",
    "s07_training",
    "s07a_calendar",
    "s08_forecast",
    "s09_wti",
    "s10_accuracy",
    "s11_deploy",
    "s12_validate",
    "s14_content",
]
