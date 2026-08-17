from types import SimpleNamespace

from caloriecam import lookup, pipeline, report
from caloriecam.lookup import _generic_foods, _menu_items, match_generic, match_menu_item, resolve
from caloriecam.sanity import SOURCE_DB_BRANDED, SOURCE_DB_GENERIC, SOURCE_MODEL, evaluate
from caloriecam.schema import FoodAnalysis, FoodItem


def _food(name, grams=100.0, kcal=200.0, confidence="high", brand=None, unit_count=None) -> FoodItem:
    return FoodItem(
        name=name,
        portion_description="a portion",
        estimated_grams=grams,
        kcal_per_100g=kcal,
        confidence=confidence,
        assumptions=[],
        brand=brand,
        unit_count=unit_count,
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
    # Originally asserted None (the single-patty entry must never claim a
    # double). Since the DB-coverage cycle there IS a Double entry - the
    # invariant is now that the DOUBLE entry claims it, never the single.
    res = resolve(_food("double cheeseburger", grams=280))
    assert res is not None
    assert res.matched_name == "Double cheeseburger"
    assert res.kcal_mid == 600  # not the 275 single-patty figure


def test_taco_count_scaling():
    res = match_menu_item(_food("3 crunchy tacos", grams=235))
    assert res is not None
    assert res.count == 3
    assert res.kcal_mid == 170  # per-unit; sanity layer multiplies by count


def test_stated_unit_count_used_when_grams_agree():
    # 3 slices x 113 g = 339 g, close enough to the stated 320 g
    res = match_menu_item(_food("pepperoni pizza", grams=320, unit_count=3))
    assert res is not None and res.count == 3


def test_count_grams_contradiction_falls_back_to_grams():
    # img17 regression: model says 3 visible slices but grams = the whole pan.
    # 3 x 107 = 321 g vs stated 630 g -> the grams win (6 slices).
    res = match_menu_item(_food("cheese pizza", grams=630, unit_count=3))
    assert res is not None and res.count == 6


def test_single_unit_with_matching_grams_stays_one():
    res = match_menu_item(_food("pepperoni pizza", grams=120, unit_count=1))
    assert res is not None and res.count == 1


def test_oversized_single_unit_scales_by_mass():
    # "1 slice" weighing 230 g is two database slices worth of pizza; calories
    # follow mass, so the gram estimate wins over the stated count.
    res = match_menu_item(_food("pepperoni pizza", grams=230, unit_count=1))
    assert res is not None and res.count == 2


def test_absurd_unit_count_falls_back_to_grams():
    res = match_menu_item(_food("pepperoni pizza", grams=230, unit_count=50))
    assert res is not None and res.count == 2  # grams-ratio fallback


# --- regressions from the 23-image benchmark (2026-08-13) --------------------


def test_chili_seasoned_fries_do_not_match_chili():
    # img09 run1: "chili" alias grabbed seasoned fries at 120 kcal/100g (real ~340)
    res = resolve(_food("french fries with chili/paprika seasoning", grams=280))
    assert res is None or res.kcal_per_100g > 200


def test_chili_peppers_do_not_match_chili_con_carne():
    # img02: garnish chili peppers matched the 120 kcal/100g stew
    assert resolve(_food("small orange chili peppers", grams=30)) is None


def test_actual_chili_still_matches():
    for name in ["chili", "beef chili", "bowl of chili", "chili con carne"]:
        res = match_generic(_food(name, grams=300))
        assert res is not None and res.kcal_per_100g == 120, name


def test_seasoned_fries_match_fries():
    res = match_generic(_food("seasoned french fries", grams=250))
    assert res is not None and res.kcal_per_100g == 307


def test_innout_cheeseburger_uses_real_calories():
    # img08: generic 275-kcal hamburger entry claimed an In-N-Out burger
    res = match_menu_item(_food("Cheeseburger", grams=268, brand="In-N-Out Burger"))
    assert res is not None
    assert res.kcal_mid == 480
    assert "In-N-Out" in res.matched_name


def test_innout_brand_in_name_only():
    res = match_menu_item(_food("In-N-Out Cheeseburger", grams=268))
    assert res is not None and res.kcal_mid == 480


def test_two_innout_cheeseburgers_scale():
    res = match_menu_item(_food("In-N-Out Cheeseburgers", grams=540))
    assert res is not None
    assert res.count == 2
    assert res.kcal_mid == 480  # per unit; sanity layer doubles it


def test_double_double():
    res = match_menu_item(_food("Double-Double", grams=330, brand="In-N-Out"))
    assert res is not None and res.kcal_mid == 670


def test_innout_fries_scale_by_order():
    one = match_menu_item(_food("french fries", grams=125, brand="In-N-Out"))
    two = match_menu_item(_food("french fries", grams=250, brand="In-N-Out"))
    assert one is not None and one.kcal_mid == 395 and one.count == 1
    assert two is not None and two.count == 2


def test_unbranded_cheeseburger_still_generic():
    res = match_menu_item(_food("cheeseburger", grams=150))
    assert res is not None and res.kcal_mid == 275


def test_two_generic_hamburgers_scale():
    res = match_menu_item(_food("hamburgers", grams=340))
    assert res is not None and res.count == 2


def test_fresh_cheese_not_cheddar():
    # img20: "fresh cheese slices" matched cheddar (403) instead of soft cheese
    res = match_generic(_food("fresh cheese slices", grams=40))
    assert res is not None and res.kcal_per_100g == 300


def test_brie_and_olives_covered():
    brie = match_generic(_food("soft white-rind cheese (brie style)", grams=250))
    olives = match_generic(_food("mixed marinated olives", grams=60))
    assert brie is not None and brie.kcal_per_100g == 300
    assert olives is not None and olives.kcal_per_100g == 145


def test_olive_oil_not_olives():
    res = match_generic(_food("olive oil", grams=30))
    assert res is not None and res.kcal_per_100g == 884


# --- regressions from the adversarial red-team audit (2026-08-13) ------------


def test_soups_do_not_match_stirfries():
    # a broth soup at ~45 kcal/100g must not get stir-fry density
    for name in ["chicken vegetable soup", "beef vegetable soup"]:
        res = match_generic(_food(name, grams=400))
        assert res is None or res.kcal_per_100g < 80, name


def test_lowcarb_substitutes_do_not_match_real_dishes():
    # cauliflower/zucchini/squash versions have ~half the calories
    for name in [
        "cauliflower fried rice",
        "spaghetti squash with meat sauce",
        "zucchini lasagna",
        "eggplant lasagna",
        "cauliflower mac and cheese",
    ]:
        assert match_generic(_food(name, grams=300)) is None, name


def test_fried_variants_do_not_match_lean_entries():
    assert match_generic(_food("fried chicken with vegetables", grams=300)) is None
    assert match_generic(_food("fried mac and cheese bites", grams=150)) is None
    assert match_generic(_food("deep fried sushi roll", grams=200)) is None


def test_casseroles_do_not_match_stirfries():
    assert match_generic(_food("chicken and broccoli casserole", grams=350)) is None


def test_ramen_variants():
    # broth ramen matches; dry brick and brothless stir-fry must not
    assert match_generic(_food("ramen", grams=400)).kcal_per_100g == 90
    assert match_generic(_food("dry instant noodles", grams=85)) is None
    assert match_generic(_food("ramen noodle stir fry", grams=300)) is None


def test_curry_variants():
    # chicken curry matches; different curries and non-curries must not
    assert match_generic(_food("chicken curry", grams=300)).kcal_per_100g == 140
    for name in ["vegetable curry", "curry chicken salad", "curry powder", "lentil curry"]:
        assert match_generic(_food(name, grams=300)) is None, name


def test_alfredo_sauce_alone_not_full_dish():
    assert match_generic(_food("alfredo sauce", grams=80)) is None


def test_cucumber_roll_not_full_sushi_density():
    assert match_generic(_food("cucumber sushi roll", grams=180)) is None


def test_combined_aliases_match_plain_pasta():
    # "penne pasta" / "spaghetti noodles" used to fall through entirely
    for name in ["penne pasta", "spaghetti noodles"]:
        res = match_generic(_food(name, grams=140))
        assert res is not None and res.kcal_per_100g == 150, name


def test_exclude_tokens_are_stem_folded():
    # "fries" as an exclude token must block "chili fries" despite stemming
    assert resolve(_food("chili fries", grams=250)) is None or \
        resolve(_food("chili fries", grams=250)).kcal_per_100g != 120


# --- regressions from red-team round 2 (ratio-path removal + audit) ----------


def test_string_similarity_no_longer_matches():
    # These matched purely because the LETTERS looked alike (SequenceMatcher).
    for name, banned_kcal in [
        ("chicken broth", 650),        # -> chicken burrito (menu, fixed kcal!)
        ("beef stew", 270),            # -> beef steak
        ("chicken katsu", 35),         # -> chicken noodle soup
        ("potato soup", 40),           # -> tomato soup
        ("chocolate milk", 370),       # -> chocolate cake
        ("smashed potatoes", 110),     # -> mashed potatoes
        ("chicken fried steak", 175),  # -> fried rice
        ("french roast", 230),         # -> french toast
        ("greek salad", 90),           # -> garden salad
        ("chicken and rice", 550),     # -> fried chicken sandwich (menu)
    ]:
        res = resolve(_food(name, grams=300))
        got = None if res is None else (res.kcal_per_100g or res.kcal_mid)
        assert got != banned_kcal, f"{name} still hits the old wrong match"


def test_new_entries_catch_former_ratio_victims():
    assert match_generic(_food("chicken broth", grams=240)).kcal_per_100g == 15
    assert match_generic(_food("beef stew", grams=300)).kcal_per_100g == 110
    assert match_generic(_food("chicken katsu", grams=150)).kcal_per_100g == 270
    assert match_generic(_food("fried shrimp", grams=120)).kcal_per_100g == 270
    assert match_generic(_food("chocolate milk", grams=244)).kcal_per_100g == 83
    assert match_generic(_food("sweet potato", grams=150)).kcal_per_100g == 90
    assert match_generic(_food("smoked salmon", grams=60)).kcal_per_100g == 130
    assert match_generic(_food("chicken wings", grams=200)).kcal_per_100g == 290
    assert match_generic(_food("potato chips", grams=40)).kcal_per_100g == 530
    assert match_generic(_food("roasted potatoes", grams=170)).kcal_per_100g == 140
    assert match_generic(_food("muesli", grams=55)).kcal_per_100g == 350
    assert match_generic(_food("yogurt cup", grams=150)).kcal_per_100g == 95
    assert match_generic(_food("spaghetti with marinara sauce", grams=250)).kcal_per_100g == 100


def test_loose_entries_reject_meaning_changers():
    for name in [
        "egg white omelette",
        "raw oats",
        "protein shake",
        "acai smoothie bowl",
        "pumpkin spice latte",
        "boston cream donut",
        "waffle cone",
        "potato waffles",
        "lasagna soup",
        "sushi bake",
        "cup noodle soup",
        "coffee cake",
        "grilled chicken strips",
        "olive tapenade",
        "greek salad with olives",
        "brownie with ice cream",
        "curry fries",
        "deviled eggs",
    ]:
        assert match_generic(_food(name, grams=200)) is None, name


def test_menu_entries_reject_meaning_changers():
    for name in [
        "bacon cheeseburger",
        "cheeseburger and fries",
        "bean burrito",
        "breakfast burrito",
        "california burrito",
        "sushi burrito",
        "taco soup",
        "english muffin breakfast sandwich",
        "grilled chicken sandwich",
    ]:
        assert match_menu_item(_food(name, grams=250)) is None, name


def test_hamburger_patty_falls_to_ground_beef():
    res = resolve(_food("hamburger patty", grams=80))
    assert res is not None and res.kcal_per_100g == 245


def test_oatmeal_cookie_is_a_cookie():
    res = match_generic(_food("oatmeal cookie", grams=30))
    assert res is not None and res.kcal_per_100g == 480


def test_iconic_names_match_without_brand():
    big_mac = match_menu_item(_food("big mac", grams=216))
    whopper = match_menu_item(_food("whopper", grams=290))
    assert big_mac is not None and big_mac.kcal_mid == 575
    assert whopper is not None and whopper.kcal_mid == 690


def test_iconic_does_not_override_conflicting_brand():
    assert resolve(_food("big mac", grams=216, brand="Burger King")) is None


def test_innout_absorbs_harmless_extras():
    res = match_menu_item(_food("in n out cheeseburger with onions", grams=268))
    assert res is not None and res.kcal_mid == 480


def test_five_guys_entries():
    res = match_menu_item(_food("hamburger", grams=303, brand="Five Guys"))
    assert res is not None and res.kcal_mid == 700


def test_oes_plural_stemming():
    res = match_generic(_food("boiled potatoes", grams=170))
    assert res is not None and res.kcal_per_100g == 93


def test_crispy_bacon_and_pbj_aliases():
    assert match_generic(_food("crispy bacon", grams=25)).kcal_per_100g == 540
    assert match_generic(_food("pb&j sandwich", grams=100)).kcal_per_100g == 350


def test_stir_fried_phrasing_still_matches():
    res = match_generic(_food("stir fried chicken with vegetables", grams=300))
    assert res is not None and res.kcal_per_100g == 110


def test_unbranded_fries_with_wrong_brand_grams_fall_to_generic():
    # "french fries" @152g labeled McDonald's used to hit the SMALL fries entry
    res = resolve(_food("french fries", grams=152, brand="McDonald's"))
    assert res is not None
    assert res.kcal_per_100g == 307  # generic per-gram, not a fixed size


# --- evaluate() with resolutions --------------------------------------------


def test_branded_resolution_overrides_math():
    item = _food("Big Mac", grams=250, kcal=100, brand="McDonald's")
    analysis = FoodAnalysis(items=[item])
    meal = evaluate(analysis, lookup.resolve_all(analysis.items))
    est = meal.items[0]
    assert est.source == SOURCE_DB_BRANDED
    assert est.kcal_mid == 575  # published value, unchanged
    # Band widened past the published 560-590: our READING of the photo can be
    # wrong even when the menu number is exact.
    assert (est.kcal_low, est.kcal_high) == (529, 621)
    assert any("matched menu item" in a for a in est.assumptions)


def test_scaled_pizza_totals():
    item = _food("pepperoni pizza", grams=230)
    analysis = FoodAnalysis(items=[item])
    meal = evaluate(analysis, lookup.resolve_all(analysis.items))
    est = meal.items[0]
    assert est.kcal_mid == 560  # 2 x 280
    # Wider than 2x the published per-slice range because the count of 2 was
    # inferred from grams, not read off a menu.
    assert (est.kcal_low, est.kcal_high) == (448, 672)
    assert est.grams == 226  # 2 x 113 g slices
    assert any("x2" in a for a in est.assumptions)
    assert any("miscount" in a for a in est.assumptions)


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
