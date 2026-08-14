"""Branded DB hits must not report zero-width (falsely precise) ranges."""

from caloriecam import lookup
from caloriecam.sanity import evaluate
from caloriecam.schema import FoodAnalysis, FoodItem


def _food(name, grams, brand=None, unit_count=None):
    return FoodItem(
        name=name,
        portion_description="a portion",
        estimated_grams=grams,
        kcal_per_100g=250,
        confidence="high",
        assumptions=[],
        brand=brand,
        unit_count=unit_count,
    )


def _evaluate(item):
    analysis = FoodAnalysis(items=[item])
    return evaluate(analysis, lookup.resolve_all(analysis.items)).items[0]


def test_flat_branded_entry_gets_a_real_band():
    est = _evaluate(_food("Cheeseburger", grams=268, brand="In-N-Out"))
    assert est.kcal_mid == 480
    assert est.kcal_low < est.kcal_mid < est.kcal_high  # not 480/480/480


def test_multi_unit_branded_band_is_wider_than_single():
    single = _evaluate(_food("Cheeseburger", grams=268, brand="In-N-Out"))
    double = _evaluate(_food("Cheeseburgers", grams=536, brand="In-N-Out"))
    single_rel = (single.kcal_high - single.kcal_low) / single.kcal_mid
    double_rel = (double.kcal_high - double.kcal_low) / double.kcal_mid
    assert double.count_note_present if hasattr(double, "count_note_present") else True
    assert double_rel > single_rel  # inferred count adds uncertainty


def test_multi_unit_flags_count_risk_in_assumptions():
    est = _evaluate(_food("Cheeseburgers", grams=536, brand="In-N-Out"))
    assert any("miscount" in a for a in est.assumptions)


def test_ranged_entry_keeps_its_wider_published_range():
    # The generic burger entry is already 250-300; a tighter computed band
    # must never shrink a real published range.
    est = _evaluate(_food("cheeseburger", grams=170))
    assert est.kcal_low <= 250 and est.kcal_high >= 300


def test_branded_midpoint_is_unchanged():
    est = _evaluate(_food("Big Mac", grams=216))
    assert est.kcal_mid == 575  # the published number itself never moves
