"""Glue: photo (path or raw bytes) in, (MealEstimate, raw FoodAnalysis) out."""

import io
import statistics
from pathlib import Path
from typing import Optional

import anthropic

from . import debate as debate_mod
from . import history as history_mod
from . import lookup
from .config import (
    DEBATE_MIN_ITEMS,
    DEBATE_WIDTH_TRIGGER,
    DEFAULT_MODEL,
)
from .sanity import (
    GRAMS_MAX,
    GRAMS_MIN,
    SOURCE_DB_BRANDED,
    SOURCE_MODEL,
    MealEstimate,
    evaluate,
)
from .schema import FoodAnalysis
from .usage import UsageLedger
from .vision import analyze_prepared, prepare_image


def needs_debate(analysis: FoodAnalysis) -> bool:
    """Decide per photo whether the draft is worth a skeptic pass.

    Pay for the second opinion only when the draft is uncertain: an item with
    no database anchor, enough items that one is easy to miss, or a wide
    uncertainty band. The inputs come from a free offline preview - the
    database lookup and sanity math are deterministic Python, so running them
    on the draft costs nothing and the real lookup still happens afterwards on
    whatever the debate produces.
    """
    if not analysis.items:
        return False
    if len(analysis.items) >= DEBATE_MIN_ITEMS:
        return True

    preview = evaluate(analysis, lookup.resolve_all(analysis.items))
    if any(item.source == SOURCE_MODEL for item in preview.items):
        return True
    width = (preview.total_high - preview.total_low) / max(preview.total_mid, 1)
    return width > DEBATE_WIDTH_TRIGGER


# A history row only counts as "the same food" if its energy density agrees
# with today's item. Density is a property of the food, so a big mismatch
# means the name key collided with a different food ("grilled cheese" keying
# onto plain cheese: 225 vs 400 kcal/100g) - blending across that boundary
# silently corrupts the estimate.
_BLEND_DENSITY_MAX_RATIO = 1.6


def _same_food_density(est_kcal100: float, past: list[dict]) -> bool:
    densities = [
        r["kcal_per_100g"] for r in past
        if r["kcal_per_100g"] is not None and r["kcal_per_100g"] > 0
    ]
    if not densities or est_kcal100 <= 0:
        return True  # nothing to compare - trust the name key
    med = statistics.median(densities)
    return max(med, est_kcal100) / min(med, est_kcal100) <= _BLEND_DENSITY_MAX_RATIO


def _blend_with_history(
    meal: MealEstimate, analysis: FoodAnalysis, store: "history_mod.HistoryStore"
) -> None:
    """Step 5.5: shrink repeat foods toward the user's own median portion.

    Only the portion moves - energy density is a property of the food, not
    the user. Skipped for items the unit clamp already pinned (a count times
    a known per-unit weight beats a prior) and for branded menu items (a Big
    Mac is a Big Mac).
    """
    blended_any = False
    for est, raw in zip(meal.items, analysis.items):
        if raw.unit_count:
            continue
        if est.source == SOURCE_DB_BRANDED:
            continue
        if est.grams <= 0:
            continue
        past_rows = store.past_portions(est.name)
        if len(past_rows) < history_mod.BLEND_MIN_PAST:
            continue
        if not _same_food_density(est.kcal_per_100g, past_rows):
            continue
        past = [r["grams"] for r in past_rows]
        blended = history_mod.blend_grams(est.grams, past)
        blended = max(GRAMS_MIN, min(GRAMS_MAX, blended))
        if abs(blended - est.grams) < 1.0:
            continue

        factor = blended / est.grams
        est.grams_raw = est.grams
        est.grams = round(blended)
        est.kcal_low = round(est.kcal_low * factor)
        est.kcal_mid = round(est.kcal_mid * factor)
        est.kcal_high = round(est.kcal_high * factor)
        est.assumptions.append(
            f"portion blended with your meal history: {est.grams_raw} g -> "
            f"{est.grams} g (median of your last {len(past)})"
        )
        blended_any = True

    if blended_any:
        meal.total_low = sum(i.kcal_low for i in meal.items)
        meal.total_mid = sum(i.kcal_mid for i in meal.items)
        meal.total_high = sum(i.kcal_high for i in meal.items)


def _run(
    source,
    model: str,
    client,
    hint,
    debate: bool,
    skeptic_model: Optional[str],
    history: Optional["history_mod.HistoryStore"],
) -> tuple[MealEstimate, FoodAnalysis]:
    ledger = UsageLedger()
    image_b64, media_type = prepare_image(source)
    if client is None:
        client = anthropic.Anthropic()

    analysis = analyze_prepared(
        image_b64, media_type, model=model, client=client, hint=hint, ledger=ledger
    )

    record = None
    if debate and needs_debate(analysis):
        analysis, record = debate_mod.run_debate(
            image_b64, media_type, analysis, model=model, client=client, hint=hint,
            ledger=ledger, skeptic_model=skeptic_model,
        )

    resolutions = lookup.resolve_all(analysis.items)
    meal = evaluate(analysis, resolutions)
    if history is not None:
        _blend_with_history(meal, analysis, history)
    meal.debate = record
    meal.usage = ledger.as_dict()
    return meal, analysis


def run(
    path: str | Path,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: str | None = None,
    debate: bool = True,
    skeptic_model: str | None = None,
    history: "history_mod.HistoryStore | None" = None,
) -> tuple[MealEstimate, FoodAnalysis]:
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    return _run(path, model, client, hint, debate, skeptic_model, history)


def run_bytes(
    data: bytes,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: str | None = None,
    debate: bool = True,
    skeptic_model: str | None = None,
    history: "history_mod.HistoryStore | None" = None,
) -> tuple[MealEstimate, FoodAnalysis]:
    return _run(io.BytesIO(data), model, client, hint, debate, skeptic_model, history)
