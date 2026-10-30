"""Label encodings for the calendar features (date_group_id, season, season_year).

The mapping file state/encoding_mappings.json is append-only: existing codes
never change, so models trained on yesterday's codes still read today's file.
New labels found in the dimension tables get the next free integer, in sorted
order, so two refreshes of the same inputs write the same file.

An unknown label is an error. Nothing here defaults to code 0, because code 0
is a real label (AFTER_EASTER_2010).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

ENCODED_COLUMNS = ("date_group_id", "season", "season_year")


class UnmappedLabelError(ValueError):
    def __init__(self, unmapped: Mapping[str, list[str]]):
        self.unmapped = {k: sorted(v) for k, v in unmapped.items() if v}
        parts = [f"{col}: {vals[:10]}{' ...' if len(vals) > 10 else ''} ({len(vals)})"
                 for col, vals in self.unmapped.items()]
        super().__init__("unmapped encoding labels: " + "; ".join(parts))


def load_mappings(path: Path) -> dict[str, dict[str, int]]:
    with open(path) as f:
        raw = json.load(f)
    missing = [c for c in ENCODED_COLUMNS if c not in raw]
    if missing:
        raise ValueError(f"{path} lacks mapping(s) for {missing}")
    return {c: {str(k): int(v) for k, v in raw[c].items()} for c in ENCODED_COLUMNS} | {
        k: v for k, v in raw.items() if k not in ENCODED_COLUMNS
    }


def extend_mappings(
    mappings: Mapping[str, Mapping[str, int]],
    labels: Mapping[str, Iterable[str]],
) -> tuple[dict[str, dict[str, int]], dict[str, list[str]]]:
    extended = {k: dict(v) for k, v in mappings.items()}
    added: dict[str, list[str]] = {}
    for col in ENCODED_COLUMNS:
        current = extended.setdefault(col, {})
        new = sorted({str(x) for x in labels.get(col, ()) if pd.notna(x)} - set(current))
        next_code = max(current.values(), default=-1) + 1
        for offset, label in enumerate(new):
            current[label] = next_code + offset
        added[col] = new
    return extended, added


def encode(values: pd.Series, mapping: Mapping[str, int], column: str) -> pd.Series:
    labels = values.astype(str)
    codes = labels.map(mapping)
    unknown = labels[codes.isna()].unique().tolist()
    if unknown:
        raise UnmappedLabelError({column: unknown})
    return codes.astype(int)


def lookup(mapping: Mapping[str, int], label) -> int | None:
    if label is None or (not isinstance(label, str) and pd.isna(label)):
        return None
    return mapping.get(str(label))


def write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def dimension_labels(dim_dir: Path) -> tuple[dict[str, list[str]], str | None]:
    dg = pd.read_csv(dim_dir / "dimdategroupid.csv", usecols=["park_date", "date_group_id"])
    se = pd.read_csv(dim_dir / "dimseason.csv", usecols=["park_date", "season", "season_year"])
    last = max(pd.to_datetime(dg["park_date"]).max(), pd.to_datetime(se["park_date"]).max())
    labels = {
        "date_group_id": dg["date_group_id"].dropna().astype(str).unique().tolist(),
        "season": se["season"].dropna().astype(str).unique().tolist(),
        "season_year": se["season_year"].dropna().astype(str).unique().tolist(),
    }
    return labels, (last.date().isoformat() if pd.notna(last) else None)


def refresh_mappings_file(source: Path, target: Path, dim_dir: Path) -> dict:
    mappings = load_mappings(source)
    labels, dim_last_date = dimension_labels(dim_dir)
    extended, added = extend_mappings(mappings, labels)
    if extended != mappings or source != target or not target.exists():
        write_json_atomic(target, extended)
    return {
        "source": str(source),
        "target": str(target),
        "added": {k: v for k, v in added.items() if v},
        "n_added": sum(len(v) for v in added.values()),
        "dimension_last_date": dim_last_date,
    }
