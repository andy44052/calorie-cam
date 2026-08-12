from types import SimpleNamespace

import pytest

from caloriecam import pipeline, report


class _FakeMessages:
    def __init__(self, response):
        self._response = response

    def parse(self, **kwargs):
        return self._response


class FakeClient:
    def __init__(self, analysis):
        self.messages = _FakeMessages(
            SimpleNamespace(parsed_output=analysis, stop_reason="end_turn")
        )


def test_end_to_end_with_fake_client(photo_path, sample_analysis):
    meal, analysis = pipeline.run(photo_path, client=FakeClient(sample_analysis))
    assert analysis is sample_analysis
    assert (meal.total_low, meal.total_mid, meal.total_high) == (392, 508, 623)

    text = report.to_text(meal, str(photo_path))
    assert "TOTAL" in text

    payload = report.to_dict(meal, str(photo_path))
    assert payload["total_kcal"]["mid"] == 508


def test_missing_file_raises_before_api_call(tmp_path, sample_analysis):
    with pytest.raises(FileNotFoundError):
        pipeline.run(tmp_path / "ghost.jpg", client=FakeClient(sample_analysis))


def test_no_food_path(photo_path, empty_analysis):
    meal, _ = pipeline.run(photo_path, client=FakeClient(empty_analysis))
    assert meal.items == []
    assert "No food or drink detected." in report.to_text(meal, "x.jpg")
