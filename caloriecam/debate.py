"""Adversarial review: a skeptic challenges the draft, the lead analyst rules.

Flow (all calls see the same photo):
  1. draft   - the normal analysis (vision.analyze_prepared)
  2. critic  - must find concrete problems, with an argument for each;
               instructed to return ZERO challenges if the draft is sound
  3. reviser - rules on every challenge with a reason (accept / partially
               accept / reject) and emits the final corrected analysis

The reviser call is skipped when the critic raises nothing, and the whole
debate is skipped when the draft found no food.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .config import DEFAULT_MODEL, MAX_TOKENS
from .schema import FoodAnalysis
from .vision import structured_call


class Challenge(BaseModel):
    target: str = Field(
        description="Which draft item this disputes (its name), or 'missing item' / 'overall'"
    )
    kind: Literal[
        "missed_item",
        "hallucinated_item",
        "portion_too_low",
        "portion_too_high",
        "density_too_low",
        "density_too_high",
        "wrong_identification",
    ] = Field(description="The type of error being alleged")
    argument: str = Field(
        description="Concrete reasoning from visible evidence or strong priors - why the draft is wrong"
    )


class Critique(BaseModel):
    challenges: list[Challenge] = Field(
        description="Every defensible objection to the draft; EMPTY if the draft is sound"
    )
    overall_assessment: str = Field(
        description="One or two sentences on the draft's overall quality"
    )


class ChallengeRuling(BaseModel):
    challenge: str = Field(description="Short restatement of the challenge being ruled on")
    verdict: Literal["accepted", "partially_accepted", "rejected"] = Field(
        description="Whether the challenge changed the analysis"
    )
    reason: str = Field(description="Why - the evidence that decided it")


class DebatedAnalysis(BaseModel):
    rulings: list[ChallengeRuling] = Field(
        description="One ruling per challenge, in the same order"
    )
    final: FoodAnalysis = Field(
        description="The corrected final analysis (unchanged items stay unchanged)"
    )


CRITIC_SYSTEM = """\
You are the skeptical second opinion in a calorie-estimation system. You get a \
photo of food and another analyst's draft estimate. Your only job is to find \
what is WRONG with the draft. Look for, in order of how common they are:

- Missed items: sauces, dressings, cooking oil or butter glaze, spreads, \
drinks, sides at the edge of frame, anything under or behind other food.
- Portions too LOW: undercounting is the classic failure. Compare against the \
plate size and pile height. Restaurant portions run 1.5-2x label servings.
- Energy density too low: values that look like the raw or plain food when \
the photo shows restaurant/fried/sauced preparation.
- Hallucinated items: anything in the draft you cannot actually see.
- Portions or density too HIGH, or the food is misidentified outright.

Rules: every challenge needs a concrete argument from visible evidence or \
strong priors - "looks small" is not an argument, "the burrito spans most of \
a 27 cm plate, so 200 g is implausibly low" is. When an item states a \
unit_count, verify the count against what you can actually count in the photo. Do NOT invent objections to \
seem useful: if the draft is sound, return an empty challenges list. Respect \
any user-provided context; do not challenge facts the user stated.
"""

REVISER_SYSTEM = """\
You are the lead analyst in a calorie-estimation system. A skeptical reviewer \
has challenged your draft analysis of this photo. Rule on EVERY challenge, in \
order, against what you can actually see:

- accepted: the challenge is right - fix the analysis accordingly.
- partially_accepted: directionally right - adjust, but not as far as claimed.
- rejected: the challenge is wrong - say what evidence refutes it.

Give a real reason for every ruling; never cave to a weak argument and never \
defend a clear mistake. Then output the final analysis: apply exactly the \
accepted corrections, keep everything else unchanged, and follow the same \
estimation rules as before (grams, kcal_per_100g as prepared, confidence, \
assumptions). If a correction changes an item, note why in its assumptions.
"""


def _image_block(image_b64: str, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": image_b64},
    }


def _hint_line(hint: Optional[str]) -> str:
    if not hint:
        return ""
    return f'\n\nUser-provided context (treat as ground truth): "{hint}"'


def criticize(
    image_b64: str,
    media_type: str,
    draft: FoodAnalysis,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: Optional[str] = None,
) -> Critique:
    text = (
        "Here is the draft analysis to challenge:\n"
        + draft.model_dump_json(indent=2)
        + _hint_line(hint)
    )
    messages = [
        {"role": "user", "content": [_image_block(image_b64, media_type), {"type": "text", "text": text}]}
    ]
    return structured_call(
        client=client,
        model=model,
        max_tokens=MAX_TOKENS,
        system=CRITIC_SYSTEM,
        messages=messages,
        output_format=Critique,
    )


def revise(
    image_b64: str,
    media_type: str,
    draft: FoodAnalysis,
    critique: Critique,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: Optional[str] = None,
) -> DebatedAnalysis:
    text = (
        "Your draft analysis:\n"
        + draft.model_dump_json(indent=2)
        + "\n\nThe reviewer's challenges:\n"
        + critique.model_dump_json(indent=2)
        + _hint_line(hint)
    )
    messages = [
        {"role": "user", "content": [_image_block(image_b64, media_type), {"type": "text", "text": text}]}
    ]
    return structured_call(
        client=client,
        model=model,
        max_tokens=MAX_TOKENS,
        system=REVISER_SYSTEM,
        messages=messages,
        output_format=DebatedAnalysis,
    )


def run_debate(
    image_b64: str,
    media_type: str,
    draft: FoodAnalysis,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: Optional[str] = None,
) -> tuple[FoodAnalysis, Optional[dict]]:
    """Challenge the draft; return (final_analysis, debate_record).

    The record is None when there was nothing to debate (no items found).
    """
    if not draft.items:
        return draft, None

    critique = criticize(image_b64, media_type, draft, model=model, client=client, hint=hint)
    record = {
        "challenges": [c.model_dump() for c in critique.challenges],
        "assessment": critique.overall_assessment,
        "rulings": [],
    }
    if not critique.challenges:
        return draft, record

    debated = revise(
        image_b64, media_type, draft, critique, model=model, client=client, hint=hint
    )
    record["rulings"] = [r.model_dump() for r in debated.rulings]
    return debated.final, record
