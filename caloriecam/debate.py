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
from .vision import TruncatedError, structured_call


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

# Appended when the critic runs on a cheaper model than the lead analyst.
# Measured (Run B/D, 2026-08-18): cheap critics raise about HALF the
# portion_too_low challenges of the lead-tier critic, and the system's old
# undercounting returns. Small models need the expectation stated outright.
EAGER_CRITIC_SUPPLEMENT = """\

You are reviewing the work of a stronger model, so be thorough, not deferential:
- Check EVERY draft item one by one: portion grams, kcal_per_100g, count.
- Vision drafts systematically UNDERESTIMATE portions and hidden fat. On any \
restaurant-style, sauced, fried, or multi-item photo, a sound-looking draft \
usually still hides 1-3 real problems - find them.
- A draft with several items and zero challenges should be rare. Only return \
an empty list for genuinely trivial photos (a single plain fruit, a drink).
- Still argue from evidence; never fabricate an objection you cannot defend.
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
    ledger=None,
    eager: bool = False,
) -> Critique:
    text = (
        "Here is the draft analysis to challenge:\n"
        + draft.model_dump_json(indent=2)
        + _hint_line(hint)
    )
    messages = [
        {"role": "user", "content": [_image_block(image_b64, media_type), {"type": "text", "text": text}]}
    ]
    system = CRITIC_SYSTEM + (EAGER_CRITIC_SUPPLEMENT if eager else "")
    return structured_call(
        client=client,
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
        output_format=Critique,
        ledger=ledger,
        stage="critic",
    )


# A challenge is "the same" as another when it disputes the same thing in the
# same direction; the reviser only needs to hear it once.
_MERGE_CAP = 12


def merge_critiques(critiques: list[Critique]) -> Critique:
    seen: set[tuple] = set()
    merged: list[Challenge] = []
    for critique in critiques:
        for ch in critique.challenges:
            key = (ch.kind, ch.target.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append(ch)
    assessment = " / ".join(
        c.overall_assessment for c in critiques if c.overall_assessment
    )
    return Critique(challenges=merged[:_MERGE_CAP], overall_assessment=assessment)


def revise(
    image_b64: str,
    media_type: str,
    draft: FoodAnalysis,
    critique: Critique,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: Optional[str] = None,
    ledger=None,
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
        ledger=ledger,
        stage="reviser",
    )


def run_debate(
    image_b64: str,
    media_type: str,
    draft: FoodAnalysis,
    model: str = DEFAULT_MODEL,
    client=None,
    hint: Optional[str] = None,
    ledger=None,
    skeptic_model: Optional[str] = None,
    critic_count: int = 1,
) -> tuple[FoodAnalysis, Optional[dict]]:
    """Challenge the draft; return (final_analysis, debate_record).

    The record is None when there was nothing to debate (no items found).
    ``skeptic_model`` runs the critic and reviser on a different (cheaper)
    model than the primary analyst; None keeps everything on ``model``.
    ``critic_count`` runs several independent critics and unions their
    challenges - a cheap-critic ensemble recovers the challenge coverage a
    single cheap critic lacks (measured Run B/D), while 2-3 cheap critics
    still cost less than one lead-tier critic. When a cheap skeptic is in
    play, the critic prompt also switches to the eager variant.
    """
    if not draft.items:
        return draft, None

    debate_model = skeptic_model or model
    eager = bool(skeptic_model) and skeptic_model != model
    critic_count = max(1, critic_count)
    critiques = [
        criticize(
            image_b64, media_type, draft, model=debate_model, client=client,
            hint=hint, ledger=ledger, eager=eager,
        )
        for _ in range(critic_count)
    ]
    critique = merge_critiques(critiques) if critic_count > 1 else critiques[0]
    record = {
        "challenges": [c.model_dump() for c in critique.challenges],
        "assessment": critique.overall_assessment,
        "rulings": [],
    }
    if critic_count > 1:
        record["critic_count"] = critic_count
        record["per_critic_challenges"] = [len(c.challenges) for c in critiques]
    if not critique.challenges:
        return draft, record

    try:
        debated = revise(
            image_b64,
            media_type,
            draft,
            critique,
            model=debate_model,
            client=client,
            hint=hint,
            ledger=ledger,
        )
    except TruncatedError:
        # The reviser is the longest output in the pipeline; on a rich photo it
        # can still overrun. The draft is a complete, valid analysis - returning
        # it un-revised beats losing an estimate the user already paid for.
        record["reviser_truncated"] = True
        return draft, record
    if skeptic_model:
        record["skeptic_model"] = skeptic_model
    record["rulings"] = [r.model_dump() for r in debated.rulings]
    # Per-verdict counts, not just a corrections tally: "partially accepted"
    # is real pushback (the analyst moved less than demanded), so collapsing
    # it into "accepted" would overstate how often the reviser rubber-stamps.
    record["verdict_counts"] = {
        verdict: sum(1 for r in debated.rulings if r.verdict == verdict)
        for verdict in ("accepted", "partially_accepted", "rejected")
    }
    return debated.final, record
