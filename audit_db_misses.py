"""Why do model_estimate items miss the database? Classify every miss.

    python audit_db_misses.py results.jsonl --csv misses.csv

Runs each item from a benchmark sweep through the CURRENT matcher (so fixes
since the sweep are credited), then sorts the remaining misses into:

    matched_now        the current matcher already handles it - no work needed
    branded_unmatched  chain item absent from fastfood.json - data growth
    composite          descriptive multi-food dish - decomposition candidate
    near_miss_alias    shares a food token with a DB entry - alias candidate
    not_in_db          genuinely absent single food - data growth

Impact = runs x median kcal, so the output ranks where the calories are,
not where the item count is. $0 - fully offline.
"""

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from caloriecam.lookup import _generic_foods, _menu_items, resolve
from caloriecam.schema import FoodItem
from caloriecam.text import MODIFIERS, tokens

SOURCE_MODEL = "model_estimate"

# Words that join foods into a described dish rather than naming one food.
_CONNECTORS = {"with", "and", "in", "on", "over", "topped", "served", "plus"}


def _db_token_set() -> set[str]:
    vocab: set[str] = set()
    for entry in _generic_foods():
        for alias in [entry["name"], *entry["aliases"]]:
            vocab.update(t for t in tokens(alias) if t not in MODIFIERS)
    for entry in _menu_items():
        for alias in [entry["item"], *entry["aliases"]]:
            vocab.update(t for t in tokens(alias) if t not in MODIFIERS)
    return vocab


def classify(name: str, brand: str | None, grams: float, kcal100: float,
             db_tokens: set[str]) -> tuple[str, str]:
    """Return (cause, note)."""
    probe = FoodItem(
        name=name,
        portion_description="audit probe",
        estimated_grams=grams or 150.0,
        kcal_per_100g=kcal100 or 200.0,
        confidence="medium",
        assumptions=[],
        brand=brand or None,
    )
    res = resolve(probe)
    if res is not None:
        return "matched_now", f"current matcher -> {res.matched_name}"

    if brand:
        return "branded_unmatched", f"brand {brand!r} not in fastfood.json"

    words = tokens(name)
    core = [t for t in words if t not in MODIFIERS]
    if any(w in _CONNECTORS for w in words) or len(core) >= 4:
        return "composite", "described dish - decomposition candidate"

    overlap = sorted(set(core) & db_tokens)
    if overlap:
        return "near_miss_alias", f"shares {overlap} with DB entries"
    return "not_in_db", "no token overlap with any DB entry"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("results", help="benchmark results JSONL")
    parser.add_argument("--csv", default="misses.csv")
    args = parser.parse_args(argv)

    runs = [
        rec
        for line in Path(args.results).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
        for rec in [json.loads(line)]
        if rec.get("ok")
    ]
    if not runs:
        print("no successful runs in that file", file=sys.stderr)
        return 1

    # Aggregate per distinct item name so one food misfiring in 3 runs is one
    # row with count 3, not three rows.
    agg: dict[str, dict] = defaultdict(lambda: {"count": 0, "kcals": [], "gramss": [],
                                                "k100s": [], "brand": None})
    total_kcal_all = 0.0
    model_kcal = 0.0
    for rec in runs:
        for it in rec.get("items", []):
            total_kcal_all += it.get("kcal_mid", 0)
            if it.get("source") != SOURCE_MODEL:
                continue
            model_kcal += it.get("kcal_mid", 0)
            row = agg[it["name"].strip().lower()]
            row["count"] += 1
            row["kcals"].append(it.get("kcal_mid", 0))
            row["gramss"].append(it.get("grams", 0) or 0)
            row["k100s"].append(it.get("kcal_per_100g", 0) or 0)
            row["brand"] = row["brand"] or it.get("brand")

    db_tokens = _db_token_set()
    rows = []
    for name, d in agg.items():
        med_kcal = statistics.median(d["kcals"])
        cause, note = classify(
            name, d["brand"], statistics.median(d["gramss"]),
            statistics.median(d["k100s"]), db_tokens,
        )
        rows.append({
            "name": name, "cause": cause, "runs": d["count"],
            "median_kcal": round(med_kcal),
            "impact": round(d["count"] * med_kcal),
            "note": note,
        })
    rows.sort(key=lambda r: -r["impact"])

    with open(args.csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # Summary: where the unbacked calories actually are.
    by_cause_kcal: dict[str, float] = defaultdict(float)
    by_cause_n: dict[str, int] = defaultdict(int)
    for r in rows:
        by_cause_kcal[r["cause"]] += r["impact"]
        by_cause_n[r["cause"]] += 1
    impact_total = sum(by_cause_kcal.values()) or 1

    print(f"{len(runs)} runs; model_estimate kcal share of sweep: "
          f"{100 * model_kcal / (total_kcal_all or 1):.0f}%")
    print(f"{len(rows)} distinct missed foods -> {args.csv}\n")
    print(f"{'cause':20} {'foods':>6} {'share of unbacked kcal':>24}")
    for cause in sorted(by_cause_kcal, key=lambda c: -by_cause_kcal[c]):
        print(f"{cause:20} {by_cause_n[cause]:>6} "
              f"{100 * by_cause_kcal[cause] / impact_total:>23.0f}%")

    print("\ntop 15 by impact:")
    for r in rows[:15]:
        print(f"  {r['impact']:>7}  {r['cause']:18} {r['name'][:44]:44} {r['note'][:48]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
