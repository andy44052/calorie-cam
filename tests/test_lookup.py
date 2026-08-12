from types import SimpleNamespace

from caloriecam import lookup, pipeline, report
from caloriecam.lookup import _generic_foods, _menu_items, match_generic, match_menu_item, resolve
from caloriecam.sanity import SOURCE_DB_BRANDED, SOURCE_DB_GENERIC, SOURCE_MODEL, evaluate
from caloriecam.schema import FoodAnalysis, FoodItem


def _food(name, grams=100.0, kcal=200.0, confidence="high", brand=None) -> FoodItem:
    return FoodItem(
        name=name,
        portion_description="a portion",
        estimated_grams=grams,
        kcal_per_100g=kcal,
        confidence=confidence,
        assumptions=[],
        brand=brand,
    )


# --- data integrity ----------------------------------------------------------


def test_menu_data_is_sane():
    items = _menu_items()
    assert len(items) >= 10
    for entry in items:
        kcal = entry["kcal"]
        assert 0 < kcal["low"] <= kcal["mid"] <= kcal["high"] < 3000, entry["item"]
        assert entry["aliases"], entry["item"]
        if entry.get("unit_scalable"):
            assert entry.get("serving_g"), f"{entry['item']} scalable but no serving_g"
        assert isinstance(entry.get("loose_match", False), bool), entry["item"]
        assert all(isinstance(t, str) for t in entry.get("exclude_tokens", [])), entry["item"]


def test_generic_data_is_sane():
    foods = _generic_foods()
    assert len(foods) >= 80
    for entry in foods:
        assert 0 < entry["kcal_per_100g"] <= 900, entry["name"]
        assert entry["aliases"], entry["name"]
        assert isinstance(entry.get("loose_match", False), bool), entry["name"]
        assert all(isinstance(t, str) for t in entry.get("exclude_tokens", [])), entry["name"]


# --- menu-item matching ------------------------------------------------------


def test_big_mac_with_brand_field():
    res = match_menu_item(_food("Big Mac", grams=220, brand="McDonald's"))
    assert res is not None
    assert res.source == SOURCE_DB_BRANDED
    assert (res.kcal_low, res.kcal_mid, res.kcal_high) == (560, 575, 590)


def test_brand_normalization_without_apostrophe():
    res = match_menu_item(_food("big mac", grams=220, brand="mcdonalds"))
    assert res is not None and res.kcal_mid == 575


def test_brand_inside_name_is_enough():
    res = match_menu_item(_food("McDonald's Big Mac", grams=220, brand=None))
    assert res is not None and res.kcal_mid == 575


def test_wrong_brand_blocks_branded_match():
    assert resolve(_food("big mac", grams=220, brand="Burger King")) is None


def test_fries_sizes_disambiguate():
    res = match_menu_item(_food("medium french fries", grams=115, brand="McDonald's"))
    assert res is not None and res.kcal_mid == 340


def test_unsized_fries_fall_through_to_generic():
    item = _food("french fries", grams=120, brand=None)
    assert match_menu_item(item) is None
    res = match_generic(item)
    assert res is not None
    assert res.kcal_per_100g == 307


def test_whopper_vs_double_whopper():
    single = match_menu_item(_food("Whopper", grams=290, brand="Burger King"))
    double = match_menu_item(_food("Double Whopper", grams=380, brand="Burger King"))
    assert single is not None and single.kcal_mid == 690
    assert double is not None and double.kcal_mid == 950


def test_brandless_menu_item():
    res = match_menu_item(_food("chicken burrito bowl", grams=450))
    assert res is not None
    assert res.kcal_mid == 650


def test_menu_beats_generic_in_resolve():
    res = resolve(_food("Egg McMuffin", grams=135, brand="McDonald's"))
    assert res is not None and res.source == SOURCE_DB_BRANDED
    assert res.kcal_mid == 310


# --- pizza unit scaling ------------------------------------------------------


def test_two_pizza_slices_scale():
    res = match_menu_item(_food("pepperoni pizza", grams=230))
    assert res is not None
    assert res.count == 2
    assert (res.kcal_low, res.kcal_mid, res.kcal_high) == (230, 280, 330)


def test_single_pizza_slice_not_scaled():
    res = match_menu_item(_food("pepperoni pizza slice", grams=110))
    assert res is not None and res.count == 1


# --- generic matching --------------------------------------------------------


def test_generic_chicken_breast():
    res = match_generic(_food("grilled chicken breast", grams=150))
    assert res is not None
    assert res.source == SOURCE_DB_GENERIC
    assert res.kcal_per_100g == 165


def test_generic_rice_variants():
    for name in ["white rice", "steamed white rice", "jasmine rice"]:
        res = match_generic(_food(name, grams=160))
        assert res is not None and res.kcal_per_100g == 130, name


def test_unknown_food_returns_none():
    assert resolve(_food("chocolate lava cake", grams=120)) is None


# --- combined foods must not match a single ingredient -----------------------


def test_beef_and_broccoli_is_not_broccoli():
    res = match_generic(_food("beef and broccoli", grams=300))
    assert res is not None
    assert res.matched_name == "beef stir-fry"
    assert res.kcal_per_100g == 120  # not broccoli's 35


def test_banana_bread_is_not_banana():
    res = match_generic(_food("banana bread", grams=60))
    assert res is not None
    assert res.matched_name == "banana bread"
    assert res.kcal_per_100g == 330  # not banana's 89


def test_apple_pie_is_not_apple():
    res = match_generic(_food("apple pie", grams=125))
    assert res is not None and res.kcal_per_100g == 240  # not apple's 52


def test_carrot_cake_is_not_carrot():
    res = match_generic(_food("carrot cake", grams=100))
    assert res is not None and res.kcal_per_100g == 400  # not carrot's 41


def test_tomato_soup_is_not_tomato():
    res = match_generic(_food("tomato soup", grams=300))
    assert res is not None and res.kcal_per_100g == 40  # not tomato's 18


def test_chicken_fried_rice_is_not_plain_rice():
    res = match_generic(_food("chicken fried rice", grams=250))
    assert res is not None
    assert res.matched_name == "fried rice"
    assert res.kcal_per_100g == 175  # not plain rice's 130


def test_plain_fried_rice_beats_plain_rice():
    res = match_generic(_food("fried rice", grams=200))
    assert res is not None and res.kcal_per_100g == 175


def test_strawberry_milkshake_is_not_strawberries():
    res = match_generic(_food("strawberry milkshake", grams=400))
    assert res is not None and res.kcal_per_100g == 120  # not strawberries' 33


def test_harmless_modifiers_still_match():
    res = match_generic(_food("a bowl of steamed white rice", grams=200))
    assert res is not None and res.kcal_per_100g == 130


def test_plural_folding_matches():
    res = match_generic(_food("two fried eggs", grams=100))
    assert res is not None and res.kcal_per_100g == 144


def test_diet_soda_not_matched_to_regular():
    res = match_generic(_food("diet coke", grams=400))
    assert res is not None
    assert res.kcal_per_100g < 1  # diet entry, not regular soda's 41


def test_grilled_chicken_thigh_not_forced_to_breast():
    res = match_generic(_food("grilled chicken thigh", grams=130))
    assert res is not None
    assert res.kcal_per_100g == 215  # thigh entry, not breast's 165


def test_unknown_combined_dish_stays_unmatched():
    assert match_generic(_food("chicken and rice casserole", grams=350)) is None


def test_big_mac_meal_not_matched_as_single_item():
    item = _food("Big Mac meal", grams=600, brand="McDonald's")
    assert resolve(item) is None  # a meal is burger+fries+drink, not 575 kcal


def test_double_cheeseburger_not_matched_to_single_patty():
    assert resolve(_food("double cheeseburger", grams=280)) is None


def test_taco_count_scaling():
    res = match_menu_item(_food("3 crunchy tacos", grams=235))
    assert res is not None
    assert res.count == 3
    assert res.kcal_mid == 170  # per-unit; sanity layer multiplies by count


# --- evaluate() with resolutions --------------------------------------------


def test_branded_resolution_overrides_math():
    item = _food("Big Mac", grams=250, kcal=100, brand="McDonald's")
    analysis = FoodAnalysis(items=[item])
    meal = evaluate(analysis, lookup.resolve_all(analysis.items))
    est = meal.items[0]
    assert est.source == SOURCE_DB_BRANDED
    assert (est.kcal_low, est.kcal_mid, est.kcal_high) == (560, 575, 590)
    assert any("matched menu item" in a for a in est.assumptions)


def test_scaled_pizza_totals():
    item = _food("pepperoni pizza", grams=230)
    analysis = FoodAnalysis(items=[item])
    meal = evaluate(analysis, lookup.resolve_all(analysis.items))
    est = meal.items[0]
    assert (est.kcal_low, est.kcal_mid, est.kcal_high) == (460, 560, 660)
    assert est.grams == 226  # 2 x 113 g slices
    assert any("x2" in a for a in est.assumptions)


def test_generic_resolution_replaces_density_keeps_grams():
    item = _food("grilled chicken breast", grams=150, kcal=250, confidence="high")
    analysis = FoodAnalysis(items=[item])
    meal = evaluate(analysis, lookup.resolve_all(analysis.items))
    est = meal.items[0]
    assert est.source == SOURCE_DB_GENERIC
    assert est.kcal_per_100g == 165
    assert est.kcal_mid == 248  # 150 g x 165/100, not the model's 250
    assert any("energy density from food database" in a for a in est.assumptions)


def test_unmatched_item_stays_model_estimate():
    item = _food("chocolate lava cake", grams=120, kcal=350)
    analysis = FoodAnalysis(items=[item])
    meal = evaluate(analysis, lookup.resolve_all(analysis.items))
    assert meal.items[0].source == SOURCE_MODEL
    assert meal.items[0].kcal_mid == 420


# --- end-to-end through the pipeline -----------------------------------------


class _FakeMessages:
    def __init__(self, analysis):
        self._analysis = analysis

    def parse(self, **kwargs):
        from caloriecam.debate import Critique

        if kwargs["output_format"] is Critique:
            parsed = Critique(challenges=[], overall_assessment="draft is sound")
        else:
            parsed = self._analysis
        return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn")


class FakeClient:
    def __init__(self, analysis):
        self.messages = _FakeMessages(analysis)


def test_pipeline_applies_database(photo_path):
    analysis = FoodAnalysis(
        items=[
            _food("Big Mac", grams=210, kcal=260, brand="McDonald's"),
            _food("medium french fries", grams=115, kcal=310, brand="McDonald's"),
        ],
        scale_reference="tray",
    )
    meal, _ = pipeline.run(photo_path, client=FakeClient(analysis))
    assert [i.source for i in meal.items] == [SOURCE_DB_BRANDED, SOURCE_DB_BRANDED]
    assert meal.total_mid == 575 + 340
    text = report.to_text(meal, "lunch.jpg")
    assert ", db]" in text
