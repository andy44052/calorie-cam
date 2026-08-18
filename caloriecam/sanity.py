"""Layer 2: deterministic math on top of the vision output.

Recomputes calories ourselves, clamps implausible values, applies database
overrides from the lookup layer, and turns per-item confidence into a
low/mid/high kcal range. Everything here is pure Python and fully unit-tested.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from . import units
from .schema import FoodAnalysis, FoodItem
from .text import tokens as _tokens

if TYPE_CHECKING:
    from .lookup import Resolution

# Relative margin applied around the midpoint estimate. Portion size dominates
# the error, so the margins are wide on purpose.
CONFIDENCE_MARGIN = {"high": 0.15, "medium": 0.30, "low": 0.50}
DEFAULT_MARGIN = 0.50

# Even a published menu value carries risk from OUR reading of the photo
# (wrong size/variant), plus count risk when several units were inferred.
BRANDED_ITEM_MARGIN = 0.08
BRANDED_COUNT_MARGIN = 0.12

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
    # Pre-blend grams when personal-history blending moved this item; None
    # otherwise. The history store records THIS value, never the blended one,
    # so the prior can't feed on its own output.
    grams_raw: Optional[int] = None


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
    # Token/cost accounting for the API calls this estimate cost.
    usage: Optional[dict] = None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# Words meaning the counted unit is a CUT PIECE of the food, not the whole
# food a weight band describes. "12 sliced avocado crescents" x the whole-
# avocado band = 12 avocados (Run A shipped exactly that: a 1,920 kcal salad
# avocado). Identity-wise these words are harmless; unit-wise they are not.
_PIECE_WORDS = frozenset({
    "slice", "sliced", "slices", "crescent", "crescents", "wedge", "wedges",
    "chunk", "chunks", "cube", "cubed", "cubes", "strip", "strips",
    "sliver", "slivers", "segment", "segments", "shaving", "shavings",
})


def _unit_band_grams(raw: FoodItem, assumptions: list[str]) -> Optional[float]:
    """Recompute grams as count x per-unit weight, clamped to a known band.

    Counts are the stable part of a countable-food estimate; the per-unit
    weight is what drifts between runs. When we know the plausible weight of
    one unit, the count pins the total.
    """
    count = raw.unit_count
    if not count or count < 1:
        return None

    band = units.find_band(raw.name)
    if band is not None:
        # The band must weigh the same unit the model counted. If the item
        # name says the units are cut pieces and the band's own vocabulary
        # doesn't (a "maki piece" band legitimately weighs pieces), the band
        # describes a different unit - keep the model's own arithmetic.
        item_words = set(_tokens(raw.name)) & _PIECE_WORDS
        band_words = set(
            t for alias in (band.name, *band.aliases) for t in _tokens(alias)
        )
        if item_words and not (item_words & band_words):
            band = None
    per_unit = raw.per_unit_grams
    if per_unit is None or per_unit <= 0:
        if band is None:
            return None
        # The model counted but never priced a unit: fall back to the typical.
        per_unit = band.typical_g
        assumptions.append(
            f"per-unit weight not stated; used typical {per_unit:.0f} g for "
            f"'{band.name}'"
        )
    elif band is not None:
        clamped = band.clamp(per_unit)
        if clamped != per_unit:
            assumptions.append(
                f"per-unit weight adjusted {per_unit:.0f} g -> {clamped:.0f} g "
                f"(plausible range {band.low_g:.0f}-{band.high_g:.0f} g for "
                f"'{band.name}')"
            )
            per_unit = clamped

    grams = _clamp(count * per_unit, GRAMS_MIN, GRAMS_MAX)
    if abs(grams - raw.estimated_grams) > 0.05 * max(grams, raw.estimated_grams):
        assumptions.append(
            f"portion recomputed as {count} x {per_unit:.0f} g = {grams:.0f} g "
            f"(model stated {raw.estimated_grams:.0f} g)"
        )
    return grams


def _clamped_grams(raw: FoodItem, assumptions: list[str]) -> float:
    from_units = _unit_band_grams(raw, assumptions)
    if from_units is not None:
        return from_units
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

        # Published per-item values are exact, but OUR reading of the photo is
        # not: the item could be a different size/variant, and for multi-unit
        # hits the count itself was inferred. Without this a branded meal
        # reports a zero-width range - false precision.
        spread = BRANDED_ITEM_MARGIN
        if res.count > 1:
            spread += BRANDED_COUNT_MARGIN
            assumptions.append(
                f"count of {res.count} inferred from the photo - a miscount moves this "
                "item by one whole unit"
            )
        kcal_low = min(kcal_low, round(kcal_mid * (1 - spread)))
        kcal_high = max(kcal_high, round(kcal_mid * (1 + spread)))
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
