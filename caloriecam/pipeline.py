"""Glue: photo (path or raw bytes) in, (MealEstimate, raw FoodAnalysis) out."""

import io
from pathlib import Path

import anthropic

from . import debate as debate_mod
from . import lookup
from .config import DEFAULT_MODEL, SKIP_DEBATE_MAX_ITEMS
from .sanity import MealEstimate, evaluate
from .schema import FoodAnalysis
from .usage import UsageLedger
from .vision import analyze_prepared, prepare_image


def _worth_debating(analysis: FoodAnalysis) -> bool:
    """Skip the adversarial pass on drafts it has never found fault with.

    Benchmark evidence: the only all-high-confidence runs (two apples) were
    also the only runs where the critic raised zero challenges. Simple,
    few-item, high-confidence photos pay two extra calls for nothing.
    """
    if not analysis.items:
        return False
    if len(analysis.items) > SKIP_DEBATE_MAX_ITEMS:
        return True
    return not all(item.confidence == "high" for item in analysis.items)


def _run(source, model: str, client, hint, debate: bool) -> tuple[MealEstimate, FoodAnalysis]:
    ledger = UsageLedger()
    image_b64, media_type = prepare_image(source)
    if client is None:
        client = anthropic.Anthropic()

    analysis = analyze_prepared(
        image_b64, media_type, model=model, client=client, hint=hint, ledger=ledger
    )

    record = None
    if debate and _worth_debating(analysis):
        analysis, record = debate_mod.run_debate(
            image_b64, media_type, analysis, model=model, client=client, hint=hint,
            ledger=ledger,
        )

    resolutions = lookup.resolve_all(analysis.items)
    meal = evaluate(analysis, resolutions)
    meal.debate = record
    meal.usage = ledger.as_dict()
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
