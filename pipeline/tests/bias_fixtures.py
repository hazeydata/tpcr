from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

RUN_DATE = date(2026, 10, 30)
FIRST = date(2026, 2, 20)
ENTITIES = {"AK": ["AK85", "AK86", "AK25"], "HS": ["HS112", "HS147"]}


def season_of(d: date) -> tuple[str, str]:
    if d.month <= 2:
        return "WINTER", f"WINTER_{d.year}"
    if d.month <= 4:
        return "BEFORE_EASTER", f"BEFORE_EASTER_{d.year}"
    if d.month <= 8:
        return "SUMMER", f"SUMMER_{d.year}"
    return "AUTUMN", f"AUTUMN_{d.year}"


def frozen_mappings() -> dict:
    return {
        "date_group_id": {"DG_A": 0, "DG_B": 1},
        "season": {"AFTER_EASTER": 0, "AUTUMN": 1, "BEFORE_EASTER": 2, "SUMMER": 3, "WINTER": 4},
        "season_year": {"AFTER_EASTER_2010": 0, "BEFORE_EASTER_2026": 1, "WINTER_2026": 2},
    }


def _days(start: date, end: date):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def build_base(base: Path, last_actual: date = RUN_DATE - timedelta(days=1)) -> Path:
    dim = base / "dimension_tables"
    dim.mkdir(parents=True)
    all_days = _days(FIRST, RUN_DATE + timedelta(days=40))
    pd.DataFrame({"park_date": all_days,
                  "date_group_id": ["DG_A" if d.weekday() < 5 else "DG_B" for d in all_days]}
                 ).to_csv(dim / "dimdategroupid.csv", index=False)
    pd.DataFrame({"park_date": all_days,
                  "season": [season_of(d)[0] for d in all_days],
                  "season_year": [season_of(d)[1] for d in all_days]}
                 ).to_csv(dim / "dimseason.csv", index=False)
    pd.DataFrame({"park": [p for p in ENTITIES for _ in all_days],
                  "date": [d for _ in ENTITIES for d in all_days],
                  "opening_time": [f"{d} 08:00:00" for _ in ENTITIES for d in all_days]}
                 ).to_csv(dim / "dimparkhours.csv", index=False)
    codes = [e for es in ENTITIES.values() for e in es]
    pd.DataFrame({"code": codes, "fastpass_booth": [False] * len(codes),
                  "extinct_on": [None] * len(codes)}).to_csv(dim / "dimentity.csv", index=False)

    state = base / "state"
    state.mkdir()
    (state / "encoding_mappings.json").write_text(json.dumps(frozen_mappings()))

    facts = base / "fact_tables" / "parquet"
    facts.mkdir(parents=True)
    synth = base / "synthetic_actuals"
    synth.mkdir()
    fact_rows, synth_rows = [], {p: [] for p in ENTITIES}
    for park, ents in ENTITIES.items():
        for e in ents:
            for d in _days(FIRST, last_actual):
                for h in (9, 12, 15):
                    ts = datetime(d.year, d.month, d.day, h, 0)
                    fact_rows.append({"entity_code": e, "park_date": d, "observed_at": ts.isoformat(),
                                      "observed_at_ts": ts, "wait_time_type": "ACTUAL",
                                      "wait_time_minutes": 20 + h})
                    synth_rows[park].append({"entity_code": e, "park_date": d,
                                             "observed_at": str(ts), "synthetic_actual": 30.0 + h})
    pd.DataFrame(fact_rows).to_parquet(facts / "facts.parquet", index=False)
    for park, rows in synth_rows.items():
        pd.DataFrame(rows).to_parquet(synth / f"{park}.parquet", index=False)
    return base


def tree_digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}
