"""Glue: photo (path or raw bytes) in, (MealEstimate, raw FoodAnalysis) out."""

import io
from pathlib import Path

from . import lookup
from .config import DEFAULT_MODEL
from .sanity import MealEstimate, evaluate
from .schema import FoodAnalysis
from .vision import analyze_image


def _run(source, model: str, client, hint) -> tuple[MealEstimate, FoodAnalysis]:
    analysis = analyze_image(source, model=model, client=client, hint=hint)
    resolutions = lookup.resolve_all(analysis.items)
    return evaluate(analysis, resolutions), analysis


def run(
    path: str | Path,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: str | None = None,
) -> tuple[MealEstimate, FoodAnalysis]:
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    return _run(path, model, client, hint)


def run_bytes(
    data: bytes,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: str | None = None,
) -> tuple[MealEstimate, FoodAnalysis]:
    return _run(io.BytesIO(data), model, client, hint)
