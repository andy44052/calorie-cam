"""Glue: photo (path or raw bytes) in, (MealEstimate, raw FoodAnalysis) out."""

import io
from pathlib import Path

import anthropic

from . import debate as debate_mod
from . import lookup
from .config import DEFAULT_MODEL
from .sanity import MealEstimate, evaluate
from .schema import FoodAnalysis
from .vision import analyze_prepared, prepare_image


def _run(source, model: str, client, hint, debate: bool) -> tuple[MealEstimate, FoodAnalysis]:
    image_b64, media_type = prepare_image(source)
    if client is None:
        client = anthropic.Anthropic()

    analysis = analyze_prepared(
        image_b64, media_type, model=model, client=client, hint=hint
    )

    record = None
    if debate:
        analysis, record = debate_mod.run_debate(
            image_b64, media_type, analysis, model=model, client=client, hint=hint
        )

    resolutions = lookup.resolve_all(analysis.items)
    meal = evaluate(analysis, resolutions)
    meal.debate = record
    return meal, analysis


def run(
    path: str | Path,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: str | None = None,
    debate: bool = True,
) -> tuple[MealEstimate, FoodAnalysis]:
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    return _run(path, model, client, hint, debate)


def run_bytes(
    data: bytes,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: str | None = None,
    debate: bool = True,
) -> tuple[MealEstimate, FoodAnalysis]:
    return _run(io.BytesIO(data), model, client, hint, debate)
