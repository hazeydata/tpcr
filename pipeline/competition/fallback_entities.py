"""Which entities count as "fallback" entities for scoring (TPCR-SHADOW-REVIVE-001 AC7).

An entity is a fallback entity when it is a single-rider queue, or production
served it by ratio. Production S8 tags ratio-served rows with
prediction_method == "fallback_ratio"; challenger forecasters use
"fallback_ratio_no_model" / "fallback_ratio_missing_features", and fallback-v2
uses "ratio_<level>". All of those are ratio-served.
"""

from __future__ import annotations

import pandas as pd

RATIO_METHOD_PREFIXES = ("fallback_ratio", "ratio_")
SINGLE_RIDER_TEXT = "single rider"


def entity_names(entities) -> dict[str, str]:
    """Map entity_code -> name from a dimentity frame (code|entity_code, name) or a dict."""
    if entities is None:
        return {}
    if isinstance(entities, dict):
        return {str(k): str(v) for k, v in entities.items()}
    code_col = "code" if "code" in entities.columns else "entity_code"
    name_col = "name" if "name" in entities.columns else "entity_name"
    return {
        str(c): ("" if pd.isna(n) else str(n))
        for c, n in zip(entities[code_col], entities[name_col])
    }


def is_single_rider(entity_code: str, entities=None) -> bool:
    """True for a single-rider queue.

    Uses an explicit boolean `is_single_rider` column when the dimentity frame
    has one, else the entity name containing "single rider" (case-insensitive).
    """
    if entities is not None and not isinstance(entities, dict) \
            and "is_single_rider" in entities.columns:
        code_col = "code" if "code" in entities.columns else "entity_code"
        row = entities.loc[entities[code_col] == entity_code, "is_single_rider"]
        if len(row) and pd.notna(row.iloc[0]):
            return bool(row.iloc[0])
    return SINGLE_RIDER_TEXT in entity_names(entities).get(entity_code, "").lower()


def is_ratio_method(method) -> bool:
    return isinstance(method, str) and method.startswith(RATIO_METHOD_PREFIXES)


def ratio_served_entities(prod_df) -> set[str]:
    """Entities with any production row served by ratio."""
    if prod_df is None or len(prod_df) == 0 or "prediction_method" not in prod_df.columns:
        return set()
    mask = prod_df["prediction_method"].map(is_ratio_method)
    return set(prod_df.loc[mask, "entity_code"].unique())


def fallback_entities(prod_df, entities=None) -> set[str]:
    """Single-rider queues plus production ratio-served entities.

    Single-rider detection is limited to entities in the production forecast so
    the set matches what is scored.
    """
    codes = set(prod_df["entity_code"].unique()) if prod_df is not None and len(prod_df) else set()
    single = {c for c in codes if is_single_rider(c, entities)}
    return single | ratio_served_entities(prod_df)
