"""Per-entity bias guard (TPCR-AK-BIAS-001 AC4).

An entity is guarded when, over the trailing window of model-produced slots,
|bias| > bias_threshold_min AND |MAE - |bias|| <= mae_tolerance * |bias|.
Guarded entities are forecast with the fallback ratio path in s08, reason
`bias_guard`. The guard never alters forecasts in any other way.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

REASON = "bias_guard"
WINDOW_DAYS = 30
BIAS_THRESHOLD_MIN = 20.0
MAE_TOLERANCE = 0.10
GUARD_FILE = "bias_guard.json"
LOG_FILE = "bias_guard_log.jsonl"


def evaluate(slot_df: pd.DataFrame, as_of: date, previous: dict | None = None) -> dict:
    previous = previous or {}
    start = (as_of - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    end = as_of.isoformat()
    df = slot_df[(slot_df["park_date"].astype(str) >= start) & (slot_df["park_date"].astype(str) <= end)]
    df = df[df["prediction_method"] != "fallback_ratio"]
    df = (
        df.sort_values("evaluation_date")
        .drop_duplicates(subset=["entity_code", "park_date", "time_slot"], keep="last")
    )
    stats = df.groupby("entity_code").agg(
        n_slots=("signed_error", "size"),
        bias=("signed_error", "mean"),
        mae=("absolute_error", "mean"),
    )
    entities = {}
    for code, row in stats.iterrows():
        mag = abs(float(row["bias"]))
        guarded = mag > BIAS_THRESHOLD_MIN and abs(float(row["mae"]) - mag) <= MAE_TOLERANCE * mag
        entities[code] = {
            "guarded": bool(guarded),
            "bias": round(float(row["bias"]), 3),
            "mae": round(float(row["mae"]), 3),
            "n_slots": int(row["n_slots"]),
            "basis": "window",
        }
    for code, prev in previous.items():
        if code not in entities and prev.get("guarded"):
            entities[code] = dict(prev, basis="carried")
    return {
        "as_of": end,
        "window_start": start,
        "rule": {
            "window_days": WINDOW_DAYS,
            "bias_threshold_min": BIAS_THRESHOLD_MIN,
            "mae_tolerance": MAE_TOLERANCE,
        },
        "reason": REASON,
        "guarded": sorted(c for c, e in entities.items() if e["guarded"]),
        "entities": entities,
    }


def load(state_dir: Path) -> dict:
    try:
        return json.loads((Path(state_dir) / GUARD_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def guarded_entities(state_dir: Path) -> set[str]:
    return set(load(state_dir).get("guarded", []))


def save(result: dict, state_dir: Path, accuracy_dir: Path) -> None:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / GUARD_FILE
    tmp = path.with_name(f".{GUARD_FILE}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(result, indent=1, sort_keys=True))
    os.replace(tmp, path)
    accuracy_dir = Path(accuracy_dir)
    accuracy_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "as_of": result["as_of"],
        "reason": REASON,
        "guarded": result["guarded"],
        "stats": {c: result["entities"][c] for c in result["guarded"]},
    }
    with open(accuracy_dir / LOG_FILE, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
