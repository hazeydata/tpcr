import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from scorecard import WDW, grade_park, load, thresholds, unique_path


def pair(r):
    return {"date": r["park_date"].isoformat(), "forecast": float(r["forecast_wti"]), "actual": float(r["actual_wti"])}


def weekly(wti, start, end):
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    goal = thresholds()["mae_goal"]
    parks, missing = {}, {}
    for park in WDW:
        d = wti[(wti["park_code"] == park) & wti["park_date"].isin(dates)].sort_values("park_date")
        gaps = [x.isoformat() for x in dates if x not in set(d["park_date"])]
        if gaps:
            missing[park] = gaps
        if d.empty:
            continue
        rows = d.to_dict("records")
        absd = d["wti_error"].abs()
        days = [{**pair(r), "error": float(r["wti_error"]), "grade": grade_park(float(r["wti_error"]))} for r in rows]
        mae = round(float(absd.mean()), 2)
        parks[park] = {
            "avg_abs_miss": mae,
            "closest": pair(rows[int(absd.values.argmin())]),
            "biggest": pair(rows[int(absd.values.argmax())]),
            "n_days": len(rows),
            "days": days,
            "hits": sum(x["grade"] == "hit" for x in days),
            "misses": sum(x["grade"] == "miss" for x in days),
            "no_mark": sum(x["grade"] == "none" for x in days),
            "mae": mae,
            "goal_met": mae <= goal,
        }
    return {"start": start.isoformat(), "end": end.isoformat(), "complete": not missing, "missing_dates": missing, "parks": parks}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    out = a.out or Path(os.environ["TPCR_SCORECARD_DIR"])
    data = weekly(load(Path(os.environ["TPCR_ACCURACY_DIR"]) / "wti_accuracy.parquet", "wti_accuracy"), a.start, a.end)
    out.mkdir(parents=True, exist_ok=True)
    p = unique_path(out, f"weekly_{a.start}_{a.end}", ".json")
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
