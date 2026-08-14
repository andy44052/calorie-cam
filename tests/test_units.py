"""Per-unit weight bands: count x unit weight beats an eyeballed pile."""

from caloriecam import units
from caloriecam.sanity import evaluate
from caloriecam.schema import FoodAnalysis, FoodItem


def _food(name, grams, unit_count=None, per_unit_grams=None, kcal=200.0):
    return FoodItem(
        name=name,
        portion_description="a portion",
        estimated_grams=grams,
        kcal_per_100g=kcal,
        confidence="medium",
        assumptions=[],
        brand=None,
        unit_count=unit_count,
        per_unit_grams=per_unit_grams,
    )


def _est(item):
    return evaluate(FoodAnalysis(items=[item])).items[0]


# --- band lookup -------------------------------------------------------------


def test_band_data_is_sane():
    bands = units._bands()
    assert len(bands) >= 40, "units.json should cover a useful set of foods"
    seen = set()
    for _alias_tokens, band in bands:
        if band.name in seen:
            continue
        seen.add(band.name)
        assert 0 < band.low_g <= band.typical_g <= band.high_g, band.name
        assert band.high_g / band.low_g <= 6, f"{band.name} band too wide to be useful"


def test_find_band_matches_plain_and_qualified_names():
    assert units.find_band("large egg") is not None
    assert units.find_band("egg") is not None
    assert units.find_band("lemon bar") is not None


def test_multimodal_foods_are_deliberately_absent():
    """A 15-70 g "cookie" band cannot catch an error, so it is not shipped.

    Falling back to the model's own estimate beats clamping into a band so
    wide that every guess fits inside it.
    """
    for name in ["chocolate chip cookie", "cracker", "waffle", "meatball"]:
        assert units.find_band(name) is None, name


def test_ingredient_row_cannot_claim_a_composed_dish():
    # "potato samosa" must not weigh a whole potato (the chili-fries pattern)
    assert units.find_band("potato samosa") is None
    assert units.find_band("carrot cake slice").name != "Carrot (whole)"
    assert units.find_band("egg roll").name != "Large egg"


def test_colour_and_varietal_words_do_not_block_a_match():
    for name in ["wine grapes", "red grapes", "green apple", "black olives"]:
        assert units.find_band(name) is not None, name


def test_longest_alias_wins():
    assert units.find_band("baby carrots").name == "Baby carrot"
    assert units.find_band("mandarin orange").name == "Mandarin"


def test_find_band_ignores_bulk_foods():
    assert units.find_band("steamed white rice") is None
    assert units.find_band("tomato soup") is None


# --- clamping behaviour ------------------------------------------------------


def test_out_of_band_unit_weight_is_pulled_to_the_edge():
    # 15 lemon bars at an implausible 200 g each
    est = _est(_food("lemon bars", grams=3000, unit_count=15, per_unit_grams=200))
    assert est.grams < 3000
    assert any("per-unit weight adjusted" in a for a in est.assumptions)


def test_in_band_unit_weight_is_respected():
    est = _est(_food("chocolate chip cookies", grams=90, unit_count=3, per_unit_grams=30))
    assert est.grams == 90
    assert not any("per-unit weight adjusted" in a for a in est.assumptions)


def test_count_times_weight_overrides_contradictory_grams():
    # Model counted 4 eggs at 50 g but wrote 400 g for the pile
    est = _est(_food("fried eggs", grams=400, unit_count=4, per_unit_grams=50))
    assert est.grams == 200
    assert any("portion recomputed" in a for a in est.assumptions)


def test_missing_per_unit_weight_uses_typical():
    est = _est(_food("large eggs", grams=999, unit_count=2))
    assert 80 <= est.grams <= 130  # 2 x ~50 g, not the absurd 999
    assert any("per-unit weight not stated" in a for a in est.assumptions)


def test_unknown_countable_food_keeps_model_math():
    est = _est(_food("mystery pastry", grams=300, unit_count=3, per_unit_grams=100))
    assert est.grams == 300  # count x weight agrees; nothing to clamp


def test_bulk_food_unaffected():
    est = _est(_food("steamed white rice", grams=250))
    assert est.grams == 250


def test_zero_or_missing_count_is_ignored():
    assert _est(_food("cookies", grams=120, unit_count=0)).grams == 120
    assert _est(_food("cookies", grams=120)).grams == 120


def test_lemon_bar_regression():
    """The measured failure: counts stable at 15, weights swinging 40-53 g."""
    grams = [
        _est(_food("lemon bars", grams=15 * w, unit_count=15, per_unit_grams=w)).grams
        for w in (40, 45, 53)
    ]
    spread = (max(grams) - min(grams)) / (sum(grams) / len(grams))
    assert spread < 0.35  # bands keep the swing bounded
