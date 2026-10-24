#!/usr/bin/env python3
"""Time the S4 model_aggregates build on a synthetic fixture (TPCR-S4-AGG-001 AC11).

    python scripts/bench_model_aggregates.py --dir /tmp/x --entities 200 --days 365

Prints input rows, output rows, build seconds and rows/second, so the time for
the real fact table can be extrapolated from its row count.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.core.model_aggregates import build_model_aggregates  # noqa: E402
from pipeline.tests.agg_fixture import write_synthetic_facts  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--entities", type=int, default=200)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--step-min", type=int, default=15)
    ap.add_argument("--groups", type=int, default=50)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--memory", default="8GB")
    a = ap.parse_args()
    as_of = date(2026, 10, 23)
    t = time.time()
    n = write_synthetic_facts(a.dir, a.entities, as_of - timedelta(days=a.days), as_of - timedelta(days=1),
                              step_min=a.step_min, n_groups=a.groups)
    gen = time.time() - t
    r = build_model_aggregates(a.dir, as_of=as_of, threads=a.threads, memory_limit=a.memory)
    print(f"input_rows={n} gen_sec={gen:.1f} out_rows={r.rows} build_sec={r.duration_sec} "
          f"rows_per_sec={n / r.duration_sec:,.0f}")


if __name__ == "__main__":
    main()
