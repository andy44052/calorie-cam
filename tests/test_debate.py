from types import SimpleNamespace

from caloriecam import pipeline, report
from caloriecam.debate import (
    Challenge,
    ChallengeRuling,
    Critique,
    DebatedAnalysis,
    run_debate,
)
from caloriecam.schema import FoodAnalysis, FoodItem


def _item(name, grams, kcal, confidence="medium"):
    return FoodItem(
        name=name,
        portion_description="a portion",
        estimated_grams=grams,
        kcal_per_100g=kcal,
        confidence=confidence,
        assumptions=[],
        brand=None,
    )


class QueueClient:
    """Returns queued parsed outputs in order, recording every call."""

    def __init__(self, *parsed_outputs):
        self.calls = []
        self._queue = list(parsed_outputs)
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=self._queue.pop(0), stop_reason="end_turn")


def _draft():
    return FoodAnalysis(
        items=[_item("chicken caesar wrap", grams=180, kcal=170)],
        scale_reference="hand",
        overall_notes=None,
    )


def _critique_with_challenge():
    return Critique(
        challenges=[
            Challenge(
                target="chicken caesar wrap",
                kind="portion_too_low",
                argument="The wrap spans the whole hand and is densely rolled; 180 g is a small-wrap weight.",
            )
        ],
        overall_assessment="One likely undercount.",
    )


def _debated_final():
    return DebatedAnalysis(
        rulings=[
            ChallengeRuling(
                challenge="portion_too_low: wrap heavier than 180 g",
                verdict="accepted",
                reason="Wrap diameter vs the hand supports ~260 g.",
            )
        ],
        final=FoodAnalysis(
            items=[_item("chicken caesar wrap", grams=260, kcal=170)],
            scale_reference="hand",
            overall_notes=None,
        ),
    )


# --- run_debate unit behavior -------------------------------------------------


def test_debate_revises_when_challenged():
    client = QueueClient(_critique_with_challenge(), _debated_final())
    final, record = run_debate("QUJD", "image/jpeg", _draft(), client=client)
    assert final.items[0].estimated_grams == 260
    assert len(record["challenges"]) == 1
    assert record["rulings"][0]["verdict"] == "accepted"
    assert len(client.calls) == 2  # critic + reviser


def test_debate_skips_reviser_when_no_challenges():
    client = QueueClient(Critique(challenges=[], overall_assessment="sound"))
    draft = _draft()
    final, record = run_debate("QUJD", "image/jpeg", draft, client=client)
    assert final is draft
    assert record["challenges"] == [] and record["rulings"] == []
    assert len(client.calls) == 1  # critic only


def test_debate_skips_entirely_for_empty_draft():
    client = QueueClient()  # any call would IndexError
    empty = FoodAnalysis(items=[], overall_notes="no food")
    final, record = run_debate("QUJD", "image/jpeg", empty, client=client)
    assert final is empty
    assert record is None
    assert client.calls == []


def test_critic_sees_photo_and_draft():
    client = QueueClient(Critique(challenges=[], overall_assessment="sound"))
    run_debate("QUJD", "image/jpeg", _draft(), client=client, hint="no dressing")
    (call,) = client.calls
    blocks = call["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert "chicken caesar wrap" in blocks[1]["text"]
    assert "no dressing" in blocks[1]["text"]
    assert call["output_format"] is Critique


# --- through the pipeline ------------------------------------------------------


def test_pipeline_uses_debated_final(photo_path):
    client = QueueClient(_draft(), _critique_with_challenge(), _debated_final())
    meal, _ = pipeline.run(photo_path, client=client)
    assert len(client.calls) == 3  # analyzer, critic, reviser
    assert meal.items[0].grams == 260  # the revised portion, not the draft's 180
    assert meal.debate is not None
    assert meal.debate["rulings"][0]["verdict"] == "accepted"


def test_report_shows_debate_summary(photo_path):
    client = QueueClient(_draft(), _critique_with_challenge(), _debated_final())
    meal, _ = pipeline.run(photo_path, client=client)
    text = report.to_text(meal, "wrap.jpg")
    assert "debate: 1 challenge(s) raised - 1 led to corrections, 0 rejected" in text
    payload = report.to_dict(meal, "wrap.jpg")
    assert payload["debate"]["challenges"][0]["kind"] == "portion_too_low"


def test_report_omits_debate_line_when_clean(photo_path, sample_analysis):
    client = QueueClient(sample_analysis, Critique(challenges=[], overall_assessment="ok"))
    meal, _ = pipeline.run(photo_path, client=client)
    assert "debate:" not in report.to_text(meal, "meal.jpg")
    assert report.to_dict(meal, "meal.jpg")["debate"]["challenges"] == []
