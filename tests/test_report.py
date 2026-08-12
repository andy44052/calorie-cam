import json

from caloriecam import report
from caloriecam.sanity import evaluate
from caloriecam.schema import FoodAnalysis, FoodItem


def test_text_report_contents(sample_analysis):
    meal = evaluate(sample_analysis)
    text = report.to_text(meal, "meal.jpg")
    assert "CalorieCam  meal.jpg" in text
    assert "grilled chicken breast" in text
    assert "white rice" in text
    assert "~ 508 kcal" in text or "~508 kcal" in text.replace("~ ", "~")
    assert "(392-623)" in text
    assert "[high]" in text and "[medium]" in text
    assert "scale reference: dinner plate" in text


def test_text_report_is_ascii(sample_analysis):
    meal = evaluate(sample_analysis)
    text = report.to_text(meal, "meal.jpg")
    text.encode("ascii")  # raises if any non-ascii slipped in


def test_no_food_report(empty_analysis):
    meal = evaluate(empty_analysis)
    text = report.to_text(meal, "keyboard.jpg")
    assert "No food or drink detected." in text
    assert "laptop keyboard" in text
    assert "TOTAL" not in text


def test_brand_prefixed_in_display():
    analysis = FoodAnalysis(
        items=[
            FoodItem(
                name="Big Mac",
                portion_description="one burger",
                estimated_grams=220,
                kcal_per_100g=257,
                confidence="high",
                assumptions=[],
                brand="McDonald's",
            )
        ]
    )
    text = report.to_text(evaluate(analysis), "lunch.jpg")
    assert "McDonald's Big Mac" in text


def test_brand_not_duplicated_when_already_in_name():
    analysis = FoodAnalysis(
        items=[
            FoodItem(
                name="McDonald's Big Mac",
                portion_description="one burger",
                estimated_grams=220,
                kcal_per_100g=257,
                confidence="high",
                assumptions=[],
                brand="McDonald's",
            )
        ]
    )
    text = report.to_text(evaluate(analysis), "lunch.jpg")
    assert "McDonald's McDonald's" not in text


def test_long_names_truncated():
    analysis = FoodAnalysis(
        items=[
            FoodItem(
                name="extremely long dish name that would wreck the table layout",
                portion_description="a plate",
                estimated_grams=300,
                kcal_per_100g=150,
                confidence="low",
                assumptions=[],
                brand=None,
            )
        ]
    )
    text = report.to_text(evaluate(analysis), "dinner.jpg")
    assert "..." in text
    longest = max(len(line) for line in text.splitlines())
    assert longest < 100


def test_json_report_shape(sample_analysis):
    meal = evaluate(sample_analysis)
    payload = report.to_dict(meal, "meal.jpg")
    assert payload["image"] == "meal.jpg"
    assert payload["total_kcal"] == {"low": 392, "mid": 508, "high": 623}
    assert len(payload["items"]) == 2
    first = payload["items"][0]
    assert first["name"] == "grilled chicken breast"
    assert first["kcal"] == {"low": 210, "mid": 248, "high": 285}
    assert first["source"] == "model_estimate"
    json.dumps(payload)  # must be serializable as-is


def test_json_report_empty(empty_analysis):
    payload = report.to_dict(evaluate(empty_analysis), "keyboard.jpg")
    assert payload["items"] == []
    assert payload["total_kcal"]["mid"] == 0
