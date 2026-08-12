import pytest
from pydantic import ValidationError

from caloriecam.schema import FoodAnalysis, FoodItem


def test_parses_full_payload():
    payload = {
        "items": [
            {
                "name": "cheeseburger",
                "portion_description": "one standard burger",
                "estimated_grams": 220,
                "kcal_per_100g": 260,
                "confidence": "high",
                "assumptions": ["includes cheese and sauce"],
                "brand": "McDonald's",
            }
        ],
        "scale_reference": "hand holding the burger",
        "overall_notes": None,
    }
    analysis = FoodAnalysis.model_validate(payload)
    assert analysis.items[0].brand == "McDonald's"
    assert analysis.items[0].estimated_grams == 220


def test_grams_coerced_to_float():
    item = FoodItem(
        name="apple",
        portion_description="one medium apple",
        estimated_grams=180,
        kcal_per_100g=52,
        confidence="high",
        assumptions=[],
    )
    assert isinstance(item.estimated_grams, float)
    assert item.brand is None


def test_invalid_confidence_rejected():
    with pytest.raises(ValidationError):
        FoodItem(
            name="apple",
            portion_description="one",
            estimated_grams=180,
            kcal_per_100g=52,
            confidence="certain",
            assumptions=[],
        )


def test_empty_items_allowed():
    analysis = FoodAnalysis(items=[], overall_notes="no food")
    assert analysis.items == []
    assert analysis.scale_reference is None
