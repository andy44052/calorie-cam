"""Fit calorie-calibration factors from your own verified meals.

    python calibrate.py show           # current factors + how many gold meals exist
    python calibrate.py fit            # fit per-source factors and write them
    python calibrate.py fit --dry-run  # show what would be written

Gold meals come from the app's correction box with "measured" ticked - a
weighed meal or a package label, not an eyeball guess. The fit solves
ridge-regularized least squares (shrinking toward 1.0, hard for small
samples), clamps factors to a modest band, and refuses to write anything
from fewer than MIN_PAIRS meals: a calibration layer must never become a
second source of error.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from caloriecam import calibration, history  # noqa: E402

SOURCES = ["database_generic", "database_branded", "model_estimate"]

# Ridge weight: with n meals the prior pulls factors toward 1.0 with strength
# ~PRIOR_K/(n+PRIOR_K) - at 10 meals the data carries ~62%, at 30 meals ~83%.
PRIOR_K = 6.0


def _solve(pairs: list[dict]) -> tuple[dict, dict]:
    """Ridge least squares: truth ~= sum_s factor_s * est_kcal_s."""
    n = len(pairs)
    p = len(SOURCES)
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    counts = {s: 0 for s in SOURCES}
    for pair in pairs:
        x = [pair["by_source"].get(s, 0.0) or 0.0 for s in SOURCES]
        for s, v in zip(SOURCES, x):
            if v:
                counts[s] += 1
        for i in range(p):
            xty[i] += x[i] * pair["truth"]
            for j in range(p):
                xtx[i][j] += x[i] * x[j]

    # Ridge toward 1.0, scaled to the data's own magnitude so the prior's
    # strength is sample-size-driven, not unit-driven.
    trace = sum(xtx[i][i] for i in range(p)) or 1.0
    lam = (trace / p) * (PRIOR_K / (n + PRIOR_K))
    for i in range(p):
        xtx[i][i] += lam
        xty[i] += lam * 1.0

    # Gaussian elimination (p=3; no numpy dependency).
    a = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    for col in range(p):
        pivot = max(range(col, p), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        if abs(a[col][col]) < 1e-12:
            a[col][col] = 1.0  # source never observed; ridge keeps it at 1.0
        for r in range(p):
            if r != col:
                ratio = a[r][col] / a[col][col]
                for c in range(col, p + 1):
                    a[r][c] -= ratio * a[col][c]
    factors = {
        s: max(calibration.FACTOR_MIN, min(calibration.FACTOR_MAX, a[i][p] / a[i][i]))
        for i, s in enumerate(SOURCES)
    }
    return factors, counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="current factors and gold-meal count")
    fit = sub.add_parser("fit", help="fit factors from verified meals")
    fit.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    store = history.default_store()
    if store is None:
        print("history is disabled (CALORIECAM_HISTORY=off) - nothing to fit on",
              file=sys.stderr)
        return 1
    pairs = store.verified_truths()

    if args.cmd == "show":
        print(f"verified (gold) meals: {len(pairs)}  (need {calibration.MIN_PAIRS} to fit)")
        print(f"active calibration: {calibration.provenance()}")
        for s in SOURCES:
            print(f"  {s:20} x{calibration.factor_for(s):.3f}")
        return 0

    if len(pairs) < calibration.MIN_PAIRS:
        print(f"only {len(pairs)} verified meals - need {calibration.MIN_PAIRS}.")
        print('mark measured meals via the correction box with "measured" ticked.')
        return 1

    factors, counts = _solve(pairs)
    # A source seen in fewer than 3 gold meals keeps factor 1.0 - one meal's
    # worth of evidence must not steer every future estimate from that source.
    for s in SOURCES:
        if counts[s] < 3:
            factors[s] = 1.0

    err_before = [
        (sum(p["by_source"].values()) - p["truth"]) / p["truth"] for p in pairs
    ]
    err_after = [
        (sum(factors[s] * (p["by_source"].get(s) or 0.0) for s in SOURCES) - p["truth"])
        / p["truth"]
        for p in pairs
    ]
    mean = lambda xs: sum(xs) / len(xs)  # noqa: E731
    print(f"fitted on {len(pairs)} verified meals "
          f"(per-source coverage: {counts})")
    for s in SOURCES:
        print(f"  {s:20} x{factors[s]:.3f}")
    print(f"bias  before {mean(err_before):+.1%}  ->  after {mean(err_after):+.1%}")
    print(f"MAPE  before {mean([abs(e) for e in err_before]):.1%}"
          f"  ->  after {mean([abs(e) for e in err_after]):.1%}   (in-sample)")

    if args.dry_run:
        print("(dry run - nothing written)")
        return 0

    payload = {
        "factors": factors,
        "fitted_on_meals": len(pairs),
        "per_source_meals": counts,
        "fitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out = Path(__file__).parent / "caloriecam" / "data" / "calibration.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    calibration.reload()
    print(f"written -> {out}")
    print("note: in-sample numbers flatter the fit; judge it on the NEXT "
          "benchmark sweep / next week's verified meals.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
