"""The adaptive debate gate: pay for the skeptic only when the draft is shaky."""

from types import SimpleNamespace

from caloriecam import pipeline
from caloriecam.debate import Critique
from caloriecam.pipeline import needs_debate
from caloriecam.schema import FoodAnalysis, FoodItem


def _item(name="apple", confidence="high", grams=180.0, kcal100=52.0):
    return FoodItem(
        name=name,
        portion_description="one",
        estimated_grams=grams,
        kcal_per_100g=kcal100,
        confidence=confidence,
        assumptions=[],
        brand=None,
    )


def test_empty_analysis_skips():
    assert needs_debate(FoodAnalysis(items=[])) is False


def test_db_anchored_high_confidence_skips():
    # Apple + banana: both database_generic, high confidence -> narrow band.
    analysis = FoodAnalysis(items=[_item(), _item(name="banana", grams=118, kcal100=89)])
    assert needs_debate(analysis) is False


def test_unmatched_variant_counts_as_unanchored():
    # A food the database genuinely does not know has no anchor, so the
    # skeptic pass is warranted.
    analysis = FoodAnalysis(items=[_item(), _item(name="dragonfruit sorbet")])
    assert needs_debate(analysis) is True


def test_colour_variants_anchor_to_the_plain_food():
    # A green apple has the same energy density as a red one, so "green apple"
    # must anchor rather than counting as unmatched. This fixture used to be
    # the unanchored case above; colour words became modifiers when Run E
    # showed "red wine grapes (on the vine)" booking 80 kcal/100g instead of
    # the database's 69.
    analysis = FoodAnalysis(items=[_item(), _item(name="green apple")])
    assert needs_debate(analysis) is False


def test_any_model_estimate_item_debates():
    # "mystery casserole" matches nothing -> no database anchor.
    analysis = FoodAnalysis(items=[_item(), _item(name="mystery casserole xyz")])
    assert needs_debate(analysis) is True


def test_six_items_debate_even_when_all_anchored():
    names = ["apple", "banana", "orange", "white rice", "broccoli", "carrot"]
    analysis = FoodAnalysis(items=[_item(name=n) for n in names])
    assert needs_debate(analysis) is True


def test_five_anchored_items_do_not_trip_the_count_trigger():
    names = ["apple", "banana", "orange", "white rice", "broccoli"]
    analysis = FoodAnalysis(items=[_item(name=n) for n in names])
    # May still debate via width, but not via the count rule alone:
    # all high-confidence db-generic items -> width 0.30 <= 0.35 -> skips.
    assert needs_debate(analysis) is False


def test_wide_uncertainty_band_debates():
    # Low confidence -> 0.50 margin -> width 1.0 > 0.35, even though the
    # apple itself is database-anchored.
    analysis = FoodAnalysis(items=[_item(confidence="low")])
    assert needs_debate(analysis) is True


class _Msgs:
    def __init__(self, analysis):
        self._analysis = analysis
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        parsed = (
            Critique(challenges=[], overall_assessment="ok")
            if kwargs["output_format"] is Critique
            else self._analysis
        )
        return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn", usage=None)


class FakeClient:
    def __init__(self, analysis):
        self.messages = _Msgs(analysis)


def test_pipeline_skips_critic_call_on_easy_photo(photo_path):
    analysis = FoodAnalysis(items=[_item(), _item(name="banana", grams=118, kcal100=89)])
    client = FakeClient(analysis)
    meal, _ = pipeline.run(photo_path, client=client)
    assert len(client.messages.calls) == 1  # analyze only
    assert meal.debate is None


def test_pipeline_still_debates_uncertain_photo(photo_path):
    analysis = FoodAnalysis(items=[_item(name="mystery casserole xyz", confidence="low")])
    client = FakeClient(analysis)
    pipeline.run(photo_path, client=client)
    assert len(client.messages.calls) == 2  # analyze + critic


def test_skeptic_model_routes_critic_to_cheaper_model(photo_path):
    analysis = FoodAnalysis(items=[_item(name="mystery casserole xyz", confidence="low")])
    client = FakeClient(analysis)
    pipeline.run(photo_path, client=client, skeptic_model="claude-haiku-4-5")
    models = [c["model"] for c in client.messages.calls]
    assert models[0] != "claude-haiku-4-5"  # primary analyst unchanged
    assert models[1] == "claude-haiku-4-5"  # critic on the cheap model


def test_no_skeptic_model_keeps_debate_on_primary(photo_path):
    analysis = FoodAnalysis(items=[_item(name="mystery casserole xyz", confidence="low")])
    client = FakeClient(analysis)
    pipeline.run(photo_path, client=client, model="primary-model")
    assert [c["model"] for c in client.messages.calls] == ["primary-model"] * 2
