"""Posted->actual ratio study and synthetic-actual refresh for AK85/AK86 (TPCR-AK-BIAS-001 AC1, AC2).

  python -m pipeline.diagnostics.pandora_ratio study   --output-base PATH [--year 2026]
  python -m pipeline.diagnostics.pandora_ratio refresh --output-base PATH --ratio PATH [--after 2026-03-20]

study: real 2026 POSTED/ACTUAL pairs (same entity, park_date, hour; the s05
pairing), per entity and hour: n, mean posted, mean actual, ratio of means.
Writes accuracy/pandora_ratio_<year>.json and .md. Changes no model input.

refresh: synthetic_actual = posted * ratio[entity][hour] for POSTED rows with
park_date > --after, written to synthetic_actuals/ratio_refresh/<entity>.parquet,
sorted and with fixed schema so identical inputs give byte-identical files.
ratio_version (sha256 of the ratio JSON) and the source window are stored in
the parquet metadata and in the sidecar <entity>.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

ENTITIES = ("AK85", "AK86")
FREEZE_DATE = "2026-03-20"

PAIRS_SQL = """
WITH posted AS (
    SELECT entity_code, park_date, EXTRACT(HOUR FROM observed_at_ts)::INT AS hour,
           AVG(wait_time_minutes) AS posted_wait
    FROM read_parquet('{glob}')
    WHERE wait_time_type = 'POSTED' AND wait_time_minutes > 0 AND observed_at_ts IS NOT NULL
      AND entity_code IN ({ents}) AND park_date >= '{start}' AND park_date <= '{end}'
    GROUP BY ALL
),
actual AS (
    SELECT entity_code, park_date, EXTRACT(HOUR FROM observed_at_ts)::INT AS hour,
           AVG(wait_time_minutes) AS actual_wait
    FROM read_parquet('{glob}')
    WHERE wait_time_type = 'ACTUAL' AND wait_time_minutes > 0 AND observed_at_ts IS NOT NULL
      AND entity_code IN ({ents}) AND park_date >= '{start}' AND park_date <= '{end}'
    GROUP BY ALL
)
SELECT p.entity_code, p.hour, COUNT(*) AS n,
       AVG(p.posted_wait) AS mean_posted, AVG(a.actual_wait) AS mean_actual,
       MIN(p.park_date)::VARCHAR AS first_date, MAX(p.park_date)::VARCHAR AS last_date
FROM posted p JOIN actual a USING (entity_code, park_date, hour)
GROUP BY p.entity_code, p.hour
ORDER BY p.entity_code, p.hour
"""


def _glob(output_base: Path) -> str:
    return str(Path(output_base) / "fact_tables" / "parquet" / "*.parquet")


def study(output_base: Path, year: int = 2026, entities=ENTITIES) -> dict:
    start, end = f"{year}-01-01", f"{year}-12-31"
    ents = ", ".join(f"'{e}'" for e in entities)
    with duckdb.connect() as con:
        rows = con.execute(PAIRS_SQL.format(glob=_glob(output_base), ents=ents, start=start, end=end)).fetchall()
    ratios: dict = {e: {} for e in entities}
    window = [None, None]
    for code, hour, n, mp, ma, first, last in rows:
        ratios[code][str(hour)] = {
            "n": int(n), "mean_posted": round(mp, 3), "mean_actual": round(ma, 3),
            "ratio": round(ma / mp, 4),
        }
        window[0] = first if window[0] is None else min(window[0], first)
        window[1] = last if window[1] is None else max(window[1], last)
    return {"source": "real POSTED/ACTUAL pairs", "year": year,
            "source_window": {"start": window[0], "end": window[1]}, "ratios": ratios}


def render_markdown(result: dict) -> str:
    w = result["source_window"]
    out = [f"# AK85/AK86 posted->actual ratio by hour ({result['year']} real actuals only)", "",
           f"Source window: {w['start']} to {w['end']}. Pairing: same entity, park_date and hour (s05 method).", ""]
    for code, hours in result["ratios"].items():
        out += [f"## {code}", "", "| hour | n | mean posted | mean actual | ratio |", "|---|---|---|---|---|"]
        for h in sorted(hours, key=int):
            r = hours[h]
            out.append(f"| {int(h):02d} | {r['n']} | {r['mean_posted']:.1f} | {r['mean_actual']:.1f} | {r['ratio']:.3f} |")
        out.append("")
    return "\n".join(out)


def ratio_version(ratio_path: Path) -> str:
    return hashlib.sha256(Path(ratio_path).read_bytes()).hexdigest()[:16]


SCHEMA = pa.schema([
    ("entity_code", pa.string()),
    ("park_date", pa.string()),
    ("observed_at", pa.string()),
    ("posted_wait", pa.float64()),
    ("ratio", pa.float64()),
    ("synthetic_actual", pa.float64()),
])


def refresh(output_base: Path, ratio_path: Path, after: str = FREEZE_DATE, entities=ENTITIES) -> dict:
    ratio_doc = json.loads(Path(ratio_path).read_text())
    version = ratio_version(ratio_path)
    out_dir = Path(output_base) / "synthetic_actuals" / "ratio_refresh"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for code in entities:
        hours = ratio_doc["ratios"].get(code, {})
        if not hours:
            raise ValueError(f"{code}: no hourly ratios in {ratio_path}")
        with duckdb.connect() as con:
            rows = con.execute(f"""
                SELECT entity_code, park_date::VARCHAR, strftime(observed_at_ts, '%Y-%m-%d %H:%M:%S'),
                       wait_time_minutes::DOUBLE, EXTRACT(HOUR FROM observed_at_ts)::INT
                FROM read_parquet('{_glob(output_base)}')
                WHERE wait_time_type = 'POSTED' AND wait_time_minutes > 0 AND observed_at_ts IS NOT NULL
                  AND entity_code = ? AND park_date > ?
                ORDER BY 1, 2, 3, 4
            """, [code, after]).fetchall()
        missing = sorted({r[4] for r in rows} - {int(h) for h in hours})
        if missing:
            raise ValueError(f"{code}: no ratio for hours {missing}; refusing to guess")
        cols = {k: [] for k in SCHEMA.names}
        for ent, day, ts, posted, hour in rows:
            ratio = hours[str(hour)]["ratio"]
            cols["entity_code"].append(ent)
            cols["park_date"].append(day)
            cols["observed_at"].append(ts)
            cols["posted_wait"].append(posted)
            cols["ratio"].append(ratio)
            cols["synthetic_actual"].append(max(round(posted * ratio, 3), 1.0))
        window = {"after": after, "first": cols["park_date"][0] if rows else None,
                  "last": cols["park_date"][-1] if rows else None}
        meta = {"ratio_version": version, "ratio_source_window": ratio_doc["source_window"],
                "posted_window": window, "rows": len(rows)}
        table = pa.table(cols, schema=SCHEMA).replace_schema_metadata(
            {"tpcr_ratio_refresh": json.dumps(meta, sort_keys=True)})
        path = out_dir / f"{code}.parquet"
        pq.write_table(table, path, compression="zstd", write_statistics=False,
                       use_dictionary=False)
        (out_dir / f"{code}.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
        written[code] = meta
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("study")
    s.add_argument("--output-base", type=Path, required=True)
    s.add_argument("--year", type=int, default=2026)
    r = sub.add_parser("refresh")
    r.add_argument("--output-base", type=Path, required=True)
    r.add_argument("--ratio", type=Path, required=True)
    r.add_argument("--after", default=FREEZE_DATE)
    a = ap.parse_args(argv)
    if a.cmd == "study":
        res = study(a.output_base, a.year)
        out = Path(a.output_base) / "accuracy"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"pandora_ratio_{a.year}.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
        (out / f"pandora_ratio_{a.year}.md").write_text(render_markdown(res) + "\n")
        print(out / f"pandora_ratio_{a.year}.md")
    else:
        print(json.dumps(refresh(a.output_base, a.ratio, a.after), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
