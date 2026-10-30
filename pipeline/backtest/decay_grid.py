"""Fix 4 back-test: geo-decay half-life x min_training_year grid (TPCR-BIAS-001).

For every day D in the window and every candidate, each entity's baseline model
is fitted exactly as s07 does (fit_baseline) on training rows with park_date < D
and year >= min_training_year, with decay measured from D, and then predicts the
real ACTUAL rows of day D. Rows come from the Phase A (s06b) per-park files.

Metrics, all minutes, error = predicted - actual:
  entity-day: mean prediction vs mean actual over that day's real observations.
  per ride:   mean signed miss and MAE over its entity-days.
  per park:   park-day WTI proxy = mean over rides observed that day; WTI MAE and
              mean signed miss over park-days.

Pass bar (AC16, locked): for AK85 and AK86 |mean signed miss| drops >= 50% vs the
730/2016 baseline, and no park's WTI MAE rises by more than 0.5.

    python -m pipeline.backtest.decay_grid --training-dir DIR --out DIR \\
        --parks AK HS --start 2026-09-28 --end 2026-10-27
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from pipeline.config import PipelineConfig
from pipeline.steps.s07_training import BASELINE_FEATURES, baseline_matrix, fit_baseline

BIAS_RIDES = ("AK85", "AK86")
WATCH_RIDES = ("AK85", "AK86", "HS112", "HS103", "HS20")
MAE_TOLERANCE = 0.5
BIAS_CUT = 0.5
RIDE_MAE_WORSEN = 0.25


@dataclass(frozen=True)
class Candidate:
    halflife_days: int
    min_training_year: int

    @property
    def name(self) -> str:
        return f"hl{self.halflife_days}_y{self.min_training_year}"


BASELINE = Candidate(730, 2016)
GRID = tuple(Candidate(h, y) for h in (730, 365, 180) for y in (2016, 2022))


def window(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def entity_days(park_df: pd.DataFrame, candidate: Candidate, days: list[date],
                cfg: PipelineConfig, min_samples: int | None = None) -> pd.DataFrame:
    min_samples = min_samples or cfg.training_min_obs_lite
    df = park_df.copy()
    df["_d"] = pd.to_datetime(df["park_date"]).dt.date
    df = df[pd.to_datetime(df["park_date"]).dt.year >= candidate.min_training_year]
    rows = []
    for entity, edf in df.groupby("entity_code", sort=True):
        for d in days:
            target = edf[(edf["_d"] == d) & ~edf["is_synthetic"].astype(bool)]
            if target.empty:
                continue
            fitted = fit_baseline(edf[edf["_d"] < d].drop(columns="_d"), cfg, min_samples,
                                  d, candidate.halflife_days)
            if fitted is None:
                continue
            bst, _ = fitted
            X = baseline_matrix(target.drop(columns="_d"))[BASELINE_FEATURES].values.astype(np.float32)
            pred = np.clip(bst.predict(xgb.DMatrix(X)), 0, 300)
            rows.append({"candidate": candidate.name, "entity_code": entity, "park_date": d,
                         "pred": float(pred.mean()), "actual": float(target["actual_time"].mean())})
    return pd.DataFrame(rows, columns=["candidate", "entity_code", "park_date", "pred", "actual"])


def summarise(ed: pd.DataFrame, park_of) -> tuple[pd.DataFrame, pd.DataFrame]:
    ed = ed.assign(park=ed["entity_code"].map(park_of), err=ed["pred"] - ed["actual"])
    rides = (ed.groupby(["candidate", "park", "entity_code"])
               .agg(days=("err", "size"), signed=("err", "mean"),
                    mae=("err", lambda e: e.abs().mean()))
               .reset_index())
    park_day = (ed.groupby(["candidate", "park", "park_date"])[["pred", "actual"]].mean()
                  .reset_index())
    park_day["err"] = park_day["pred"] - park_day["actual"]
    parks = (park_day.groupby(["candidate", "park"])
                     .agg(days=("err", "size"), signed=("err", "mean"),
                          wti_mae=("err", lambda e: e.abs().mean()))
                     .reset_index())
    return rides, parks


def evaluate(rides: pd.DataFrame, parks: pd.DataFrame, baseline: str = BASELINE.name) -> pd.DataFrame:
    base_p = parks[parks["candidate"] == baseline].set_index("park")["wti_mae"]
    base_r = rides[rides["candidate"] == baseline].set_index("entity_code")
    out = []
    for cand in sorted(parks["candidate"].unique()):
        p = parks[parks["candidate"] == cand].set_index("park")["wti_mae"]
        r = rides[rides["candidate"] == cand].set_index("entity_code")
        delta = (p - base_p).dropna()
        row = {"candidate": cand, "worst_park_mae_delta": float(delta.max()) if len(delta) else np.nan,
               "worst_park": delta.idxmax() if len(delta) else None}
        bias_ok = True
        for ride in BIAS_RIDES:
            s = r["signed"].get(ride, np.nan)
            b = base_r["signed"].get(ride, np.nan)
            row[f"{ride}_signed"] = s
            row[f"{ride}_cut"] = 1 - abs(s) / abs(b) if b and not np.isnan(b) and not np.isnan(s) else np.nan
            bias_ok &= bool(row[f"{ride}_cut"] >= BIAS_CUT) if not np.isnan(row[f"{ride}_cut"]) else False
        mae_ok = bool(len(delta)) and bool((delta <= MAE_TOLERANCE).all())
        row["passes"] = bias_ok and mae_ok
        rmae = (r["mae"] / base_r["mae"] - 1).dropna()
        row["rides_mae_worse_25pct"] = sorted(rmae[rmae > RIDE_MAE_WORSEN].index.tolist())
        out.append(row)
    return pd.DataFrame(out)


def recommend(verdict: pd.DataFrame, parks: pd.DataFrame) -> str | None:
    passing = verdict[verdict["passes"]]
    if passing.empty:
        return None
    mean_mae = parks.groupby("candidate")["wti_mae"].mean()
    return min(passing["candidate"], key=lambda c: (mean_mae.get(c, np.inf), c))


def main() -> None:
    from pipeline.core.park_codes import entity_to_park
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--training-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--parks", nargs="+", default=["AK", "HS", "EP", "MK"])
    ap.add_argument("--start", type=date.fromisoformat, default=date(2026, 9, 28))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 27))
    args = ap.parse_args()
    cfg = PipelineConfig()
    days = window(args.start, args.end)
    frames = []
    for park in args.parks:
        park_df = pd.read_parquet(args.training_dir / f"{park}.parquet")
        for cand in GRID:
            frames.append(entity_days(park_df, cand, days, cfg))
        del park_df
    ed = pd.concat(frames, ignore_index=True)
    rides, parks = summarise(ed, entity_to_park)
    verdict = evaluate(rides, parks)
    args.out.mkdir(parents=True, exist_ok=True)
    ed.to_csv(args.out / "entity_days.csv", index=False)
    rides.to_csv(args.out / "rides.csv", index=False)
    parks.to_csv(args.out / "parks.csv", index=False)
    verdict.to_csv(args.out / "verdict.csv", index=False)
    rec = recommend(verdict, parks)
    (args.out / "recommendation.txt").write_text(f"{rec or 'none: keep baseline 730/2016'}\n")
    print(verdict.to_string(index=False))
    print(f"recommended: {rec}")


if __name__ == "__main__":
    main()
