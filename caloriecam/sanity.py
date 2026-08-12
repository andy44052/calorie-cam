"""Layer 2: deterministic math on top of the vision output.

Recomputes calories ourselves, clamps implausible values, applies database
overrides from the lookup layer, and turns per-item confidence into a
low/mid/high kcal range. Everything here is pure Python and fully unit-tested.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .schema import FoodAnalysis, FoodItem

if TYPE_CHECKING:
    from .lookup import Resolution

# Relative margin applied around the midpoint estimate. Portion size dominates
# the error, so the margins are wide on purpose.
CONFIDENCE_MARGIN = {"high": 0.15, "medium": 0.30, "low": 0.50}
DEFAULT_MARGIN = 0.50

# Plausibility bounds. Pure oil is ~884 kcal/100g, so nothing edible exceeds ~900.
GRAMS_MIN, GRAMS_MAX = 1.0, 2000.0
KCAL_PER_100G_MIN, KCAL_PER_100G_MAX = 0.0, 900.0

# Provenance of the numbers, worst to best.
SOURCE_MODEL = "model_estimate"
SOURCE_DB_GENERIC = "database_generic"
SOURCE_DB_BRANDED = "database_branded"


@dataclass
class ItemEstimate:
    name: str
    grams: int
    kcal_per_100g: float
    kcal_low: int
    kcal_mid: int
    kcal_high: int
    confidence: str
    assumptions: list[str]
    brand: Optional[str]
    source: str


@dataclass
class MealEstimate:
    items: list[ItemEstimate]
    total_low: int
    total_mid: int
    total_high: int
    scale_reference: Optional[str]
    notes: Optional[str]
    # Adversarial-review record (challenges + rulings); None when no debate ran.
    debate: Optional[dict] = None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _clamped_grams(raw: FoodItem, assumptions: list[str]) -> float:
    grams = _clamp(raw.estimated_grams, GRAMS_MIN, GRAMS_MAX)
    if grams != raw.estimated_grams:
        assumptions.append(
            f"portion adjusted from {raw.estimated_grams:.0f} g to {grams:.0f} g "
            "(outside plausible range)"
        )
    return grams


def _evaluate_item(raw: FoodItem, res: "Optional[Resolution]") -> ItemEstimate:
    assumptions = list(raw.assumptions)

    if res is not None and res.kcal_mid is not None:
        # Menu-item hit: the database gives total kcal for a standardized item,
        # so the grams x density math does not apply.
        note = f"matched menu item '{res.matched_name}'"
        if res.count > 1:
            note += f" x{res.count}"
        assumptions.append(note + " - calories from food database")
        if res.serving_g:
            grams = res.serving_g * res.count
        else:
            grams = _clamped_grams(raw, assumptions)
        kcal_low = (res.kcal_low if res.kcal_low is not None else res.kcal_mid) * res.count
        kcal_high = (res.kcal_high if res.kcal_high is not None else res.kcal_mid) * res.count
        kcal_mid = res.kcal_mid * res.count
        return ItemEstimate(
            name=raw.name,
            grams=round(grams),
            kcal_per_100g=round(kcal_mid * 100 / grams, 1) if grams else 0.0,
            kcal_low=round(kcal_low),
            kcal_mid=round(kcal_mid),
            kcal_high=round(kcal_high),
            confidence=raw.confidence,
            assumptions=assumptions,
            brand=raw.brand,
            source=res.source,
        )

    if res is not None and res.kcal_per_100g is not None:
        # Generic-food hit: trust the database energy density, keep the model's
        # portion estimate (portion is still the dominant uncertainty).
        kcal100 = res.kcal_per_100g
        assumptions.append(f"energy density from food database ('{res.matched_name}')")
        source = res.source
    else:
        kcal100 = _clamp(raw.kcal_per_100g, KCAL_PER_100G_MIN, KCAL_PER_100G_MAX)
        if kcal100 != raw.kcal_per_100g:
            assumptions.append(
                f"energy density adjusted from {raw.kcal_per_100g:.0f} to {kcal100:.0f} "
                "kcal/100g (outside plausible range)"
            )
        source = SOURCE_MODEL

    grams = _clamped_grams(raw, assumptions)
    margin = CONFIDENCE_MARGIN.get(raw.confidence, DEFAULT_MARGIN)
    mid = grams * kcal100 / 100.0

    return ItemEstimate(
        name=raw.name,
        grams=round(grams),
        kcal_per_100g=kcal100,
        kcal_low=round(mid * (1 - margin)),
        kcal_mid=round(mid),
        kcal_high=round(mid * (1 + margin)),
        confidence=raw.confidence,
        assumptions=assumptions,
        brand=raw.brand,
        source=source,
    )


def evaluate(
    analysis: FoodAnalysis,
    resolutions: "Optional[list[Optional[Resolution]]]" = None,
) -> MealEstimate:
    if resolutions is None:
        resolutions = [None] * len(analysis.items)

    items = [_evaluate_item(raw, res) for raw, res in zip(analysis.items, resolutions)]

    return MealEstimate(
        items=items,
        total_low=sum(i.kcal_low for i in items),
        total_mid=sum(i.kcal_mid for i in items),
        total_high=sum(i.kcal_high for i in items),
        scale_reference=analysis.scale_reference,
        notes=analysis.overall_notes,
    )
