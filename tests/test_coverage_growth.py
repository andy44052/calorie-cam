"""Phase 2 of the DB-coverage cycle: new entries, aliases, and retry
normalizations - every recovery pinned, every red-team trap asserted dead."""

import pytest

from caloriecam.lookup import _retry_variants, resolve
from caloriecam.schema import FoodAnalysis, FoodItem


def _probe(name: str, brand: str | None = None, grams: float = 150.0,
           kcal100: float = 200.0):
    return resolve(FoodItem(
        name=name, portion_description="probe", estimated_grams=grams,
        kcal_per_100g=kcal100, confidence="medium", assumptions=[], brand=brand,
    ))


# --- recoveries: the adjudicated top-45, now matched ------------------------

RECOVERIES = [
    # (verbose sweep name, entry that must claim it)
    ("lemon bars (shortbread crust with lemon curd filling)", "lemon bar"),
    ("lemon bars (shortbread crust with lemon custard, powdered sugar dusting)", "lemon bar"),
    ("black seedless grapes", "grapes"),
    ("pan-seared filet mignon steak", "steak, cooked"),
    ("creamy steak fettuccine with mushrooms, cherry tomatoes and spinach", "pasta alfredo"),
    ("creamy fettuccine with steak, mushrooms, cherry tomatoes and spinach (tuscan-style pasta)", "pasta alfredo"),
    ("steamed white rice with toasted sesame seeds", "white rice, cooked"),
    ("tuna salad sandwich on whole grain bread", "tuna salad sandwich"),
    ("blue cheese wedge", "blue cheese"),
    ("rustic seeded bread loaf, sliced", "artisan bread (baguette/sourdough/rye)"),
    ("white baguette / ciabatta pieces and sesame loaf section", "artisan bread (baguette/sourdough/rye)"),
    ("dark rye / seeded sourdough slices", "artisan bread (baguette/sourdough/rye)"),
    ("twisted puff-pastry breadsticks", "breadsticks / grissini (pastry twists)"),
    ("walnut halves", "walnuts"),
    ("chickpeas", "chickpeas, cooked"),
    ("cured meats (salami and prosciutto)", "cured meats / charcuterie"),
    ("cured meat slices (prosciutto and salami)", "cured meats / charcuterie"),
    ("roasted chicken (half, skin-on) with rosemary", "roast chicken (with skin)"),
    ("square butter crackers", "crackers (butter/snack)"),
    ("feta cubes with paprika/pepper coating", "feta cheese"),
]


@pytest.mark.parametrize("name,expected", RECOVERIES)
def test_top45_recovery(name, expected):
    res = _probe(name)
    assert res is not None, f"{name!r} still unmatched"
    assert res.matched_name == expected, f"{name!r} -> {res.matched_name!r}"


def test_head_retry_recovers_garnish_clauses():
    res = _probe("pan-seared filet mignon steak with pan jus")
    assert res is not None and res.matched_name == "steak, cooked"
    res = _probe("french fries with seasoning", kcal100=310.0)
    assert res is not None and res.matched_name.startswith("french fries")


def test_burger_entries_match_patty_descriptions():
    double = _probe(
        "double cheeseburger with special sauce, lettuce, tomato, pickle and onion on sesame bun",
        grams=220.0,
    )
    assert double is not None and double.matched_name == "Double cheeseburger"
    triple = _probe(
        "triple-stack cheeseburger with burger sauce, lettuce, tomato, onion and pickles",
        grams=290.0,
    )
    assert triple is not None and triple.matched_name == "Triple cheeseburger"


# --- red-team traps: must stay dead ------------------------------------------

def test_creamy_steak_pasta_is_not_steak():
    res = _probe("creamy steak fettuccine with mushrooms, cherry tomatoes and spinach")
    assert res is not None and res.matched_name == "pasta alfredo"  # NOT steak @270


def test_butter_crackers_are_not_butter():
    res = _probe("square butter crackers")
    assert res is not None and "cracker" in res.matched_name


def test_head_retry_refuses_to_drop_a_real_food():
    # "with rice" discards half the meal - vetoed because rice is a DB food.
    assert _probe("chicken with rice") is None
    assert "chicken" not in [v for v in _retry_variants("chicken with rice")]


def test_paren_strip_cannot_unveto_excludes():
    # Stripping "(banana loaf)" must not let plain bread claim banana bread.
    res = _probe("bread (banana loaf)")
    assert res is None or "banana" in res.matched_name


def test_compound_cheese_dishes_stay_unclaimed():
    assert _probe("watermelon feta salad") is None
    assert _probe("blue cheese dressing") is None
    # A blue cheese burger is a burger (generic burger entry may claim it) -
    # but never the blue cheese entry at 353 kcal/100g.
    res = _probe("blue cheese burger")
    assert res is None or res.matched_name != "blue cheese"


def test_charcuterie_does_not_claim_composites():
    assert _probe("salami sandwich") is None
    res = _probe("salami pizza")
    assert res is None or "charcuterie" not in res.matched_name


# --- caught by the 69-run replay, not by isolated probes ---------------------

def test_location_notes_cannot_feed_the_charcuterie_entry():
    """The vision model writes position notes into names. 'charcuterie plate'
    as a LOCATION must not book smoked salmon at cured-meat density (the one
    wrong match the replay found: 117 -> 370 kcal/100g)."""
    res = _probe("smoked salmon slices (loose, top-right charcuterie plate)")
    assert res is None or "charcuterie" not in res.matched_name


def test_prosciutto_books_prosciutto_not_mixed_board_density():
    res = _probe("prosciutto / cured ham slices")
    assert res is not None and res.matched_name == "prosciutto"
    assert res.kcal_per_100g == 250  # not the 370 salami-weighted figure


def test_crostini_are_a_composite_not_cured_meat():
    assert _probe("salami and prosciutto crostini") is None
    assert _probe("salami and cream cheese crostini") is None


def test_counted_slices_never_weigh_as_whole_items():
    """Run A shipped a 1,920 kcal salad avocado: 12 counted crescents x the
    whole-avocado band's 100 g minimum. A band may only reprice a count when
    it weighs the unit that was counted."""
    from caloriecam.sanity import evaluate

    sliced = FoodItem(
        name="sliced avocado", portion_description="fanned crescents",
        estimated_grams=150, kcal_per_100g=160, unit_count=12,
        per_unit_grams=12.0, confidence="medium", assumptions=[],
    )
    meal = evaluate(FoodAnalysis(items=[sliced], scale_reference="bowl"), [None])
    # 12 x 12 g = 144 g of avocado, not 12 x 100 g = 1200 g.
    assert meal.items[0].grams <= 200

    # Counter-case: a band whose own vocabulary is per-piece still applies.
    slices = FoodItem(
        name="bread slices", portion_description="stack",
        estimated_grams=320, kcal_per_100g=267, unit_count=4,
        per_unit_grams=80.0, confidence="medium", assumptions=[],
    )
    meal2 = evaluate(FoodAnalysis(items=[slices], scale_reference="plate"), [None])
    # The bread-slice band clamps 80 g/slice down into its 22-45 g range.
    assert meal2.items[0].grams <= 4 * 45


def test_kalamata_olives_book_kalamata_density():
    """Two independent vision passes put kalamata at ~220-240 kcal/100g; the
    general cured-olive 145 figure under-booked them by ~1.6x."""
    res = _probe("kalamata olives")
    assert res is not None and res.matched_name == "kalamata olives"
    assert res.kcal_per_100g == 230
    res = _probe("mixed marinated olives")
    assert res is not None and res.matched_name == "olives (cured)"


def test_roasted_sweet_potato_cubes_stay_model_estimate():
    """Oil-roasted cubes at the plain-90 density was a -31% booking; the
    model's own estimate is closer. Alias deliberately removed."""
    res = _probe("roasted sweet potato cubes", kcal100=130.0)
    assert res is None


def test_lemon_garnish_is_not_dessert():
    res = _probe("lemon slices")
    assert res is None or res.matched_name != "lemon bar"


def test_mixed_roast_chicken_with_breast_word_stays_model_estimate():
    # Mechanism lens: breast-only landing for a mixed skin-on bird is WRONG;
    # the roast entry excludes "breast" so this stays a model estimate.
    assert _probe("roast chicken (leg quarter and breast piece) with rosemary") is None


def test_fettuccine_marinara_is_not_alfredo():
    res = _probe("fettuccine with marinara")
    assert res is None or res.matched_name != "pasta alfredo"


def test_caprese_stays_unmatched():
    assert _probe("caprese salad (tomato slices with mozzarella pearls, olive oil and oregano)") is None


def test_roasted_chickpea_snack_is_not_boiled_chickpeas():
    assert _probe("crunchy roasted chickpea snack") is None


# --- retry plumbing -----------------------------------------------------------

def test_retry_variants_shapes():
    assert _retry_variants("lemon bars (shortbread crust)") == ["lemon bars"]
    # tail of 1 harmless token -> head allowed
    assert "french fries" in _retry_variants("french fries with seasoning")
    # tail contains a DB food -> head refused
    assert _retry_variants("chicken with rice") == []
    # long ingredient tails are refused
    assert _retry_variants("stew with beef, potatoes, carrots and onions") == []
    assert _retry_variants("plain name") == []
