from types import SimpleNamespace

from caloriecam import pipeline
from caloriecam.debate import Critique
from caloriecam.pipeline import _worth_debating
from caloriecam.schema import FoodAnalysis, FoodItem


def _item(confidence="high", name="apple"):
    return FoodItem(
        name=name,
        portion_description="one",
        estimated_grams=180,
        kcal_per_100g=52,
        confidence=confidence,
        assumptions=[],
        brand=None,
    )


def test_simple_all_high_confidence_skips():
    analysis = FoodAnalysis(items=[_item(), _item(name="green apple")])
    assert _worth_debating(analysis) is False


def test_any_non_high_confidence_debates():
    analysis = FoodAnalysis(items=[_item(), _item(confidence="medium")])
    assert _worth_debating(analysis) is True


def test_many_items_always_debate_even_if_high():
    analysis = FoodAnalysis(items=[_item(name=f"food{i}") for i in range(5)])
    assert _worth_debating(analysis) is True


def test_empty_analysis_skips():
    assert _worth_debating(FoodAnalysis(items=[])) is False


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
    analysis = FoodAnalysis(items=[_item(), _item(name="green apple")])
    client = FakeClient(analysis)
    meal, _ = pipeline.run(photo_path, client=client)
    assert len(client.messages.calls) == 1  # analyze only
    assert meal.debate is None


def test_pipeline_still_debates_uncertain_photo(photo_path):
    analysis = FoodAnalysis(items=[_item(confidence="low")])
    client = FakeClient(analysis)
    pipeline.run(photo_path, client=client)
    assert len(client.messages.calls) == 2  # analyze + critic
