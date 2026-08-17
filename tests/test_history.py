"""Meal history: recording, the blending prior, corrections, daily totals."""

import pytest

from caloriecam import history, lookup, pipeline
from caloriecam.history import BLEND_K, HistoryStore, blend_grams, default_store
from caloriecam.sanity import evaluate
from caloriecam.schema import FoodAnalysis, FoodItem
from caloriecam.text import name_key


def _analysis(name="pasta with tomato sauce", grams=300.0, kcal100=150.0,
              confidence="medium", unit_count=None, per_unit=None, brand=None):
    return FoodAnalysis(
        items=[FoodItem(
            name=name, portion_description="a plate", estimated_grams=grams,
            kcal_per_100g=kcal100, confidence=confidence, assumptions=[],
            unit_count=unit_count, per_unit_grams=per_unit, brand=brand,
        )],
        scale_reference="plate", overall_notes=None,
    )


def _meal(analysis):
    return evaluate(analysis, lookup.resolve_all(analysis.items))


@pytest.fixture
def store(tmp_path):
    return HistoryStore(tmp_path / "test.db")


# --- name_key ---------------------------------------------------------------


def test_name_key_drops_modifiers_and_word_order():
    assert name_key("grilled chicken breast") == name_key("chicken breast, sliced")
    assert name_key("Chicken Breast") == name_key("breast chicken")


def test_name_key_folds_plurals():
    assert name_key("scrambled eggs") == name_key("scrambled egg")


def test_name_key_keeps_calorie_relevant_words_apart():
    assert name_key("fried rice") != name_key("rice")
    assert name_key("banana bread") != name_key("banana")


def test_name_key_survives_all_modifier_names():
    assert name_key("large cooked serving") != ""


def test_name_key_drops_digit_counts():
    # "2 eggs" is the same food as "eggs" - digits are counts, not identity.
    assert name_key("2 eggs") == name_key("eggs") == name_key("two eggs")
    assert name_key("3 crunchy tacos") == name_key("crunchy taco")
    # "2%" is identity, not a count ("2%".isdigit() is False).
    assert name_key("2% milk") != name_key("milk")


# --- blend math -------------------------------------------------------------


def test_blend_needs_two_past_meals():
    assert blend_grams(400.0, []) == 400.0
    assert blend_grams(400.0, [300.0]) == 400.0


def test_blend_weights_follow_shrinkage_formula():
    # 2 past -> 50% history; new 400, median 300 -> 350
    assert blend_grams(400.0, [300.0, 300.0]) == pytest.approx(350.0)
    # 10 past -> 10/12 = 83.3% history
    assert blend_grams(400.0, [300.0] * 10) == pytest.approx(
        (10 / (10 + BLEND_K)) * 300 + (2 / 12) * 400
    )


def test_blend_uses_median_not_mean():
    # One wild outlier in history must not drag the prior.
    past = [300.0, 310.0, 305.0, 9999.0]
    w = 4 / (4 + BLEND_K)
    assert blend_grams(400.0, past) == pytest.approx(w * 307.5 + (1 - w) * 400.0)


# --- store round-trip -------------------------------------------------------


def test_record_and_read_back(store, sample_analysis):
    meal = _meal(sample_analysis)
    meal_id = store.record(meal, sample_analysis, model="test-model", hint="no oil")
    assert meal_id == 1
    past = store.past_grams("white rice")
    assert past == [200.0]
    assert store.today_total() == {"meals": 1, "kcal": meal.total_mid}


def test_past_grams_most_recent_first_and_windowed(store):
    for grams in range(100, 1300, 100):  # 12 meals: 100..1200 g
        a = _analysis(grams=float(grams))
        store.record(_meal(a), a, model="m")
    past = store.past_grams("pasta with tomato sauce")
    assert len(past) == history.BLEND_WINDOW  # capped at 10
    assert past[0] == 1200.0  # most recent first
    assert 100.0 not in past and 200.0 not in past  # oldest two aged out


def test_past_grams_keys_on_normalized_name(store):
    a = _analysis(name="Pasta With Tomato Sauce")
    store.record(_meal(a), a, model="m")
    assert store.past_grams("tomato sauce pasta") == [300.0]
    assert store.past_grams("pasta") == []  # different food, different key


def test_correction_scales_past_grams_at_read_time(store):
    a = _analysis(grams=300.0)
    meal = _meal(a)
    meal_id = store.record(meal, a, model="m")
    assert store.correct(meal_id, round(meal.total_mid * 0.5)) is True
    # The stored row is untouched; the scaling happens on read.
    assert store.past_grams("pasta with tomato sauce")[0] == pytest.approx(150.0, abs=1)


def test_correct_unknown_meal_returns_false(store):
    assert store.correct(999, 400) is False


def test_today_total_prefers_corrections(store, sample_analysis):
    meal = _meal(sample_analysis)
    meal_id = store.record(meal, sample_analysis, model="m")
    store.correct(meal_id, 999)
    assert store.today_total() == {"meals": 1, "kcal": 999}


def test_daily_totals_and_lifetime_cost(store, sample_analysis):
    meal = _meal(sample_analysis)
    meal.usage = {"total_cost_usd": 0.21}
    store.record(meal, sample_analysis, model="m")
    store.record(meal, sample_analysis, model="m")
    days = store.daily_totals()
    assert len(days) == 1
    assert days[0]["meals"] == 2
    assert store.lifetime_cost_usd() == pytest.approx(0.42)


def test_default_store_off_switch(monkeypatch):
    monkeypatch.setenv("CALORIECAM_HISTORY", "off")
    assert default_store() is None


def test_default_store_env_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CALORIECAM_HISTORY", str(tmp_path / "alt.db"))
    s = default_store()
    assert s is not None and s.path == tmp_path / "alt.db"


def test_default_store_on_values_mean_default_not_a_file_named_on(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_DEFAULT_PATH", tmp_path / "default.db")
    for value in ("on", "1", "true", "YES"):
        monkeypatch.setenv("CALORIECAM_HISTORY", value)
        s = default_store()
        assert s is not None and s.path == tmp_path / "default.db", value


# --- blending inside the pipeline -------------------------------------------


def _blend(analysis, store):
    meal = _meal(analysis)
    pipeline._blend_with_history(meal, analysis, store)
    return meal


def _seed(store, n, grams=300.0, **kwargs):
    for _ in range(n):
        a = _analysis(grams=grams, **kwargs)
        store.record(_meal(a), a, model="m")


def test_blending_moves_repeat_food_toward_median(store):
    _seed(store, 3, grams=300.0)
    meal = _blend(_analysis(grams=500.0), store)
    item = meal.items[0]
    # 3 past -> 60% history: 0.6*300 + 0.4*500 = 380
    assert item.grams == 380
    assert item.grams_raw == 500
    assert any("meal history" in a for a in item.assumptions)
    # kcal scaled by the same factor: grams and density stay consistent
    # (density comes from the generic DB here, not the model's guess)
    assert item.kcal_mid == pytest.approx(item.grams * item.kcal_per_100g / 100, abs=2)
    assert meal.total_mid == item.kcal_mid


def test_blending_skips_first_and_second_sighting(store):
    _seed(store, 1)
    meal = _blend(_analysis(grams=500.0), store)
    assert meal.items[0].grams_raw is None
    assert meal.items[0].grams == 500


def test_blending_skips_counted_items(store):
    _seed(store, 3, grams=300.0)
    counted = _analysis(grams=500.0, unit_count=5, per_unit=100.0)
    meal = _meal(counted)
    before = meal.items[0].grams
    pipeline._blend_with_history(meal, counted, store)
    assert meal.items[0].grams == before
    assert meal.items[0].grams_raw is None


def test_blending_skips_branded_items(store):
    # Seed BIG MAC history (not some other food) so the branded guard is the
    # only thing standing between this item and a blend - deleting the guard
    # must fail this test.
    _seed(store, 3, name="Big Mac", brand="McDonald's", grams=220.0)
    a = _analysis(name="Big Mac", grams=250.0, brand="McDonald's")
    meal = _meal(a)
    assert meal.items[0].source == "database_branded"  # precondition
    assert len(store.past_grams("Big Mac")) >= 2  # precondition: blendable history
    before = meal.items[0].grams
    pipeline._blend_with_history(meal, a, store)
    assert meal.items[0].grams == before
    assert meal.items[0].grams_raw is None


def test_recorded_grams_are_preblend(store):
    """The prior must never train on its own output."""
    _seed(store, 4, grams=300.0)
    a = _analysis(grams=600.0)
    meal = _blend(a, store)
    assert meal.items[0].grams != 600  # blending fired
    store.record(meal, a, model="m")
    # The newest stored row is the RAW 600, not the blended value.
    assert store.past_grams("pasta with tomato sauce")[0] == 600.0


def test_correction_on_blended_meal_stays_in_raw_frame(store):
    """The review's headline bug: correcting a BLENDED meal must not leak the
    blend factor back into the training signal.

    Seed 3 x 300 g, then a 600 g estimate blends to 420 g shown. The user
    corrects that meal to its true raw total ("it really was 600"). The
    stored raw 600 g row must read back as 600 g - dividing by the shown
    (post-blend) total would inflate it to ~857 g.
    """
    _seed(store, 3, grams=300.0)
    a = _analysis(grams=600.0)
    meal = _blend(a, store)
    assert meal.items[0].grams == 420  # 3/(3+2) history weight
    meal_id = store.record(meal, a, model="m")

    raw_kcal_total = round(600 * meal.items[0].kcal_per_100g / 100)
    store.correct(meal_id, raw_kcal_total)
    assert store.past_grams("pasta with tomato sauce")[0] == pytest.approx(600.0)


def test_absurd_correction_ratio_is_clamped(store):
    a = _analysis(grams=300.0)
    meal = _meal(a)
    meal_id = store.record(meal, a, model="m")
    store.correct(meal_id, meal.total_mid * 100)  # fat-fingered "40000"
    # Ratio capped at 5x, not 100x.
    assert store.past_grams("pasta with tomato sauce")[0] == pytest.approx(1500.0)


def test_blending_skips_density_mismatch(store):
    """A name-key collision ("grilled cheese" keying onto "cheese") must not
    blend across foods - energy density is the tell."""
    _seed(store, 3, name="grilled zorbington", kcal100=60.0)  # keys to "zorbington"
    imposter = _analysis(name="zorbington", grams=200.0, kcal100=500.0)
    meal = _meal(imposter)
    assert len(store.past_grams("zorbington")) == 3  # keys collide (by design here)
    pipeline._blend_with_history(meal, imposter, store)
    assert meal.items[0].grams == 200  # density 500 vs 60 -> not the same food
    assert meal.items[0].grams_raw is None

    # Control: agreeing density on the same key does blend.
    same_food = _analysis(name="zorbington", grams=200.0, kcal100=65.0)
    meal2 = _meal(same_food)
    pipeline._blend_with_history(meal2, same_food, store)
    assert meal2.items[0].grams_raw is not None


def test_pipeline_without_history_never_blends_or_records(store, photo_path):
    """The benchmark path: pipeline.run with history omitted must measure the
    model, untouched by past meals."""
    from types import SimpleNamespace

    from caloriecam.debate import Critique

    _seed(store, 3, grams=300.0)

    class _Msgs:
        def __init__(self, analysis):
            self._analysis = analysis

        def parse(self, **kwargs):
            parsed = (
                Critique(challenges=[], overall_assessment="ok")
                if kwargs["output_format"] is Critique
                else self._analysis
            )
            return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn", usage=None)

    client = SimpleNamespace(messages=_Msgs(_analysis(grams=500.0)))
    meal, _ = pipeline.run(photo_path, client=client)  # no history= kwarg
    assert meal.items[0].grams == 500
    assert meal.items[0].grams_raw is None
    assert len(store.past_grams("pasta with tomato sauce")) == 3  # nothing recorded


def test_blending_stacks_with_corrections(store):
    a1 = _analysis(grams=400.0)
    m1 = _meal(a1)
    mid = store.record(m1, a1, model="m")
    store.correct(mid, round(m1.total_mid * 0.75))
    a2 = _analysis(grams=400.0)
    store.record(_meal(a2), a2, model="m")
    # history = [400 (uncorrected), 300 (corrected read)] -> median 350
    meal = _blend(_analysis(grams=500.0), store)
    assert meal.items[0].grams == round(0.5 * 350 + 0.5 * 500)
