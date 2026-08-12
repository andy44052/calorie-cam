from types import SimpleNamespace

import pytest

from caloriecam import pipeline, report
from caloriecam.debate import Critique


class _FakeMessages:
    """Answers the analysis call with the given draft and any critic call with
    'no challenges', so pipeline tests exercise the debate-skip path."""

    def __init__(self, analysis):
        self._analysis = analysis
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["output_format"] is Critique:
            parsed = Critique(challenges=[], overall_assessment="draft is sound")
        else:
            parsed = self._analysis
        return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn")


class FakeClient:
    def __init__(self, analysis):
        self.messages = _FakeMessages(analysis)


def test_end_to_end_with_fake_client(photo_path, sample_analysis):
    client = FakeClient(sample_analysis)
    meal, analysis = pipeline.run(photo_path, client=client)
    assert analysis is sample_analysis
    assert (meal.total_low, meal.total_mid, meal.total_high) == (392, 508, 623)
    assert meal.debate is not None and meal.debate["challenges"] == []

    text = report.to_text(meal, str(photo_path))
    assert "TOTAL" in text

    payload = report.to_dict(meal, str(photo_path))
    assert payload["total_kcal"]["mid"] == 508


def test_debate_off_makes_single_call(photo_path, sample_analysis):
    client = FakeClient(sample_analysis)
    meal, _ = pipeline.run(photo_path, client=client, debate=False)
    assert len(client.messages.calls) == 1
    assert meal.debate is None


def test_missing_file_raises_before_api_call(tmp_path, sample_analysis):
    with pytest.raises(FileNotFoundError):
        pipeline.run(tmp_path / "ghost.jpg", client=FakeClient(sample_analysis))


def test_no_food_path(photo_path, empty_analysis):
    client = FakeClient(empty_analysis)
    meal, _ = pipeline.run(photo_path, client=client)
    assert meal.items == []
    assert meal.debate is None  # nothing to argue about
    assert len(client.messages.calls) == 1  # no critic call for an empty draft
    assert "No food or drink detected." in report.to_text(meal, "x.jpg")
