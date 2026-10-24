"""Challenger: fallback-v2

TPCR-S8-FALLBACK-001 Phases 2-3 as a challenger (TPCR-SHADOW-REVIVE-001 AC8).
Not an XGBoost challenger: it is not retrained, and rolling_shadow dispatches it
to pipeline.competition.fallback_v2 instead of train/forecast_challenger.

Changes from production (fallback entities only; all other rows are copied):
- single-rider queues: parent ride forecast * median(child/parent posted) scale
- ratio-served entities: per-entity / category / global ACTUAL/POSTED ratio
  instead of the single global 0.678

Scored mainly on fallback entities; all-entity MAE is also reported so a
no-regression check is possible (OQ3).
"""

NAME = "fallback-v2"
STATUS = "active"
SCORE_SCOPE = "fallback"
DESCRIPTION = "Single-rider parent model plus per-entity/category fallback ratios (competition only)"
DATE_REGISTERED = "2026-10-24"

HYPERPARAMS = {}  # no model: nothing to train
FEATURES = None
