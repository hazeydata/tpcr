"""TPCR-SHADOW-REVIVE-001 Phase 2 tests: full-entity retrains, identical-set scoring,
fallback entities, fallback-v2.

No xgboost, no network, tmp dirs, small synthetic frames. Tests that need duckdb
or pyarrow skip when it is missing. Runs under pytest or unittest.
"""

import importlib.util
import json
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

try:
    import pyarrow  # noqa: F401
    HAVE_PYARROW = True
except ImportError:
    HAVE_PYARROW = False

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "rolling_shadow.py"
STEPS_DIR = REPO_ROOT / "pipeline" / "steps"

needs_pandas = unittest.skipIf(pd is None, "pandas missing")
needs_parquet = unittest.skipIf(pd is None or not HAVE_PYARROW, "pandas/pyarrow missing")

TODAY = date(2026, 10, 24)
SLOTS = ["06:00:00", "06:05:00", "06:10:00", "06:15:00"]


def load_rolling_shadow():
    spec = importlib.util.spec_from_file_location("rolling_shadow_p2_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frame(rows, value_col):
    return pd.DataFrame(rows, columns=["entity_code", "slot", value_col])


# --------------------------------------------------------------------------
# AC5: eligibility
# --------------------------------------------------------------------------

@needs_pandas
class EligibilityTest(unittest.TestCase):
    COUNTS = {"MK01": 500, "MK02": 500, "MK03": 50, "BB01": 500, "MK04": 500, "MK05": 500}
    EXTINCT = {"MK02": date(2020, 1, 1), "MK04": date(2027, 1, 1), "MK05": TODAY}

    def eligible(self, **kw):
        from pipeline.competition.eligibility import production_eligible_entities

        args = dict(entity_counts=self.COUNTS, extinct_on=self.EXTINCT,
                    min_obs=100, ignore_parks=["BB", "TL", "VB"])
        args.update(kw)
        return production_eligible_entities(Path("unused"), TODAY, **args)

    def test_extinct_excluded_and_sorted(self):
        got = self.eligible()
        # MK02 extinct in 2020, MK05 extinct on the run date (<=), MK03 below min_obs, BB01 water park
        self.assertEqual(got, ["MK01", "MK04"])  # MK04 goes extinct in the future: still eligible
        self.assertEqual(got, sorted(got))

    def test_extinct_on_run_date_is_excluded_but_day_before_is_not(self):
        self.assertNotIn("MK05", self.eligible())
        self.assertIn("MK05", self.eligible(extinct_on={"MK05": TODAY + timedelta(days=1)}))

    def test_report_reasons_and_count_equals_production_set(self):
        from pipeline.competition.eligibility import eligibility_report

        rep = eligibility_report(Path("unused"), TODAY, entity_counts=self.COUNTS,
                                 extinct_on=self.EXTINCT, min_obs=100, ignore_parks=["BB"])
        self.assertEqual(rep["excluded"]["MK02"], "extinct")
        self.assertEqual(rep["excluded"]["MK03"], "below_min_obs")
        self.assertEqual(rep["excluded"]["BB01"], "ignored_park")
        # every counted entity is either eligible or excluded: nothing is subset away
        self.assertEqual(set(rep["eligible"]) | set(rep["excluded"]), set(self.COUNTS))
        self.assertEqual(len(rep["eligible"]), len(set(rep["eligible"])))

    def test_load_extinct_on_reads_dimentity(self):
        from pipeline.competition.eligibility import load_extinct_on

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "dimentity.csv").write_text(
                "code,name,extinct_on\nMK01,A,\nMK02,B,2020-05-01\nHS29,C,2000-01-01\n")
            self.assertEqual(load_extinct_on(Path(d)),
                             {"MK02": date(2020, 5, 1), "HS29": date(2000, 1, 1)})


# --------------------------------------------------------------------------
# AC6: paired scores
# --------------------------------------------------------------------------

def paired_fixture():
    s1, s2, s3, s4 = SLOTS
    actual = frame([("A", s1, 10), ("A", s2, 99), ("A", s3, 99), ("B", s1, 20), ("B", s2, 30),
                    ("C", s1, 5)], "actual")
    prod = frame([("A", s1, 12), ("A", s2, 1), ("A", s4, 1), ("B", s1, 18), ("B", s2, 36),
                  ("C", s1, 6)], "prod_pred")
    chal = frame([("A", s1, 11), ("A", s3, 1), ("A", s4, 1), ("B", s1, 20), ("B", s2, 33)],
                 "chal_pred")
    return prod, chal, actual


@needs_pandas
class PairedScoresTest(unittest.TestCase):
    def build(self, fallback=("B",)):
        from pipeline.competition.paired_scores import build_paired_scores

        prod, chal, actual = paired_fixture()
        return build_paired_scores(prod, chal, actual, "chal-a", "2026-10-23", set(fallback))

    def test_intersection_columns_and_drop_reasons(self):
        from pipeline.competition.paired_scores import SCORE_COLUMNS

        scores, dropped = self.build()
        self.assertEqual(list(scores.columns), SCORE_COLUMNS)
        self.assertEqual(list(zip(scores["entity_code"], scores["slot"])),
                         [("A", SLOTS[0]), ("B", SLOTS[0]), ("B", SLOTS[1])])
        # A s2: no chal | A s3: no prod | A s4: no actual | C s1: no chal
        self.assertEqual(dropped, {"no_actual": 1, "no_prod_pred": 1, "no_chal_pred": 2})
        self.assertTrue((scores["date"] == "2026-10-23").all())
        self.assertTrue((scores["challenger_id"] == "chal-a").all())
        self.assertEqual(list(scores["is_fallback_entity"]), [False, True, True])

    def test_pair_missing_several_counts_under_first_missing(self):
        from pipeline.competition.paired_scores import build_paired_scores

        s1 = SLOTS[0]
        # only the challenger knows (X, s1): missing actual and prod -> counted as no_actual
        # prod+chal only (Y, s1): missing actual -> no_actual
        # actual only (Z, s1): missing prod and chal -> no_prod_pred
        scores, dropped = build_paired_scores(
            frame([("Y", s1, 1)], "prod_pred"),
            frame([("X", s1, 1), ("Y", s1, 1)], "chal_pred"),
            frame([("Z", s1, 1)], "actual"),
            "c", "2026-10-23", set())
        self.assertEqual(len(scores), 0)
        self.assertEqual(dropped, {"no_actual": 2, "no_prod_pred": 1, "no_chal_pred": 0})

    def test_nan_predictions_count_as_missing(self):
        from pipeline.competition.paired_scores import build_paired_scores

        s1 = SLOTS[0]
        scores, dropped = build_paired_scores(
            frame([("A", s1, float("nan"))], "prod_pred"),
            frame([("A", s1, 3.0)], "chal_pred"),
            frame([("A", s1, 4.0)], "actual"), "c", "2026-10-23", set())
        self.assertEqual(len(scores), 0)
        self.assertEqual(dropped["no_prod_pred"], 1)

    def test_entity_weighted_mae_and_fallback_separate(self):
        from pipeline.competition.paired_scores import summarize

        scores, dropped = self.build(fallback=("B",))
        sm = summarize(scores, dropped, forecast_entities=4)
        c = sm["challengers"]["chal-a"]
        # prod errors: A 2 | B 2, 6 -> entity MAE A=2, B=4 -> 3.0 (flat slot average would be 3.33)
        self.assertAlmostEqual(c["mae_prod"], 3.0)
        # chal errors: A 1 | B 0, 3 -> A=1, B=1.5 -> 1.25
        self.assertAlmostEqual(c["mae_chal"], 1.25)
        self.assertAlmostEqual(c["mae_fallback_prod"], 4.0)
        self.assertAlmostEqual(c["mae_fallback_chal"], 1.5)
        self.assertEqual(c["n_fallback_entities"], 1)
        self.assertEqual(c["n_entities"], 2)
        self.assertEqual(c["dropped"], dropped)

    def test_no_fallback_rows_gives_none(self):
        from pipeline.competition.paired_scores import summarize

        scores, dropped = self.build(fallback=())
        c = summarize(scores, dropped, 4)["challengers"]["chal-a"]
        self.assertIsNone(c["mae_fallback_prod"])
        self.assertIsNone(c["mae_fallback_chal"])

    def test_summary_coverage_and_counts(self):
        from pipeline.competition.paired_scores import summarize

        scores, dropped = self.build()
        sm = summarize(scores, dropped, forecast_entities=4)
        self.assertEqual(sm["matched_pairs"], 3)
        self.assertEqual(sm["forecast_entities"], 4)
        self.assertAlmostEqual(sm["coverage_pct"], 50.0)  # A and B matched, of 4 forecast entities
        sm2 = summarize(scores, dropped, forecast_entities={"A", "B"})
        self.assertAlmostEqual(sm2["coverage_pct"], 100.0)

    def test_two_challengers_get_separate_blocks(self):
        from pipeline.competition.paired_scores import build_paired_scores, summarize

        prod, chal, actual = paired_fixture()
        a, da = build_paired_scores(prod, chal, actual, "one", "2026-10-23", set())
        b, db = build_paired_scores(prod, chal.assign(chal_pred=chal["chal_pred"] + 1),
                                    actual, "two", "2026-10-23", set())
        sm = summarize(pd.concat([a, b], ignore_index=True),
                       {"one": da, "two": db}, forecast_entities=2)
        self.assertEqual(set(sm["challengers"]), {"one", "two"})
        self.assertEqual(sm["challengers"]["one"]["dropped"], da)
        self.assertNotEqual(sm["challengers"]["one"]["mae_chal"], sm["challengers"]["two"]["mae_chal"])

    def test_empty_scores_summary(self):
        from pipeline.competition.paired_scores import SCORE_COLUMNS, summarize

        sm = summarize(pd.DataFrame(columns=SCORE_COLUMNS), {"no_actual": 3}, 10)
        self.assertEqual((sm["matched_pairs"], sm["coverage_pct"], sm["challengers"]), (0, 0.0, {}))


@unittest.skipUnless(HAVE_DUCKDB and HAVE_PYARROW and pd is not None, "duckdb/pyarrow/pandas missing")
class ShadowEvaluatePairedTest(unittest.TestCase):
    def test_report_from_paired_set_and_scores_file(self):
        from datetime import time

        from pipeline.competition.shadow_evaluate import evaluate_shadow_day

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            fact = base / "fact_tables" / "parquet"
            fact.mkdir(parents=True)
            pd.DataFrame({
                "entity_code": ["MK01", "MK01", "MK02", "MK02"],
                "park_date": ["2026-10-23"] * 4,
                "observed_at_ts": pd.to_datetime(["2026-10-23 06:00:10", "2026-10-23 06:05:10",
                                                  "2026-10-23 06:00:10", "2026-10-23 06:10:10"]),
                "wait_time_minutes": [10.0, 20.0, 30.0, 99.0],
                "wait_time_type": ["ACTUAL", "ACTUAL", "ACTUAL", "POSTED"],
            }).to_parquet(fact / "2026-10.parquet")

            shadow = base / "shadow"
            shadow.mkdir()
            t0, t1 = time(6, 0), time(6, 5)
            pd.DataFrame({
                "entity_code": ["MK01", "MK01", "MK02"], "park_date": ["2026-10-23"] * 3,
                "time_slot": [t0, t1, t0], "predicted_actual": [12, 18, 25],
                "prediction_method": ["model_baseline", "model_baseline", "fallback_ratio"],
            }).to_parquet(shadow / "baseline_2026-10-23.parquet")
            pd.DataFrame({
                "entity_code": ["MK01", "MK01", "MK02"], "park_date": ["2026-10-23"] * 3,
                "time_slot": [t0, t1, t0], "predicted_actual": [11, 22, 33],
                "prediction_method": ["m", "m", "m"],
            }).to_parquet(shadow / "challenger_2026-10-23.parquet")

            out = base / "scores.parquet"
            report = evaluate_shadow_day(
                "2026-10-23", str(shadow / "challenger_2026-10-23.parquet"),
                str(shadow / "baseline_2026-10-23.parquet"), str(base),
                challenger_id="chal-a", scores_out=str(out))

            self.assertEqual(report["n_matched"], 3)
            self.assertEqual(report["matched_pairs"], 3)
            self.assertEqual(report["n_entities"], 2)
            self.assertAlmostEqual(report["baseline_mae"], 3.5)    # MK01 2.0, MK02 5.0
            self.assertAlmostEqual(report["challenger_mae"], 2.25)  # MK01 1.5, MK02 3.0
            # MK02 is ratio-served in production -> the only fallback entity
            self.assertAlmostEqual(report["mae_fallback_prod"], 5.0)
            self.assertAlmostEqual(report["mae_fallback_chal"], 3.0)
            self.assertEqual(report["forecast_entities"], 2)
            self.assertAlmostEqual(report["coverage_pct"], 100.0)
            # the POSTED row at 06:10 is not an actual
            self.assertEqual(report["dropped"], {"no_actual": 0, "no_prod_pred": 0, "no_chal_pred": 0})
            for key in ("slot_baseline_mae", "entity_challenger_wins", "parks"):
                self.assertIn(key, report)

            scores = pd.read_parquet(out)
            self.assertEqual(len(scores), 3)
            self.assertEqual(set(scores["challenger_id"]), {"chal-a"})
            self.assertEqual(scores.set_index("entity_code").loc["MK02", "is_fallback_entity"], True)


# --------------------------------------------------------------------------
# AC7: fallback entities
# --------------------------------------------------------------------------

@needs_pandas
class FallbackEntitiesTest(unittest.TestCase):
    ENTITIES = pd.DataFrame({
        "code": ["MK01", "MK02", "MK03", "MK04"],
        "name": ["Space Mountain", "Space Mountain SINGLE RIDER", "Big Thunder", "Parade"],
    }) if pd is not None else None

    def test_is_single_rider_by_name_case_insensitive(self):
        from pipeline.competition.fallback_entities import is_single_rider

        self.assertTrue(is_single_rider("MK02", self.ENTITIES))
        self.assertFalse(is_single_rider("MK01", self.ENTITIES))
        self.assertFalse(is_single_rider("ZZ99", self.ENTITIES))
        self.assertFalse(is_single_rider("MK02"))  # no dimentity: unknown

    def test_flag_column_wins_over_name(self):
        from pipeline.competition.fallback_entities import is_single_rider

        ents = self.ENTITIES.assign(is_single_rider=[False, False, True, False])
        self.assertTrue(is_single_rider("MK03", ents))
        self.assertFalse(is_single_rider("MK02", ents))

    def test_ratio_methods_and_union(self):
        from pipeline.competition.fallback_entities import (
            fallback_entities, is_ratio_method, ratio_served_entities)

        for m in ("fallback_ratio", "fallback_ratio_no_model", "ratio_entity", "ratio_global"):
            self.assertTrue(is_ratio_method(m), m)
        for m in ("model_baseline", "parent_scaled", None):
            self.assertFalse(is_ratio_method(m), m)

        prod = pd.DataFrame({
            "entity_code": ["MK01", "MK02", "MK03", "MK04"],
            "prediction_method": ["model_baseline", "model_baseline", "model_baseline", "fallback_ratio"],
        })
        self.assertEqual(ratio_served_entities(prod), {"MK04"})
        self.assertEqual(fallback_entities(prod, self.ENTITIES), {"MK02", "MK04"})


# --------------------------------------------------------------------------
# AC8: fallback-v2
# --------------------------------------------------------------------------

def entities_fixture():
    return pd.DataFrame({
        "code": ["MK01", "MK02", "MK03", "MK04", "MK05", "MK06", "MK07", "MK08", "MK09", "MK11", "MK12"],
        "name": ["Space Mountain", "Space Mountain Single Rider", "Big Thunder",
                 "Big Thunder - Single Rider", "Orphan Ride Single Rider", "Fireworks Show",
                 "Some Show", "Mystery Thing", "Plain Ride", "Lost Ride", "Lost Ride Single Rider"],
    })


def posted_fixture():
    rows = []
    # MK01/MK02: 40 pairs in window, child = 0.5 x parent for 25, 0.9 x parent for 15 -> median 0.5
    for i in range(40):
        slot = f"{6 + i // 12:02d}:{(i % 12) * 5:02d}:00"
        rows.append(("MK01", "2026-10-20", slot, 20.0))
        rows.append(("MK02", "2026-10-20", slot, 10.0 if i < 25 else 18.0))
    # MK03/MK04: 10 pairs in window; 30 more only outside the 365-day window
    for i in range(10):
        slot = f"07:{i * 5:02d}:00"
        rows.append(("MK03", "2026-10-20", slot, 40.0))
        rows.append(("MK04", "2026-10-20", slot, 10.0))
    for i in range(30):
        slot = f"08:{(i % 12) * 5:02d}:{i // 12:02d}"
        rows.append(("MK03", "2025-01-10", slot, 40.0))
        rows.append(("MK04", "2025-01-10", slot, 10.0))
    return pd.DataFrame(rows, columns=["entity_code", "park_date", "time_slot", "posted"])


def pairs_fixture():
    rows = []
    for ent, n, posted, actual in [("MK07", 40, 10.0, 8.0), ("MK01", 35, 10.0, 7.0),
                                   ("MK09", 5, 10.0, 5.0), ("MK08", 3, 10.0, 4.0)]:
        rows += [(ent, "2026-10-20", posted, actual)] * n
    rows += [("MK07", "2024-01-01", 10.0, 1.0)] * 50  # outside the window: ignored
    return pd.DataFrame(rows, columns=["entity_code", "park_date", "posted", "actual"])


@needs_pandas
class SingleRiderParentTest(unittest.TestCase):
    def build(self):
        from pipeline.competition.fallback_v2 import build_single_rider_parent

        return build_single_rider_parent(posted_fixture(), entities_fixture(), TODAY)

    def row(self, df, child):
        rows = df[df["child_entity_code"] == child]
        self.assertEqual(len(rows), 1, f"exactly one row for {child}")
        return rows.iloc[0]

    def test_columns_and_one_row_per_child(self):
        from pipeline.competition.fallback_v2 import PARENT_COLUMNS

        df = self.build()
        self.assertEqual(list(df.columns), PARENT_COLUMNS)
        self.assertEqual(sorted(df["child_entity_code"]), ["MK02", "MK04", "MK05", "MK12"])
        parents = df[df["parent_entity_code"] != ""]["parent_entity_code"]
        self.assertTrue(all(p in set(entities_fixture()["code"]) for p in parents))
        self.assertFalse(set(parents) & set(df["child_entity_code"]))  # parent is never a single rider

    def test_at_least_30_pairs_uses_median(self):
        r = self.row(self.build(), "MK02")
        self.assertEqual(r["parent_entity_code"], "MK01")
        self.assertAlmostEqual(r["scale_factor"], 0.5)
        self.assertEqual((r["scale_source"], r["n_obs"]), ("observed", 40))
        self.assertEqual(r["computed_at"], "2026-10-24")

    def test_under_30_pairs_defaults_to_one_and_logs(self):
        from pipeline.competition.fallback_v2 import build_single_rider_parent

        with self.assertLogs("pipeline.competition.fallback_v2", level="INFO") as cm:
            df = build_single_rider_parent(posted_fixture(), entities_fixture(), TODAY)
        r = self.row(df, "MK04")  # name uses "Big Thunder - Single Rider": base name still matches
        self.assertEqual(r["parent_entity_code"], "MK03")
        self.assertEqual(r["scale_factor"], 1.0)
        self.assertEqual(r["scale_source"], "default")
        self.assertEqual(r["n_obs"], 10)  # the 30 old pairs are outside the 365-day window
        self.assertTrue(any("MK04" in line and "default" in line for line in cm.output))

    def test_unmapped_child_is_a_named_failure(self):
        from pipeline.competition.fallback_v2 import build_single_rider_parent, unmapped_children

        with self.assertLogs("pipeline.competition.fallback_v2", level="WARNING") as cm:
            df = build_single_rider_parent(posted_fixture(), entities_fixture(), TODAY)
        r = self.row(df, "MK05")
        self.assertEqual(r["parent_entity_code"], "")
        self.assertEqual(r["scale_source"], "unmapped:no_parent")
        self.assertEqual(unmapped_children(df), ["MK05"])
        self.assertTrue(any("MK05" in line for line in cm.output))

    def test_two_parents_is_ambiguous_not_guessed(self):
        from pipeline.competition.fallback_v2 import build_single_rider_parent

        ents = pd.DataFrame({"code": ["MK01", "MK21", "MK02"],
                             "name": ["Space Mountain", "Space Mountain", "Space Mountain Single Rider"]})
        df = build_single_rider_parent(posted_fixture(), ents, TODAY)
        self.assertEqual(df.iloc[0]["scale_source"], "unmapped:ambiguous")

    def test_park_must_match(self):
        from pipeline.competition.fallback_v2 import build_single_rider_parent

        ents = pd.DataFrame({"code": ["EP01", "MK02"],
                             "name": ["Space Mountain", "Space Mountain Single Rider"]})
        df = build_single_rider_parent(posted_fixture(), ents, TODAY)
        self.assertEqual(df.iloc[0]["scale_source"], "unmapped:no_parent")


@needs_pandas
class FallbackRatiosTest(unittest.TestCase):
    def ratios(self):
        from pipeline.competition.fallback_v2 import build_fallback_ratios

        return build_fallback_ratios(pairs_fixture(), entities_fixture(), TODAY)

    def test_entity_level_with_30_obs(self):
        r = self.ratios()["MK07"]
        self.assertEqual(r["level"], "entity")
        self.assertAlmostEqual(r["ratio"], 0.8)
        self.assertEqual(r["n_obs"], 40)  # the 50 out-of-window rows are ignored
        self.assertEqual(r["window"], "2025-10-24..2026-10-24")

    def test_show_with_no_entity_data_gets_category_ratio(self):
        r = self.ratios()["MK06"]  # "Fireworks Show": no pairs at all
        self.assertEqual(r["level"], "category")
        self.assertAlmostEqual(r["ratio"], 0.8)

    def test_entity_without_category_falls_to_global(self):
        out = self.ratios()
        for ent in ("MK08", "MK09"):  # no category, < 30 obs
            self.assertEqual(out[ent]["level"], "global", ent)
            self.assertAlmostEqual(out[ent]["ratio"], 0.7)

    def test_category_and_global_sections(self):
        out = self.ratios()
        self.assertEqual(set(out["_category"]), {"show"})  # other categories lack >= 30 obs
        self.assertAlmostEqual(out["_category"]["show"]["ratio"], 0.8)
        self.assertAlmostEqual(out["_global"]["ratio"], 0.7)
        self.assertEqual(out["_global"]["n_obs"], 83)

    def test_no_data_at_all_uses_default_global(self):
        from pipeline.competition.fallback_v2 import build_fallback_ratios

        empty = pd.DataFrame(columns=["entity_code", "park_date", "posted", "actual"])
        out = build_fallback_ratios(empty, entities_fixture(), TODAY)
        self.assertEqual(out["_global"]["ratio"], 0.678)
        self.assertEqual(out["MK06"]["level"], "global")

    def test_category_heuristics(self):
        from pipeline.competition.fallback_v2 import entity_category

        self.assertEqual(entity_category("Disney Starlight Parade"), "parade")
        self.assertEqual(entity_category("Waypoint Station"), "waypoint")
        self.assertEqual(entity_category("HHN House of Fear"), "hhn_house")
        self.assertEqual(entity_category("Anything", "parade"), "parade")
        self.assertIsNone(entity_category("Space Mountain"))


@needs_pandas
class ForecastFallbackV2Test(unittest.TestCase):
    def setUp(self):
        from pipeline.competition.fallback_v2 import (
            build_fallback_ratios, build_single_rider_parent, forecast_fallback_v2)

        self.entities = entities_fixture()
        self.parent_map = build_single_rider_parent(posted_fixture(), self.entities, TODAY)
        self.ratios = build_fallback_ratios(pairs_fixture(), self.entities, TODAY)

        rows = []

        def add(ent, preds, method):
            for slot, p in zip(SLOTS[:2], preds):
                rows.append((ent, "2026-10-25", slot, p, method, 10.0))

        add("MK01", [20, 30], "model_baseline")      # parent of MK02
        add("MK02", [99, 99], "fallback_ratio")      # child: replaced by parent * 0.5
        add("MK03", [40, 44], "model_baseline")      # parent of MK04 (default scale 1.0)
        add("MK04", [5, 5], "fallback_ratio")
        add("MK05", [7, 7], "fallback_ratio")        # unmapped child, ratio-served -> global ratio
        add("MK06", [7, 7], "fallback_ratio")        # show, no data -> category ratio
        add("MK07", [7, 7], "fallback_ratio")        # entity ratio
        add("MK08", [7, 7], "fallback_ratio")        # no category -> global
        add("MK09", [25, 26], "model_baseline")      # unchanged
        add("MK12", [3, 3], "fallback_ratio")        # child whose parent MK11 has no forecast
        self.prod = pd.DataFrame(rows, columns=[
            "entity_code", "park_date", "time_slot", "predicted_actual", "prediction_method", "posted_time"])
        self.out = forecast_fallback_v2(self.prod, self.parent_map, self.ratios, self.entities,
                                        prod_ratios={"__global__": 0.678})

    def rows(self, ent):
        return self.out[self.out["entity_code"] == ent].sort_values("time_slot")

    def test_schema_matches_other_challengers(self):
        from pipeline.competition.fallback_v2 import FORECAST_COLUMNS

        self.assertEqual(list(self.out.columns), FORECAST_COLUMNS)

    def test_children_are_parent_forecast_times_scale(self):
        mk02 = self.rows("MK02")
        self.assertEqual(list(mk02["predicted_actual"]), [10, 15])
        self.assertEqual(set(mk02["prediction_method"]), {"parent_scaled"})
        mk04 = self.rows("MK04")
        self.assertEqual(list(mk04["predicted_actual"]), [40, 44])  # default scale 1.0
        self.assertEqual(set(mk04["prediction_method"]), {"parent_scaled"})

    def test_child_without_parent_forecast_is_skipped(self):
        # MK12's parent MK11 has no production forecast: skipped, not sent to the ratio
        self.assertEqual(len(self.rows("MK12")), 0)

    def test_ratio_served_entities_use_their_level(self):
        expect = {"MK07": (8, "ratio_entity"), "MK06": (8, "ratio_category"),
                  "MK08": (7, "ratio_global"), "MK05": (7, "ratio_global")}
        for ent, (pred, method) in expect.items():
            r = self.rows(ent)
            self.assertEqual(set(r["predicted_actual"]), {pred}, ent)
            self.assertEqual(set(r["prediction_method"]), {method}, ent)

    def test_ratio_without_posted_column_recovers_posted_from_production_ratio(self):
        from pipeline.competition.fallback_v2 import forecast_fallback_v2

        prod = self.prod[self.prod["entity_code"] == "MK07"].drop(columns=["posted_time"]).copy()
        prod["predicted_actual"] = 7  # round(10 * 0.678) -> posted ~10.3 -> * 0.8 = 8
        out = forecast_fallback_v2(prod, self.parent_map, self.ratios, self.entities,
                                   prod_ratios={"__global__": 0.678})
        self.assertEqual(set(out["predicted_actual"]), {8})

    def test_non_fallback_rows_equal_production(self):
        key = ["entity_code", "park_date", "time_slot"]
        for ent in ("MK01", "MK03", "MK09"):
            prod = self.prod[self.prod["entity_code"] == ent].set_index(key)
            out = self.out[self.out["entity_code"] == ent].set_index(key)
            self.assertEqual(len(out), len(prod), ent)
            for col in ("predicted_actual", "prediction_method"):
                self.assertTrue((out.loc[prod.index, col] == prod[col]).all(), (ent, col))

    def test_does_not_mutate_input(self):
        self.assertEqual(list(self.prod.loc[self.prod["entity_code"] == "MK02", "predicted_actual"]), [99, 99])


# --------------------------------------------------------------------------
# Isolation: production must never import fallback-v2
# --------------------------------------------------------------------------

class FallbackV2IsolationTest(unittest.TestCase):
    def test_no_pipeline_step_imports_fallback_v2(self):
        files = sorted(STEPS_DIR.glob("*.py"))
        self.assertTrue(files)
        pipeline_entry = REPO_ROOT / "pipeline" / "pipeline.py"
        if pipeline_entry.exists():
            files.append(pipeline_entry)
        pattern = re.compile(r"fallback[_-]v2")
        hits = [f"{f.name}:{i}" for f in files
                for i, line in enumerate(f.read_text().splitlines(), 1) if pattern.search(line)]
        self.assertEqual(hits, [])

    def test_fallback_v2_outputs_stay_in_competition_namespace(self):
        text = (REPO_ROOT / "pipeline" / "competition" / "fallback_v2.py").read_text()
        writes = re.findall(r"atomic_write_\w+\(([^\n]*)", text)
        self.assertTrue(writes)
        self.assertTrue(all("comp /" in w or "out_dir" in w for w in writes), writes)
        self.assertNotIn("state_dir", text)
        self.assertNotIn("import pipeline.steps", text)
        self.assertNotIn("from pipeline.steps", text)

    def test_challenger_module_declares_metadata(self):
        from pipeline.competition.challengers import fallback_v2 as meta

        self.assertEqual(meta.NAME, "fallback-v2")
        self.assertEqual(meta.STATUS, "active")
        self.assertEqual(meta.SCORE_SCOPE, "fallback")
        self.assertTrue(meta.DESCRIPTION)


# --------------------------------------------------------------------------
# rolling_shadow: retrain gating, registry fields, dispatch, outputs
# --------------------------------------------------------------------------

class RetrainGatingTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_rolling_shadow()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mod.configure(pipeline_base=self.tmp.name, tpcr_dir=f"{self.tmp.name}/repo")
        self.now = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)

    def make_models(self, name):
        d = self.mod.COMPETITION_BASE / "models" / name
        d.mkdir(parents=True)
        (d / "MK01").mkdir()
        return d

    def test_missing_model_dir_triggers(self):
        ch = {"name": "chal-a", "trained_at": self.now.isoformat()}
        self.assertEqual(self.mod.needs_retrain(ch, now=self.now), "model dir missing")

    def test_fresh_model_does_not_retrain(self):
        self.make_models("chal-a")
        ch = {"name": "chal-a", "trained_at": (self.now - timedelta(days=6)).isoformat()}
        self.assertIsNone(self.mod.needs_retrain(ch, now=self.now))

    def test_older_than_7_days_triggers(self):
        self.make_models("chal-a")
        ch = {"name": "chal-a", "trained_at": (self.now - timedelta(days=8)).isoformat()}
        self.assertIn("older than 7 days", self.mod.needs_retrain(ch, now=self.now))

    def test_never_trained_and_force(self):
        self.make_models("chal-a")
        self.assertEqual(self.mod.needs_retrain({"name": "chal-a", "trained_at": None}, now=self.now),
                         "never trained")
        fresh = {"name": "chal-a", "trained_at": self.now.isoformat()}
        self.assertIn("--retrain", self.mod.needs_retrain(fresh, force=True, now=self.now))

    def test_module_name_selects_model_dir(self):
        self.make_models("mod_a")
        ch = {"name": "chal-a", "module_name": "mod_a", "trained_at": self.now.isoformat()}
        self.assertIsNone(self.mod.needs_retrain(ch, now=self.now))

    def test_fallback_v2_is_never_retrained(self):
        self.assertIsNone(self.mod.needs_retrain({"name": "fallback-v2"}, force=True, now=self.now))

    def test_record_training_sets_registry_fields_and_flags_mismatch(self):
        ch = {"name": "chal-a"}
        ok = {"trained_at": "2026-10-24T01:00:00+00:00", "entity_count": 333, "eligible_count": 333,
              "skipped": {}}
        self.assertIsNone(self.mod.record_training(ch, ok))
        self.assertEqual((ch["trained_at"], ch["entity_count"]), (ok["trained_at"], 333))

        bad = dict(ok, entity_count=331, skipped={"MK01": "not_trained", "MK02": "error:ValueError"})
        err = self.mod.record_training(ch, bad)
        self.assertEqual(ch["entity_count"], 331)
        self.assertIn("331", err)
        self.assertIn("333", err)
        self.assertIn("MK01=not_trained", err)

    def test_register_fallback_v2_is_idempotent(self):
        reg = self.mod.normalize_registry({"settings": {}, "challengers": [], "retired": []})
        self.assertTrue(self.mod.register_fallback_v2(reg))
        self.assertFalse(self.mod.register_fallback_v2(reg))
        entry = reg["challengers"][0]
        self.assertEqual((entry["name"], entry["status"], entry["score_scope"]),
                         ("fallback-v2", "active", "fallback"))
        self.assertEqual(self.mod.score_scope(entry), "fallback")
        self.assertEqual(self.mod.score_scope({"name": "chal-a"}), "all")

    def test_registry_metrics_uses_fallback_mae_for_fallback_scope(self):
        m = {"n_matched": 5, "baseline_mae": 4.0, "challenger_mae": 4.0,
             "mae_fallback_prod": 6.0, "mae_fallback_chal": 5.0}
        out = self.mod.registry_metrics(m, "fallback")
        self.assertEqual((out["baseline_mae"], out["challenger_mae"]), (6.0, 5.0))
        self.assertEqual(out["all_entity_baseline_mae"], 4.0)
        self.assertIs(self.mod.registry_metrics(m, "all"), m)
        no_fb = dict(m, mae_fallback_prod=None, mae_fallback_chal=None)
        self.assertEqual(self.mod.matched_pairs(self.mod.registry_metrics(no_fb, "fallback")), 0)

    def test_generate_forecasts_dispatches_fallback_v2(self):
        calls = []

        class Done:
            returncode = 0
            stdout = "RESULT:{}"
            stderr = ""

        def fake_run(module, args, timeout):
            calls.append((module, args))
            return Done()

        self.mod.run_module = fake_run
        self.assertTrue(self.mod.generate_forecasts({"name": "fallback-v2"}))
        self.assertTrue(self.mod.generate_forecasts({"name": "chal-a"}))
        self.assertEqual(calls[0][0], "pipeline.competition.fallback_v2")
        self.assertNotIn("--challenger", calls[0][1])
        self.assertEqual(calls[1][0], "pipeline.competition.forecast_challenger")


@needs_parquet
class DayOutputsTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_rolling_shadow()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mod.configure(pipeline_base=self.tmp.name, tpcr_dir=f"{self.tmp.name}/repo")
        self.base = self.mod.COMPETITION_BASE

    def stage(self, name, eval_date="2026-10-23"):
        from pipeline.competition.paired_scores import build_paired_scores

        prod, chal, actual = paired_fixture()
        scores, dropped = build_paired_scores(prod, chal, actual, name, eval_date, {"B"})
        path = self.mod.staging_path(name, eval_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        scores.to_parquet(path, index=False)
        return {
            "n_matched": len(scores), "baseline_mae": 3.0, "challenger_mae": 1.25, "n_entities": 2,
            "coverage_pct": 50.0, "forecast_entities": 4, "dropped": dropped,
            "mae_fallback_prod": 4.0, "mae_fallback_chal": 1.5, "n_fallback_entities": 1,
        }

    def test_rerun_overwrites_scores_without_duplicates_and_leaves_no_tmp(self):
        from pipeline.competition.paired_scores import SCORE_COLUMNS

        chal = {"name": "chal-a", "trained_at": "2026-10-20T00:00:00+00:00", "entity_count": 333}
        for _ in range(2):
            metrics = self.stage("chal-a")
            summary = self.mod.write_day_outputs("2026-10-23", [(chal, metrics)], [])
        scores = pd.read_parquet(self.base / "scores" / "2026-10-23.parquet")
        self.assertEqual(len(scores), 3)
        self.assertFalse(scores.duplicated(["challenger_id", "entity_code", "slot"]).any())
        self.assertEqual(list(scores.columns), SCORE_COLUMNS)
        self.assertEqual(sorted(p.name for p in (self.base / "scores").iterdir()), ["2026-10-23.parquet"])
        self.assertEqual(sorted(p.name for p in (self.base / "summaries").iterdir()), ["2026-10-23.json"])
        self.assertEqual(summary["run_status"], "ok")

    def test_summary_shape(self):
        chal = {"name": "chal-a", "trained_at": "2026-10-20T00:00:00+00:00", "entity_count": 333}
        v2 = {"name": "fallback-v2", "score_scope": "fallback"}
        idle = {"name": "chal-b"}
        results = [(chal, self.stage("chal-a")), (v2, self.stage("fallback-v2")), (idle, "no_data")]
        self.mod.write_day_outputs("2026-10-23", results, ["chal-b: forecast failed"])

        saved = json.loads((self.base / "summaries" / "2026-10-23.json").read_text())
        for key in ("date", "matched_pairs", "forecast_entities", "coverage_pct",
                    "challengers", "run_status", "errors"):
            self.assertIn(key, saved)
        self.assertEqual(saved["date"], "2026-10-23")
        self.assertEqual(saved["matched_pairs"], 3)
        self.assertEqual(saved["forecast_entities"], 4)
        self.assertEqual(saved["run_status"], "failed")
        self.assertEqual(saved["errors"], ["chal-b: forecast failed"])
        a = saved["challengers"]["chal-a"]
        for key in ("mae_prod", "mae_chal", "mae_fallback_prod", "mae_fallback_chal"):
            self.assertIn(key, a)
        self.assertEqual((a["mae_prod"], a["mae_chal"]), (3.0, 1.25))
        self.assertEqual(a["entity_count"], 333)
        self.assertEqual(saved["challengers"]["fallback-v2"]["score_scope"], "fallback")
        self.assertEqual(saved["challengers"]["chal-a"]["score_scope"], "all")
        self.assertEqual(saved["challengers"]["chal-b"]["status"], "no_data")

        scores = pd.read_parquet(self.base / "scores" / "2026-10-23.parquet")
        self.assertEqual(set(scores["challenger_id"]), {"chal-a", "fallback-v2"})
        self.assertEqual(len(scores), 6)

    def test_no_rows_leaves_existing_scores_untouched(self):
        chal = {"name": "chal-a"}
        self.mod.write_day_outputs("2026-10-23", [(chal, self.stage("chal-a"))], [])
        before = (self.base / "scores" / "2026-10-23.parquet").read_bytes()
        self.mod.write_day_outputs("2026-10-23", [(chal, "no_data")], [])
        self.assertEqual((self.base / "scores" / "2026-10-23.parquet").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()


class CoreEligibilityHookTest(unittest.TestCase):
    """With PR #522's pipeline.core.eligibility present, its rules decide (overrides + not_operating)."""

    def test_core_rules_replace_mirror_extinct(self):
        import types
        from unittest import mock
        from pipeline.competition import eligibility as el

        fake = types.SimpleNamespace(
            load_dimentity_extinct=lambda p: {"AK01": date(2026, 1, 1), "HS29": None},
            load_operating_calendar_indexed=lambda cfg, log: {"MK02": {date(2025, 1, 1)}},
            is_operating_in_window=lambda e, oc, s, t: not (e in oc and not any(s <= d <= t for d in oc[e])),
        )
        cfg = types.SimpleNamespace(dimension_dir=Path("."), forecast_days=30,
                                    training_min_obs_lite=1, ignore_parks=[])
        counts = {"AK01": 50, "HS29": 50, "MK02": 50, "MK03": 50}
        with mock.patch.object(el, "_core", return_value=fake), \
             mock.patch.object(el, "scan_training_counts", return_value=counts), \
             mock.patch("pipeline.core.logging.PipelineLogger", create=True):
            rep = el.eligibility_report(None, date(2026, 10, 24), cfg=cfg)
        self.assertEqual(rep["source"], "pipeline.core.eligibility")
        self.assertEqual(rep["eligible"], ["HS29", "MK03"])
        self.assertEqual(rep["excluded"], {"AK01": "extinct", "MK02": "not_operating"})
