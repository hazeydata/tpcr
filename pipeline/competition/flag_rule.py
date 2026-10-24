"""Flag rule (TPCR-SHADOW-REVIVE-001 AC12): when a challenger earns the `flagged` status.

A challenger is flagged only when ALL hold:
  (a) win streak of at least MIN_STREAK consecutive days ending on the evaluated
      date, a win being mae_chal < mae_prod on that day's matched (entity, slot) set;
  (b) a paired per-entity bootstrap of the MAE difference whose 95% percentile CI
      is entirely in the challenger's favor (ci95_low > 0 for gain = prod - chal);
  (c) gain_min = mae_prod - mae_chal >= MIN_GAIN_MIN minutes.

A flag changes nothing in production. numpy only; no I/O in this module.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

MIN_STREAK = 7
BOOTSTRAP_RESAMPLES = 10_000
MIN_GAIN_MIN = 0.3
# Float MAEs make an exact 0.3 gain land just under it (5.3 - 5.0 = 0.29999999999999982),
# so the gain comparison allows this tolerance.
GAIN_EPSILON = 1e-9
BOOTSTRAP_SEED = 20261024
CI_LEVEL = 95
STREAK_LOOKBACK_DAYS = 60  # streak lookback cap (the rule only needs MIN_STREAK)
_CHUNK = 1000  # resamples per draw, bounds memory; chunking does not change the stream


def per_entity_gain(entity_code, actual, prod_pred, chal_pred):
    """Per-entity mean of (|actual - prod| - |actual - chal|) over the matched pairs.

    Positive = challenger better. Returns a float array, one value per entity.
    """
    entity_code = np.asarray(entity_code)
    err_prod = np.abs(np.asarray(actual, dtype=float) - np.asarray(prod_pred, dtype=float))
    err_chal = np.abs(np.asarray(actual, dtype=float) - np.asarray(chal_pred, dtype=float))
    _, inverse = np.unique(entity_code, return_inverse=True)
    inverse = inverse.reshape(-1)
    counts = np.bincount(inverse)
    return np.bincount(inverse, weights=err_prod - err_chal) / counts


def bootstrap_ci(gains, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED, level=CI_LEVEL):
    """Percentile CI of the mean of `gains`, resampling entities with replacement.

    Deterministic for a given (gains, resamples, seed). Returns (low, high), or
    (None, None) when there are no entities.
    """
    gains = np.asarray(gains, dtype=float)
    n = len(gains)
    if n == 0:
        return None, None
    rng = np.random.default_rng(seed)
    means = np.empty(resamples)
    for start in range(0, resamples, _CHUNK):
        size = min(_CHUNK, resamples - start)
        means[start:start + size] = gains[rng.integers(0, n, size=(size, n))].mean(axis=1)
    tail = (100 - level) / 2
    low, high = np.percentile(means, [tail, 100 - tail])
    return float(low), float(high)


def is_win(mae_prod, mae_chal):
    """Daily win: challenger strictly better on the matched set."""
    return mae_prod is not None and mae_chal is not None and mae_chal < mae_prod


def gain_min(mae_prod, mae_chal):
    if mae_prod is None or mae_chal is None:
        return None
    return mae_prod - mae_chal


def streak_length(eval_date, win_today, load_summary, challenger_id):
    """Consecutive winning days ending on eval_date (YYYY-MM-DD).

    load_summary(date_str) -> that day's summary dict, or None when missing.
    A day counts only when its summary has run_status "ok" and the challenger's
    block has win true. A missing, skipped (skipped_*), failed or no_data day
    breaks the streak. Today's own win comes from win_today, and the caller
    passes win_today=False for a day whose run is not ok.
    """
    if not win_today:
        return 0
    streak = 1
    day = date.fromisoformat(eval_date)
    for _ in range(STREAK_LOOKBACK_DAYS):
        day -= timedelta(days=1)
        summary = load_summary(day.isoformat())
        if not summary or summary.get("run_status") != "ok":
            break
        block = (summary.get("challengers") or {}).get(challenger_id) or {}
        if block.get("win") is not True:
            break
        streak += 1
    return streak


def should_flag(win, streak, ci95_low, gain):
    """All three conditions: streak, CI strictly favorable, gain at least MIN_GAIN_MIN."""
    return bool(
        win
        and streak >= MIN_STREAK
        and ci95_low is not None and ci95_low > 0
        and gain is not None and gain >= MIN_GAIN_MIN - GAIN_EPSILON
    )


def significant_gain_text(flagged_ids):
    ids = list(flagged_ids)
    return "Significant gain: " + ", ".join(ids) if ids else "None yet"
