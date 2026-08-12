from caloriecam.sanity import (
    GRAMS_MAX,
    GRAMS_MIN,
    KCAL_PER_100G_MAX,
    SOURCE_MODEL,
    evaluate,
)
from caloriecam.schema import FoodAnalysis, FoodItem


def _item(**overrides) -> FoodItem:
    base = dict(
        name="test food",
        portion_description="a portion",
        estimated_grams=100,
        kcal_per_100g=200,
        confidence="high",
        assumptions=[],
        brand=None,
    )
    base.update(overrides)
    return FoodItem(**base)


def _analysis(*items: FoodItem) -> FoodAnalysis:
    return FoodAnalysis(items=list(items))


def test_basic_kcal_math():
    meal = evaluate(_analysis(_item(estimated_grams=100, kcal_per_100g=200)))
    assert meal.items[0].kcal_mid == 200


def test_known_meal_totals(sample_analysis):
    meal = evaluate(sample_analysis)
    chicken, rice = meal.items
    assert (chicken.kcal_low, chicken.kcal_mid, chicken.kcal_high) == (210, 248, 285)
    assert (rice.kcal_low, rice.kcal_mid, rice.kcal_high) == (182, 260, 338)
    assert (meal.total_low, meal.total_mid, meal.total_high) == (392, 508, 623)


def test_margins_by_confidence():
    for confidence, low, high in [
        ("high", 170, 230),
        ("medium", 140, 260),
        ("low", 100, 300),
    ]:
        meal = evaluate(_analysis(_item(confidence=confidence)))
        item = meal.items[0]
        assert item.kcal_mid == 200
        assert (item.kcal_low, item.kcal_high) == (low, high)


def test_absurd_grams_clamped_high():
    meal = evaluate(_analysis(_item(estimated_grams=5000)))
    item = meal.items[0]
    assert item.grams == GRAMS_MAX
    assert item.kcal_mid == round(GRAMS_MAX * 200 / 100)
    assert any("portion adjusted" in a for a in item.assumptions)


def test_absurd_grams_clamped_low():
    meal = evaluate(_analysis(_item(estimated_grams=0.2)))
    assert meal.items[0].grams == GRAMS_MIN
    assert any("portion adjusted" in a for a in meal.items[0].assumptions)


def test_absurd_energy_density_clamped():
    meal = evaluate(_analysis(_item(kcal_per_100g=1500)))
    item = meal.items[0]
    assert item.kcal_per_100g == KCAL_PER_100G_MAX
    assert any("energy density adjusted" in a for a in item.assumptions)


def test_negative_energy_density_clamped_to_zero():
    meal = evaluate(_analysis(_item(kcal_per_100g=-50)))
    item = meal.items[0]
    assert item.kcal_per_100g == 0
    assert item.kcal_mid == 0
    assert item.kcal_low == 0
    assert item.kcal_high == 0


def test_plausible_values_not_touched():
    meal = evaluate(_analysis(_item(estimated_grams=150, kcal_per_100g=165)))
    assert meal.items[0].assumptions == []


def test_original_assumptions_preserved():
    meal = evaluate(_analysis(_item(assumptions=["fried in oil"], estimated_grams=5000)))
    assert meal.items[0].assumptions[0] == "fried in oil"
    assert len(meal.items[0].assumptions) == 2


def test_empty_analysis(empty_analysis):
    meal = evaluate(empty_analysis)
    assert meal.items == []
    assert (meal.total_low, meal.total_mid, meal.total_high) == (0, 0, 0)
    assert meal.notes and "no food" in meal.notes.lower()


def test_source_is_model_estimate(sample_analysis):
    meal = evaluate(sample_analysis)
    assert all(item.source == SOURCE_MODEL for item in meal.items)


def test_grams_reported_as_int(sample_analysis):
    meal = evaluate(sample_analysis)
    assert all(isinstance(item.grams, int) for item in meal.items)


def test_metadata_carried_through(sample_analysis):
    meal = evaluate(sample_analysis)
    assert meal.scale_reference == "dinner plate"
    assert meal.notes is None
