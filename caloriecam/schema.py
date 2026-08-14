"""Pydantic schema the vision model is forced to fill in (structured output)."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class FoodItem(BaseModel):
    name: str = Field(
        description="Short name of the food or drink, e.g. 'grilled chicken breast'"
    )
    portion_description: str = Field(
        description="Human-readable portion, e.g. 'about 1 cup, covers half the plate'"
    )
    estimated_grams: float = Field(
        description="Estimated edible weight in grams (for liquids, 1 ml = 1 g)"
    )
    kcal_per_100g: float = Field(
        description="Calories per 100 g of this food as prepared (including cooking fat)"
    )
    unit_count: Optional[int] = Field(
        default=None,
        description="Number of discrete units when the food is countable (grapes, sushi pieces, tacos, eggs, slices); null for bulk foods like rice or sauces",
    )
    per_unit_grams: Optional[float] = Field(
        default=None,
        description="Edible weight of ONE unit in grams; set this whenever unit_count is set, so that unit_count x per_unit_grams equals estimated_grams",
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="Confidence in both the identification and the portion estimate"
    )
    assumptions: list[str] = Field(
        description="Assumptions made: preparation method, hidden ingredients, portion basis"
    )
    brand: Optional[str] = Field(
        default=None,
        description="Chain or brand name if this is a recognizable branded item, else null",
    )


class FoodAnalysis(BaseModel):
    items: list[FoodItem] = Field(
        description="One entry per distinct food or drink; empty if no food is visible"
    )
    scale_reference: Optional[str] = Field(
        default=None,
        description="Visual reference used to judge portion sizes, e.g. 'dinner plate', or null",
    )
    overall_notes: Optional[str] = Field(
        default=None,
        description="Anything affecting accuracy (lighting, occlusion, ambiguity), or why items is empty",
    )
