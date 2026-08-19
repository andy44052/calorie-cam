"""CalorieCam benchmark harness — run a photo set, then report on it.

    python benchmark.py run photos/ --runs 3 --out results.jsonl --max-cost 15
    python benchmark.py report results.jsonl --truth truth.json
    python benchmark.py report new.jsonl --compare old.jsonl

A sweep costs real money (~20c per photo per run at current prices), so this
captures everything a run can tell us in one pass rather than making you pay
for a second sweep to answer the next question: per-item provenance, unit
counts, debate verdicts, and per-call token cost.

Runs are appended to the JSONL as they complete, so an interrupted sweep
resumes where it stopped instead of starting over.
"""

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv()

import anthropic  # noqa: E402

from caloriecam import pipeline  # noqa: E402
from caloriecam.config import DEFAULT_MODEL  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".gif", ".bmp"}


# --- running -----------------------------------------------------------------


def _capture(meal, analysis) -> dict:
    """Everything one run can tell us, so a later question needs no re-run."""
    debate = meal.debate or {}
    verdicts = debate.get("verdict_counts") or {}
    return {
        "total": {"low": meal.total_low, "mid": meal.total_mid, "high": meal.total_high},
        "items": [
            {
                "name": est.name,
                "brand": est.brand,
                "grams": est.grams,
                "kcal_per_100g": est.kcal_per_100g,
                "kcal_mid": est.kcal_mid,
                "confidence": est.confidence,
                "source": est.source,
                "unit_count": raw.unit_count,
                "per_unit_grams": raw.per_unit_grams,
                # The model's own pre-pipeline numbers, so later analysis can
                # measure exactly what each step changed (the Run A bias
                # decomposition was blocked on not having these).
                "model_grams": raw.estimated_grams,
                "model_kcal_per_100g": raw.kcal_per_100g,
                "assumptions": est.assumptions,
            }
            for est, raw in zip(meal.items, analysis.items)
        ],
        "scale_reference": meal.scale_reference,
        "notes": meal.notes,
        "debate": {
            "ran": meal.debate is not None,
            "challenges": len(debate.get("challenges", [])),
            "kinds": [c.get("kind") for c in debate.get("challenges", [])],
            "accepted": verdicts.get("accepted", 0),
            "partially_accepted": verdicts.get("partially_accepted", 0),
            "rejected": verdicts.get("rejected", 0),
        },
        "usage": meal.usage,
    }


def cmd_run(args: argparse.Namespace) -> int:
    photos = sorted(
        p for p in Path(args.photos).rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not photos:
        print(f"no images found under {args.photos}", file=sys.stderr)
        return 1

    out = Path(args.out)
    done: set[tuple[str, int]] = set()
    spent = 0.0
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ok"):
                done.add((rec["photo"], rec["run"]))
                spent += (rec.get("usage") or {}).get("total_cost_usd", 0.0)
    if done:
        print(f"resuming: {len(done)} runs already recorded (${spent:.2f} spent)")

    total_runs = len(photos) * args.runs
    print(f"{len(photos)} photos x {args.runs} runs = {total_runs} runs, model {args.model}")
    if args.max_cost:
        print(f"stopping if spend reaches ${args.max_cost:.2f}")

    client = anthropic.Anthropic()
    n = 0
    with out.open("a", encoding="utf-8") as fh:
        for photo in photos:
            for run in range(1, args.runs + 1):
                n += 1
                key = (photo.name, run)
                if key in done:
                    continue
                if args.max_cost and spent >= args.max_cost:
                    print(f"\nSTOPPED: ${spent:.2f} spent, cap is ${args.max_cost:.2f}")
                    print(f"re-run the same command to continue from here.")
                    return 0

                rec = {"photo": photo.name, "run": run, "model": args.model}
                started = time.time()
                try:
                    meal, analysis = pipeline.run(
                        photo, model=args.model, client=client, debate=not args.no_debate,
                        skeptic_model=args.skeptic_model,
                    )
                    rec["ok"] = True
                    rec.update(_capture(meal, analysis))
                except anthropic.RateLimitError:
                    print(f"  rate limited, waiting 60s")
                    time.sleep(60)
                    try:
                        meal, analysis = pipeline.run(
                            photo, model=args.model, client=client,
                            debate=not args.no_debate, skeptic_model=args.skeptic_model,
                        )
                        rec["ok"] = True
                        rec.update(_capture(meal, analysis))
                    except Exception as exc:  # noqa: BLE001
                        rec["ok"] = False
                        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
                except Exception as exc:  # noqa: BLE001
                    rec["ok"] = False
                    rec["error"] = f"{type(exc).__name__}: {exc}"[:300]

                rec["seconds"] = round(time.time() - started, 1)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()

                cost = (rec.get("usage") or {}).get("total_cost_usd", 0.0)
                spent += cost
                if rec.get("ok"):
                    print(
                        f"[{n}/{total_runs}] {photo.name[:34]:34} run{run}  "
                        f"{rec['total']['mid']:>5} kcal  {rec['seconds']:>5.1f}s  "
                        f"${cost:.4f}  (${spent:.2f} total)"
                    )
                else:
                    print(f"[{n}/{total_runs}] {photo.name[:34]:34} run{run}  FAILED: {rec['error'][:60]}")

    print(f"\ndone. ${spent:.2f} spent across {total_runs} runs.")
    return 0


# --- reporting ---------------------------------------------------------------


def _load(path: Path) -> list[dict]:
    return [
        rec
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
        for rec in [json.loads(line)]
        if rec.get("ok")
    ]


def _by_photo(runs: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for rec in runs:
        # Older sweeps keyed the photo as "img"/"file" before this harness
        # existed; "img" is the display name, "file" the full path. Keys are
        # normalized to the filename stem so eras compare ("img01" == "img01.jpg").
        photo = rec.get("photo") or rec.get("img") or rec.get("file")
        photo = Path(photo).stem if photo else None
        if photo is None:
            raise ValueError(
                "results file has no photo identifier on its runs - it was "
                "probably written by an older harness. Re-run the sweep with "
                "`benchmark.py run` to produce a readable file."
            )
        out.setdefault(photo, []).append(rec)
    return out


def _spread(totals: list[int]) -> float:
    if len(totals) < 2:
        return 0.0
    mean = statistics.mean(totals)
    return 0.0 if mean == 0 else 100.0 * (max(totals) - min(totals)) / mean


def cmd_report(args: argparse.Namespace) -> int:
    runs = _load(Path(args.results))
    if not runs:
        print("no successful runs in that file", file=sys.stderr)
        return 1
    try:
        grouped = _by_photo(runs)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"=== {len(runs)} runs over {len(grouped)} photos ===\n")

    # Stability
    print(f"{'photo':38} {'runs':>22} {'spread':>7}")
    spreads = []
    for photo, recs in sorted(grouped.items()):
        totals = [r["total"]["mid"] for r in sorted(recs, key=lambda r: r["run"])]
        sp = _spread(totals)
        spreads.append(sp)
        print(f"{photo[:38]:38} {'/'.join(str(t) for t in totals):>22} {sp:>6.0f}%")
    print(
        f"\nstability: median spread {statistics.median(spreads):.0f}%, "
        f"worst {max(spreads):.0f}%"
    )

    # Cost
    costs = [(r.get("usage") or {}).get("total_cost_usd", 0.0) for r in runs]
    secs = [r.get("seconds", 0) for r in runs]
    if any(costs):
        print(
            f"cost:      ${sum(costs):.2f} total, ${statistics.mean(costs):.4f}/run "
            f"(min ${min(costs):.4f}, max ${max(costs):.4f})"
        )
        print(f"latency:   {statistics.mean(secs):.0f}s mean, {max(secs):.0f}s worst")

    # Debate — the open question: is the reviser a rubber stamp?
    debated = [r for r in runs if (r.get("debate") or {}).get("ran")]
    if debated:
        acc = sum((r.get("debate") or {}).get("accepted", 0) for r in debated)
        part = sum((r.get("debate") or {}).get("partially_accepted", 0) for r in debated)
        rej = sum((r.get("debate") or {}).get("rejected", 0) for r in debated)
        ruled = acc + part + rej
        challenged = sum(1 for r in debated if (r.get("debate") or {}).get("challenges"))
        print(
            f"\ndebate:    {len(debated)}/{len(runs)} runs debated, "
            f"{challenged} drew challenges"
        )
        if ruled:
            print(
                f"  verdicts: {acc} accepted ({100*acc/ruled:.0f}%), "
                f"{part} partial ({100*part/ruled:.0f}%), "
                f"{rej} rejected ({100*rej/ruled:.0f}%)"
            )
            print(
                "  -> reviser is a rubber stamp only if accepted% is near 100 "
                "AND partial+rejected are near 0"
            )
        else:
            print("  (no per-verdict counts in this file - written before they were recorded)")
        kinds = Counter(k for r in debated for k in (r.get("debate") or {}).get("kinds", []))
        if kinds:
            print("  challenge kinds: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common(6)))
    # Only an explicit False means the gate fired; a missing field just means
    # the file predates the flag.
    skipped = [r for r in runs if (r.get("debate") or {}).get("ran") is False]
    if skipped:
        print(f"  skip-debate gate fired on {len(skipped)} runs")

    # Database coverage
    items = [it for r in runs for it in r.get("items", [])]
    if items:
        by_source = Counter(it.get("source", "unknown") for it in items)
        kcal_by_source: Counter = Counter()
        for it in items:
            kcal_by_source[it.get("source", "unknown")] += it.get("kcal_mid", 0)
        total_kcal = sum(kcal_by_source.values()) or 1
        print(f"\ndatabase:  {len(items)} items across all runs")
        for src, count in by_source.most_common():
            print(
                f"  {src:20} {count:>4} items  "
                f"{100*kcal_by_source[src]/total_kcal:>4.0f}% of kcal"
            )
        counted = sum(1 for it in items if it.get("unit_count"))
        print(f"  {counted} items used a unit count ({100*counted/len(items):.0f}%)")

    # Confidence calibration (needs truth)
    truth = None
    if args.truth:
        raw_truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
        truth = {Path(k).stem: v for k, v in raw_truth.items()}  # match photo keys
    if truth:
        print("\naccuracy vs truth:")
        errs = []
        for photo, recs in sorted(grouped.items()):
            if photo not in truth:
                continue
            expected = truth[photo]["kcal"] if isinstance(truth[photo], dict) else truth[photo]
            est = statistics.median(r["total"]["mid"] for r in recs)
            err = (est - expected) / expected
            errs.append(err)
            flag = "OK" if abs(err) <= 0.25 else ("HIGH" if err > 0 else "LOW")
            print(f"  {photo[:38]:38} truth {expected:>6}  est {est:>6.0f}  {err:>+6.0%}  {flag}")
        if errs:
            print(
                f"\n  MAPE {statistics.mean(abs(e) for e in errs):.0%}   "
                f"bias {statistics.mean(errs):+.0%}   "
                f"within 25%: {sum(1 for e in errs if abs(e) <= 0.25)}/{len(errs)}"
            )
        else:
            print("  (no photos in the results matched a key in the truth file)")

    # Baseline comparison
    if args.compare:
        base = _by_photo(_load(Path(args.compare)))
        print(f"\nvs {Path(args.compare).name}:")
        print(f"{'photo':38} {'before':>10} {'after':>10} {'change':>9}")
        for photo, recs in sorted(grouped.items()):
            if photo not in base:
                continue
            after = statistics.median(r["total"]["mid"] for r in recs)
            before = statistics.median(r["total"]["mid"] for r in base[photo])
            delta = (after - before) / before if before else 0
            sp_before = _spread([r["total"]["mid"] for r in base[photo]])
            sp_after = _spread([r["total"]["mid"] for r in recs])
            print(
                f"{photo[:38]:38} {before:>10.0f} {after:>10.0f} {delta:>+8.0%}"
                f"   spread {sp_before:.0f}% -> {sp_after:.0f}%"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a photo set N times")
    r.add_argument("photos", help="directory of photos (searched recursively)")
    r.add_argument("--runs", type=int, default=3)
    r.add_argument("--out", default="benchmark_results.jsonl")
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--no-debate", action="store_true")
    r.add_argument(
        "--skeptic-model", default=None,
        help="run critic+reviser on a cheaper model (A/B the Haiku skeptic)",
    )
    r.add_argument(
        "--max-cost", type=float, default=None,
        help="stop once this much has been spent (resume by re-running)",
    )
    r.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="analyze a results file")
    p.add_argument("results")
    p.add_argument("--truth", help='JSON: {"photo.jpg": {"kcal": 420}, ...}')
    p.add_argument("--compare", help="an earlier results file to diff against")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
