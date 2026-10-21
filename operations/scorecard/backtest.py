import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from scorecard import errors, load, scorecard


def backtest(err, start, end):
    first = {}
    d = start
    while d <= end:
        df = scorecard(err, d)
        for r in df[df["flag"].isin(["drift", "systematic"])].to_dict("records"):
            first.setdefault((r["level"], r["key"], r["flag"]), d)
        d += timedelta(days=1)
    return first


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-04-01", type=date.fromisoformat)
    ap.add_argument("--end", default="2026-10-12", type=date.fromisoformat)
    a = ap.parse_args(argv)
    acc = Path(os.environ["TPCR_ACCURACY_DIR"])
    err = errors(load(acc / "wti_accuracy.parquet", "wti_accuracy"), load(acc / "slot_accuracy.parquet", "slot_accuracy"))
    first = backtest(err, a.start, a.end)
    for (level, key, reason), d in sorted(first.items()):
        print(f"{level}\t{key}\t{reason}\t{d.isoformat()}")
    print(f"flagged_keys\t{len({(l, k) for l, k, _ in first})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
