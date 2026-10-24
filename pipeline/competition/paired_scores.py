"""Paired scoring: production and challenger on the identical (entity, slot) set.

Pure pandas, no duckdb. A pair is scored only when production prediction,
challenger prediction and an actual all exist. Everything else is counted by
reason and logged.

Inputs are three frames keyed by (entity_code, slot):
    prod_df   : entity_code, slot, prod_pred
    chal_df   : entity_code, slot, chal_pred
    actual_df : entity_code, slot, actual
"""

from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

SCORE_COLUMNS = [
    "date", "challenger_id", "entity_code", "slot",
    "actual", "prod_pred", "chal_pred", "is_fallback_entity",
]
DROP_REASONS = ("no_actual", "no_prod_pred", "no_chal_pred")
KEY = ["entity_code", "slot"]


def _keyed(df: pd.DataFrame, value_col: str, label: str) -> pd.DataFrame:
    """Reduce a frame to unique (entity_code, slot) rows with one non-null value."""
    if df is None or len(df) == 0:
        return pd.DataFrame({
            "entity_code": pd.Series([], dtype=object),
            "slot": pd.Series([], dtype=object),
            value_col: pd.Series([], dtype=float),
        })
    out = df[KEY + [value_col]].dropna(subset=[value_col])
    dupes = int(out.duplicated(KEY).sum())
    if dupes:
        log.warning("%s: %d duplicate (entity, slot) rows, keeping first", label, dupes)
        out = out.drop_duplicates(KEY, keep="first")
    return out


def build_paired_scores(prod_df, chal_df, actual_df, challenger_id, date, fallback_entities):
    """Return (scores_df, dropped).

    scores_df has exactly SCORE_COLUMNS, one row per pair present on all three
    sides. dropped counts every other (entity, slot) seen on any side under the
    first missing item in the order no_actual, no_prod_pred, no_chal_pred.
    """
    prod = _keyed(prod_df, "prod_pred", "prod")
    chal = _keyed(chal_df, "chal_pred", "chal")
    act = _keyed(actual_df, "actual", "actual")

    merged = pd.concat([prod[KEY], chal[KEY], act[KEY]], ignore_index=True).drop_duplicates()
    merged = (
        merged.merge(act, on=KEY, how="left")
        .merge(prod, on=KEY, how="left")
        .merge(chal, on=KEY, how="left")
    )

    no_actual = merged["actual"].isna()
    no_prod = ~no_actual & merged["prod_pred"].isna()
    no_chal = ~no_actual & ~no_prod & merged["chal_pred"].isna()
    dropped = {
        "no_actual": int(no_actual.sum()),
        "no_prod_pred": int(no_prod.sum()),
        "no_chal_pred": int(no_chal.sum()),
    }

    scores = merged[~(no_actual | no_prod | no_chal)].copy()
    fallback = set(fallback_entities or ())
    scores["date"] = str(date)
    scores["challenger_id"] = challenger_id
    scores["is_fallback_entity"] = scores["entity_code"].isin(fallback)
    scores = scores[SCORE_COLUMNS].sort_values(KEY).reset_index(drop=True)

    if any(dropped.values()):
        log.info(
            "paired scores %s %s: matched=%d dropped no_actual=%d no_prod_pred=%d no_chal_pred=%d",
            challenger_id, date, len(scores),
            dropped["no_actual"], dropped["no_prod_pred"], dropped["no_chal_pred"],
        )
    return scores, dropped


def _entity_weighted_mae(df: pd.DataFrame, pred_col: str):
    """Mean of per-entity MAE (each entity weighted equally); None if no rows."""
    if len(df) == 0:
        return None
    per_entity = (df[pred_col] - df["actual"]).abs().groupby(df["entity_code"]).mean()
    return float(per_entity.mean())


def _dropped_for(dropped: dict, challenger_id: str) -> dict:
    """Accept flat {reason: n} or nested {challenger_id: {reason: n}}."""
    if dropped and any(isinstance(v, dict) for v in dropped.values()):
        return dict(dropped.get(challenger_id, {}))
    return dict(dropped or {})


def summarize(scores_df, dropped, forecast_entities) -> dict:
    """Summarize paired scores.

    forecast_entities: production forecast entity count, or a collection of codes.
    Top level matched_pairs is the largest per-challenger pair count and
    coverage_pct the smallest per-challenger coverage (conservative).
    """
    n_forecast = (
        int(forecast_entities) if isinstance(forecast_entities, (int, float))
        else len(set(forecast_entities))
    )
    challengers: dict[str, dict] = {}
    matched = 0
    coverage = None

    groups = scores_df.groupby("challenger_id") if len(scores_df) else []
    for cid, grp in groups:
        fb = grp[grp["is_fallback_entity"].astype(bool)]
        n_ent = int(grp["entity_code"].nunique())
        cov = round(n_ent / n_forecast * 100, 2) if n_forecast else 0.0
        challengers[cid] = {
            "matched_pairs": int(len(grp)),
            "n_entities": n_ent,
            "coverage_pct": cov,
            "mae_prod": _entity_weighted_mae(grp, "prod_pred"),
            "mae_chal": _entity_weighted_mae(grp, "chal_pred"),
            "mae_fallback_prod": _entity_weighted_mae(fb, "prod_pred"),
            "mae_fallback_chal": _entity_weighted_mae(fb, "chal_pred"),
            "n_fallback_entities": int(fb["entity_code"].nunique()),
            "dropped": _dropped_for(dropped, cid),
        }
        matched = max(matched, len(grp))
        coverage = cov if coverage is None else min(coverage, cov)

    return {
        "matched_pairs": int(matched),
        "forecast_entities": n_forecast,
        "coverage_pct": coverage if coverage is not None else 0.0,
        "challengers": challengers,
        "dropped": dict(dropped or {}),
    }
