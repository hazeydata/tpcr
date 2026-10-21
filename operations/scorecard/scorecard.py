import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

VERSION = "1"
WINDOWS = (7, 30, 90)
WDW = ("MK", "EP", "HS", "AK")
REQUIRED = {
    "wti_accuracy": ["park_code", "park_date", "forecast_wti", "actual_wti", "wti_error", "evaluation_date"],
    "slot_accuracy": ["entity_code", "park_date", "time_slot", "forecast_wait", "actual_wait", "signed_error", "evaluation_date"],
}
KEYS = {"wti_accuracy": ["park_code", "park_date"], "slot_accuracy": ["entity_code", "park_date", "time_slot"]}
EXIT_OK, EXIT_ERROR, EXIT_DRIFT, EXIT_STALE = 0, 1, 3, 75


class SchemaError(Exception):
    pass


class NotCalibrated(Exception):
    pass


def env_float(name, default):
    return float(os.environ.get(name, default))


def thresholds():
    return {
        "park": env_float("TPCR_DRIFT_PARK_BIAS_MIN", 5),
        "entity": env_float("TPCR_DRIFT_ENTITY_BIAS_MIN", 15),
        "min_n": env_float("TPCR_SCORECARD_MIN_N", 20),
        "hit_max": env_float("TPCR_PARK_HIT_MAX_MIN", 5),
        "miss_min": env_float("TPCR_PARK_MISS_MIN", 10),
        "mae_goal": env_float("TPCR_PARK_MAE_GOAL_MIN", 8),
    }


def grade_park(error):
    t = thresholds()
    if abs(error) <= t["hit_max"]:
        return "hit"
    if abs(error) > t["miss_min"]:
        return "miss"
    return "none"


def grade_ride(error, mean_actual, cv):
    raise NotCalibrated("TPCR-RIDE-GRADE-CALIBRATE-001")


def park_of(entity):
    return re.match(r"[A-Za-z]+", entity).group(0)


def load(path, name):
    df = pd.read_parquet(path)
    missing = [c for c in REQUIRED[name] if c not in df.columns]
    if missing:
        raise SchemaError(f"{name}: missing required column(s): {', '.join(missing)}")
    df = df[REQUIRED[name]].copy()
    df["park_date"] = pd.to_datetime(df["park_date"]).dt.date
    df = df.sort_values("evaluation_date", kind="stable")
    return df.drop_duplicates(KEYS[name], keep="last").reset_index(drop=True)


def errors(wti, slot):
    p = pd.DataFrame({"level": "park", "key": wti["park_code"], "park_date": wti["park_date"], "error": wti["wti_error"].astype(float)})
    e = pd.DataFrame({"level": "entity", "key": slot["entity_code"], "park_date": slot["park_date"], "error": slot["signed_error"].astype(float)})
    return pd.concat([p, e], ignore_index=True)


def window_stats(err, as_of, w):
    d = err[(err["park_date"] >= as_of - timedelta(days=w)) & (err["park_date"] <= as_of)]
    g = d.assign(abs_error=d["error"].abs()).groupby(["level", "key"], sort=True)
    s = g.agg(n=("error", "size"), mean_bias=("error", "mean"), median_bias=("error", "median"), mae=("abs_error", "mean")).reset_index()
    s["window_days"] = w
    return s


def flag(row, t):
    if row["n"] < t["min_n"]:
        return "low_n"
    if row["window_days"] != 30 or abs(row["mean_bias"]) < t[row["level"]]:
        return "none"
    return "systematic" if row["mae"] <= 1.1 * abs(row["mean_bias"]) else "drift"


def scorecard(err, as_of):
    t = thresholds()
    rows = []
    for w in WINDOWS:
        s = window_stats(err, as_of, w)
        prev = window_stats(err, as_of - timedelta(days=7), w)[["level", "key", "mean_bias"]].rename(columns={"mean_bias": "prev_mean_bias"})
        rows.append(s.merge(prev, on=["level", "key"], how="left"))
    df = pd.concat(rows, ignore_index=True)
    df["mae_over_abs_bias"] = df["mae"] / df["mean_bias"].abs()
    park = df["level"] == "park"
    df["mae_goal_min"] = pd.Series(t["mae_goal"], index=df.index).where(park)
    df["goal_met"] = pd.Series([bool(m <= t["mae_goal"]) if p else None for m, p in zip(df["mae"], park)], dtype="object")
    df["flag"] = [flag(r, t) for r in df.to_dict("records")]
    df["as_of"] = as_of.isoformat()
    df["scorecard_version"] = VERSION
    df = df.sort_values(["level", "key", "window_days"], ascending=[False, True, True], kind="stable").reset_index(drop=True)
    return df[["as_of", "level", "key", "window_days", "n", "mean_bias", "median_bias", "mae", "mae_over_abs_bias",
               "prev_mean_bias", "mae_goal_min", "goal_met", "flag", "scorecard_version"]]


def flags_of(df):
    t = thresholds()
    f = df[df["flag"].isin(["drift", "systematic"])]
    return [{"key": r["key"], "level": r["level"], "reason": r["flag"], "value": round(float(r["mean_bias"]), 2),
             "threshold": t[r["level"]], "window_days": int(r["window_days"])} for r in f.to_dict("records")]


def systematic_parks(df, prev):
    before = {(p["park"], p["direction"]): p["since"] for p in prev}
    out = []
    for r in df[(df["level"] == "park") & (df["flag"] == "systematic")].to_dict("records"):
        direction = "high" if r["mean_bias"] > 0 else "low"
        out.append({"park": r["key"], "window_days": 30, "mean_bias": round(float(r["mean_bias"]), 2), "direction": direction,
                    "since": before.get((r["key"], direction), r["as_of"])})
    return out


def fmt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def to_md(df, as_of):
    cols = list(df.columns)
    lines = [f"# TPCR accuracy scorecard {as_of}", "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(fmt(v) for v in r) + " |" for r in df.itertuples(index=False)]
    return "\n".join(lines) + "\n"


def unique_path(d, stem, ext):
    p, i = d / f"{stem}{ext}", 2
    while p.exists():
        p, i = d / f"{stem}_r{i}{ext}", i + 1
    return p


def write_status(out, status):
    tmp = out / "status.json.tmp"
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, out / "status.json")


def read_status(out):
    p = out / "status.json"
    return json.loads(p.read_text()) if p.exists() else {}


def run(as_of, acc, out, dry_run):
    now = datetime.now(timezone.utc).isoformat()
    base = {"as_of": as_of.isoformat(), "generated_at": now, "systematic_parks": []}
    paths = [acc / f"{n}.parquet" for n in REQUIRED]
    frames = [load(p, p.stem) for p in paths if p.exists()]
    max_date = min(f["park_date"].max() for f in frames) if len(frames) == 2 else None
    if max_date is None or max_date < as_of - timedelta(days=2):
        flag_ = {"key": "inputs", "level": "input", "reason": "inputs_stale", "value": fmt(max_date and max_date.isoformat()),
                 "threshold": (as_of - timedelta(days=2)).isoformat(), "window_days": 0}
        status = {**base, "ok": False, "reason": "inputs_stale", "input_max_park_date": max_date and max_date.isoformat(), "flags": [flag_]}
        print(json.dumps(status, indent=2, sort_keys=True))
        if not dry_run:
            out.mkdir(parents=True, exist_ok=True)
            write_status(out, status)
        return EXIT_STALE
    df = scorecard(errors(*frames), as_of)
    md = to_md(df, as_of.isoformat())
    flags = flags_of(df)
    if dry_run:
        print(md)
        print(json.dumps(flags, indent=2))
        return EXIT_DRIFT if flags else EXIT_OK
    out.mkdir(parents=True, exist_ok=True)
    pq = unique_path(out, f"scorecard_{as_of.isoformat()}", ".parquet")
    df.to_parquet(pq, index=False)
    pq.with_suffix(".md").write_text(md)
    status = {**base, "ok": True, "input_max_park_date": max_date.isoformat(), "flags": flags, "scorecard": pq.name,
              "systematic_parks": systematic_parks(df, read_status(out).get("systematic_parks", []))}
    write_status(out, status)
    return EXIT_DRIFT if flags else EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True, type=date.fromisoformat)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    out = a.out or Path(os.environ["TPCR_SCORECARD_DIR"])
    try:
        return run(a.as_of, Path(os.environ["TPCR_ACCURACY_DIR"]), out, a.dry_run)
    except (SchemaError, KeyError, ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
