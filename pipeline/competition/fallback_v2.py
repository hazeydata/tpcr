"""fallback-v2 challenger: single-rider parent model plus fallback ratios.

TPCR-S8-FALLBACK-001 Phases 2-3, built as a challenger under
TPCR-SHADOW-REVIVE-001 (AC8) instead of in production (R1). It lives in the
competition namespace only:

  * reads production forecasts, dimentity and fact tables read-only
  * writes under <competition base>/forecasts/fallback-v2/ and
    <competition base>/state/ (single_rider_parent.csv, fallback_ratios.json)
  * must never be imported by pipeline/steps or the production pipeline

Forecast frame (same schema as the other challengers):
    entity_code, park_date, time_slot, predicted_actual, prediction_method

Rules, applied to a copy of the production forecast:
  * single-rider child with a mapped parent -> parent production forecast *
    scale_factor, method "parent_scaled". A child whose parent has no forecast
    is skipped (OQ3 default), never sent to the ratio.
  * any other row production served by ratio -> posted * the entity / category /
    global ratio, method "ratio_<level>".
  * everything else is copied unchanged, so non-fallback scores tie production.

Ratios are ACTUAL / POSTED medians. When the production forecast has no
posted_time column, posted is recovered as prod_pred / production ratio, which
is accurate to the rounding of the production integer prediction.

CLI:
    python -m pipeline.competition.fallback_v2 --output-base ~/hazeydata/pipeline
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.competition.fallback_entities import is_ratio_method, is_single_rider
from pipeline.competition.io_utils import atomic_write_csv, atomic_write_json, atomic_write_parquet

log = logging.getLogger(__name__)

NAME = "fallback-v2"
WINDOW_DAYS = 365
MIN_PAIRS = 30          # single-rider scale factor floor
MIN_OBS = 30            # entity / category ratio floor
CATEGORIES = ("show", "parade", "waypoint", "hhn_house")
DEFAULT_GLOBAL_RATIO = 0.678
PARENT_COLUMNS = [
    "child_entity_code", "parent_entity_code", "scale_factor",
    "scale_source", "n_obs", "computed_at",
]
FORECAST_COLUMNS = ["entity_code", "park_date", "time_slot", "predicted_actual", "prediction_method"]


# --------------------------------------------------------------------------
# Entity helpers
# --------------------------------------------------------------------------

def _entities_frame(entities_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dimentity to code, name, park, category_hint columns."""
    if entities_df is None or len(entities_df) == 0:
        return pd.DataFrame(columns=["code", "name", "park", "hint"])
    code_col = "code" if "code" in entities_df.columns else "entity_code"
    name_col = "name" if "name" in entities_df.columns else "entity_name"
    codes = entities_df[code_col].astype(str)
    park = entities_df["park_code"].astype(str).str.upper() if "park_code" in entities_df.columns \
        else codes.str[:2]
    hint = pd.Series("", index=entities_df.index)
    for col in ("category", "entity_type"):
        if col in entities_df.columns:
            hint = hint + " " + entities_df[col].fillna("").astype(str)
    return pd.DataFrame({
        "code": codes.values,
        "name": entities_df[name_col].fillna("").astype(str).values,
        "park": park.values,
        "hint": hint.str.lower().str.strip().values,
    })


def base_name(name: str) -> str:
    """Name with "single rider" removed, lowercased, punctuation and spacing normalized."""
    s = re.sub(r"single[\s\-]*rider", " ", str(name).lower())
    s = re.sub(r"[()\[\]\-–—:,]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def entity_category(name: str, hint: str = "") -> str | None:
    """show / parade / waypoint / hhn_house from an explicit category or the name.

    dimentity has no documented category column, so this is a heuristic on an
    optional `category` / `entity_type` column and the entity name. None means
    no category (the entity falls through to the global ratio).
    """
    hint = (hint or "").strip()
    if hint in CATEGORIES:
        return hint
    text = f"{hint} {str(name).lower()}"
    if "hhn" in text or "horror nights" in text:
        return "hhn_house"
    if "parade" in text:
        return "parade"
    if "waypoint" in text:
        return "waypoint"
    if re.search(r"\bshow\b|fireworks|spectacular|nighttime", text):
        return "show"
    return None


# --------------------------------------------------------------------------
# Single-rider parent model (s8spec AC6-AC8)
# --------------------------------------------------------------------------

def map_single_riders(entities_df: pd.DataFrame) -> tuple[dict, list]:
    """child -> parent mapping (park + base name), plus named failures.

    Returns ({child: parent}, [(child, "no_parent" | "ambiguous", detail)]).
    """
    ents = _entities_frame(entities_df)
    single = {c for c in ents["code"] if is_single_rider(c, entities_df)}
    parents: dict[tuple, list] = {}
    for row in ents.itertuples():
        if row.code not in single:
            parents.setdefault((row.park, base_name(row.name)), []).append(row.code)

    mapping, failures = {}, []
    for row in ents.itertuples():
        if row.code not in single:
            continue
        cands = parents.get((row.park, base_name(row.name)), [])
        if len(cands) == 1:
            mapping[row.code] = cands[0]
        elif not cands:
            failures.append((row.code, "no_parent", row.name))
        else:
            failures.append((row.code, "ambiguous", ",".join(sorted(cands))))
    return mapping, failures


def _window(df: pd.DataFrame, today: date, window_days: int) -> pd.DataFrame:
    d = pd.to_datetime(df["park_date"]).dt.date
    return df[(d > today - timedelta(days=window_days)) & (d <= today)]


def build_single_rider_parent(posted_df, entities_df, today: date,
                              window_days: int = WINDOW_DAYS, min_pairs: int = MIN_PAIRS):
    """One row per single-rider child: parent, scale factor, provenance.

    posted_df: entity_code, park_date, time_slot, posted. The scale is the
    median of child_posted / parent_posted over same date+slot pairs in the last
    `window_days`, with at least `min_pairs` pairs, else 1.0 (scale_source
    "default", logged). Children with no unique non-single-rider parent are
    kept in the frame as named failures: empty parent, NaN scale, scale_source
    "unmapped:no_parent" / "unmapped:ambiguous".
    """
    mapping, failures = map_single_riders(entities_df)
    computed_at = today.isoformat()
    window = _window(posted_df, today, window_days) if len(posted_df) else posted_df
    by_entity = {e: g for e, g in window.groupby("entity_code")} if len(window) else {}

    rows = []
    for child, parent in sorted(mapping.items()):
        c, p = by_entity.get(child), by_entity.get(parent)
        n, scale = 0, np.nan
        if c is not None and p is not None:
            pairs = c.merge(p, on=["park_date", "time_slot"], suffixes=("_c", "_p"))
            pairs = pairs[pairs["posted_p"] > 0]
            n = len(pairs)
            if n >= min_pairs:
                scale = float((pairs["posted_c"] / pairs["posted_p"]).median())
        if n >= min_pairs:
            rows.append((child, parent, round(scale, 4), "observed", n, computed_at))
        else:
            log.info("single-rider %s -> %s: %d pairs < %d, scale_factor=1.0 (default)",
                     child, parent, n, min_pairs)
            rows.append((child, parent, 1.0, "default", n, computed_at))

    for child, kind, detail in failures:
        log.warning("single-rider %s unmapped (%s): %s", child, kind, detail)
        rows.append((child, "", np.nan, f"unmapped:{kind}", 0, computed_at))

    return pd.DataFrame(rows, columns=PARENT_COLUMNS)


def unmapped_children(parent_map: pd.DataFrame) -> list[str]:
    return sorted(parent_map.loc[parent_map["scale_source"].str.startswith("unmapped"),
                                 "child_entity_code"])


# --------------------------------------------------------------------------
# Fallback ratios (s8spec AC9)
# --------------------------------------------------------------------------

def build_fallback_ratios(pairs_df, entities_df, today: date,
                          window_days: int = WINDOW_DAYS, min_obs: int = MIN_OBS) -> dict:
    """Ratios per entity at level entity / category / global.

    pairs_df: entity_code, park_date, posted, actual (same date + slot).
    ratio = median(actual / posted) over the last `window_days`. Levels:
      entity   : the entity has >= min_obs observations
      category : else its category (show, parade, waypoint, hhn_house) has >= min_obs
      global   : else everything (default 0.678 when there is no data at all)
    Entries exist for every entity in dimentity and every entity in pairs_df,
    plus "_category" and "_global".
    """
    window_str = f"{today - timedelta(days=window_days)}..{today}"
    pairs = _window(pairs_df, today, window_days) if len(pairs_df) else pairs_df
    if len(pairs):
        pairs = pairs[(pairs["posted"] > 0) & pairs["actual"].notna()].copy()
        pairs["ratio"] = pairs["actual"] / pairs["posted"]

    ents = _entities_frame(entities_df)
    cat_of = {r.code: entity_category(r.name, r.hint) for r in ents.itertuples()}
    universe = set(ents["code"]) | (set(pairs["entity_code"]) if len(pairs) else set())

    def stat(sub):
        return {"ratio": round(float(sub["ratio"].median()), 4), "n_obs": int(len(sub)), "window": window_str}

    if len(pairs):
        global_entry = stat(pairs)
        entity_stats = {e: stat(g) for e, g in pairs.groupby("entity_code")}
        pairs["category"] = pairs["entity_code"].map(cat_of)
        cat_stats = {c: stat(g) for c, g in pairs.dropna(subset=["category"]).groupby("category")}
    else:
        global_entry = {"ratio": DEFAULT_GLOBAL_RATIO, "n_obs": 0, "window": window_str}
        entity_stats, cat_stats = {}, {}
    cat_stats = {c: s for c, s in cat_stats.items() if s["n_obs"] >= min_obs}

    out: dict = {}
    for e in sorted(universe):
        own = entity_stats.get(e)
        cat = cat_of.get(e)
        if own and own["n_obs"] >= min_obs:
            out[e] = {**own, "level": "entity"}
        elif cat in cat_stats:
            out[e] = {**cat_stats[cat], "level": "category"}
        else:
            out[e] = {**global_entry, "level": "global"}
    out["_category"] = cat_stats
    out["_global"] = global_entry
    return out


# --------------------------------------------------------------------------
# Forecast
# --------------------------------------------------------------------------

def _lookup_ratio(entity: str, ratios: dict, cat_of: dict) -> tuple[float, str]:
    entry = ratios.get(entity)
    if isinstance(entry, dict) and "ratio" in entry:
        return float(entry["ratio"]), entry.get("level", "entity")
    cat = cat_of.get(entity)
    cat_entry = ratios.get("_category", {}).get(cat) if cat else None
    if cat_entry:
        return float(cat_entry["ratio"]), "category"
    glob = ratios.get("_global")
    if glob:
        return float(glob["ratio"]), "global"
    return DEFAULT_GLOBAL_RATIO, "global"


def forecast_fallback_v2(prod_forecast_df, parent_map, ratios, entities, prod_ratios=None):
    """Challenger forecast frame (FORECAST_COLUMNS) built from the production forecast."""
    prod = prod_forecast_df[FORECAST_COLUMNS + (
        ["posted_time"] if "posted_time" in prod_forecast_df.columns else [])].copy()
    parts = []
    key = ["park_date", "time_slot"]

    # 1. single-rider children: parent forecast * scale_factor
    mapped = parent_map[parent_map["parent_entity_code"].astype(str) != ""]
    children = {r.child_entity_code: (r.parent_entity_code, float(r.scale_factor))
                for r in mapped.itertuples()}
    present = set(prod["entity_code"].unique())
    by_entity = {e: g for e, g in prod[prod["entity_code"].isin(
        present & (set(children) | {p for p, _ in children.values()}))].groupby("entity_code")}
    skipped = []
    for child, (parent, scale) in sorted(children.items()):
        if child not in present:
            continue
        pf = by_entity.get(parent)
        if pf is None or len(pf) == 0:
            skipped.append((child, parent))
            continue
        rows = by_entity[child][["entity_code"] + key].merge(
            pf[key + ["predicted_actual"]], on=key, how="inner")
        rows["predicted_actual"] = np.clip(np.round(rows["predicted_actual"] * scale), 0, 300).astype(int)
        rows["prediction_method"] = "parent_scaled"
        parts.append(rows[FORECAST_COLUMNS])
    for child, parent in skipped:
        log.warning("fallback-v2: child %s skipped, parent %s has no forecast", child, parent)
    rest = prod[~prod["entity_code"].isin(children)]

    # 2. ratio-served rows: posted * entity / category / global ratio
    ratio_mask = rest["prediction_method"].map(is_ratio_method).astype(bool)
    ratio_rows = rest[ratio_mask].copy()
    if len(ratio_rows):
        ents = _entities_frame(entities)
        cat_of = {r.code: entity_category(r.name, r.hint) for r in ents.itertuples()}
        looked = {e: _lookup_ratio(e, ratios, cat_of) for e in ratio_rows["entity_code"].unique()}
        new_ratio = ratio_rows["entity_code"].map(lambda e: looked[e][0])
        if "posted_time" in ratio_rows.columns:
            posted = ratio_rows["posted_time"].astype(float)
        else:
            prod_ratios = prod_ratios or {}
            old = ratio_rows["entity_code"].map(
                lambda e: float(prod_ratios.get(e, prod_ratios.get("__global__", DEFAULT_GLOBAL_RATIO))))
            posted = ratio_rows["predicted_actual"] / old
        ratio_rows["predicted_actual"] = np.clip(np.round(posted * new_ratio), 0, 300).astype(int)
        ratio_rows["prediction_method"] = ratio_rows["entity_code"].map(lambda e: f"ratio_{looked[e][1]}")
        parts.append(ratio_rows[FORECAST_COLUMNS])
        for e, (r, level) in sorted(looked.items()):
            log.info("fallback-v2 ratio %s: %.4f (%s)", e, r, level)

    # 3. everything else is production, unchanged
    parts.append(rest[~ratio_mask][FORECAST_COLUMNS])

    out = pd.concat(parts, ignore_index=True)
    return out.sort_values(["entity_code", "park_date", "time_slot"]).reset_index(drop=True)


# --------------------------------------------------------------------------
# Data loading + run (duckdb, only used by the CLI / orchestrator)
# --------------------------------------------------------------------------

def _fact_files(output_base: Path, start: date) -> list[str]:
    files = sorted(
        f for f in (output_base / "fact_tables" / "parquet").glob("*.parquet")
        if f.name >= f"{start:%Y-%m}"
    )
    return [str(f) for f in files]


_SLOT = ("TIME_BUCKET(INTERVAL '5 minutes', "
         "(observed_at_ts::TIMESTAMP + INTERVAL '2 minutes 30 seconds'))::TIME")


def load_posted(output_base: Path, start: date, today: date, entities: set[str]) -> pd.DataFrame:
    """POSTED waits for `entities`: entity_code, park_date, time_slot, posted (5-minute slots)."""
    import duckdb

    files = _fact_files(output_base, start)
    if not files or not entities:
        return pd.DataFrame(columns=["entity_code", "park_date", "time_slot", "posted"])
    ent_list = "', '".join(sorted(entities))
    glob = "', '".join(files)
    return duckdb.connect().execute(f"""
        SELECT entity_code, park_date::VARCHAR AS park_date, {_SLOT} AS time_slot,
               AVG(wait_time_minutes) AS posted
        FROM read_parquet(['{glob}'])
        WHERE wait_time_type = 'POSTED' AND wait_time_minutes > 0
          AND entity_code IN ('{ent_list}')
          AND park_date::VARCHAR > '{start}' AND park_date::VARCHAR <= '{today}'
        GROUP BY ALL
    """).fetchdf()


def load_ratio_pairs(output_base: Path, start: date, today: date) -> pd.DataFrame:
    """ACTUAL and POSTED on the same date + 5-minute slot: entity_code, park_date, posted, actual."""
    import duckdb

    files = _fact_files(output_base, start)
    if not files:
        return pd.DataFrame(columns=["entity_code", "park_date", "posted", "actual"])
    glob = "', '".join(files)
    return duckdb.connect().execute(f"""
        WITH w AS (
            SELECT entity_code, park_date::VARCHAR AS park_date, wait_time_type,
                   {_SLOT} AS time_slot, AVG(wait_time_minutes) AS wait
            FROM read_parquet(['{glob}'])
            WHERE wait_time_type IN ('POSTED', 'ACTUAL')
              AND park_date::VARCHAR > '{start}' AND park_date::VARCHAR <= '{today}'
            GROUP BY ALL
        )
        SELECT a.entity_code, a.park_date, p.wait AS posted, a.wait AS actual
        FROM w a JOIN w p USING (entity_code, park_date, time_slot)
        WHERE a.wait_time_type = 'ACTUAL' AND p.wait_time_type = 'POSTED' AND p.wait > 0
    """).fetchdf()


def run_fallback_v2(output_base: Path, run_date: date | None = None) -> dict:
    """Build parent map + ratios + forecast. Writes only under <output_base>/competition/."""
    output_base = Path(output_base)
    comp = output_base / "competition"
    today = run_date or date.today()
    start = today - timedelta(days=WINDOW_DAYS)

    prod = pd.read_parquet(output_base / "curves" / "forecast_parquet" / "all_forecasts.parquet")
    entities = pd.read_csv(output_base / "dimension_tables" / "dimentity.csv")
    prod_ratios_path = output_base / "state" / "fallback_ratios.json"   # read-only
    prod_ratios = json.loads(prod_ratios_path.read_text()) if prod_ratios_path.exists() else {}

    mapping, _ = map_single_riders(entities)
    posted = load_posted(output_base, start, today, set(mapping) | set(mapping.values()))
    parent_map = build_single_rider_parent(posted, entities, today)
    ratios = build_fallback_ratios(load_ratio_pairs(output_base, start, today), entities, today)

    atomic_write_csv(parent_map, comp / "state" / "single_rider_parent.csv")
    atomic_write_json(comp / "state" / "fallback_ratios.json", ratios)

    forecast = forecast_fallback_v2(prod, parent_map, ratios, entities, prod_ratios)
    out_dir = comp / "forecasts" / NAME
    atomic_write_parquet(forecast, out_dir / f"all_forecasts_{NAME}.parquet")

    counts = forecast["prediction_method"].value_counts().to_dict()
    log.info("fallback-v2 forecast: %d rows, %s", len(forecast), counts)
    return {
        "challenger_name": NAME,
        "rows": int(len(forecast)),
        "methods": {k: int(v) for k, v in counts.items()},
        "children_mapped": int((parent_map["parent_entity_code"] != "").sum()),
        "children_unmapped": unmapped_children(parent_map),
    }


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    p = argparse.ArgumentParser(description="fallback-v2 challenger forecast (competition namespace only)")
    p.add_argument("--output-base", type=Path, required=True)
    p.add_argument("--run-date", type=date.fromisoformat, default=None)
    args = p.parse_args()
    try:
        result = run_fallback_v2(args.output_base, args.run_date)
    except Exception as e:
        print(f"fallback-v2 failed: {e}")
        return 1
    print("RESULT:" + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
