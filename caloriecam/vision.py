"""Layer 1: photo -> structured FoodAnalysis via the Claude vision API."""

import base64
import io
import time
from pathlib import Path
from typing import BinaryIO

import anthropic
from PIL import Image, ImageOps

from . import usage as usage_mod
from .config import DEFAULT_MODEL, HINT_MAX_CHARS, JPEG_QUALITY, MAX_IMAGE_PX, MAX_TOKENS
from .schema import FoodAnalysis

try:  # iPhone HEIC photos open transparently when pillow-heif is present
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass


class VisionError(RuntimeError):
    """The model call completed but produced no usable analysis."""


class RefusalError(VisionError):
    """The API declined the request (stop_reason == 'refusal')."""


SYSTEM_PROMPT = """\
You are the vision engine of a calorie-estimation app. You receive one photo \
and must identify the food in it and estimate calories. Be rigorous and honest \
about uncertainty.

Identification:
- List every distinct food and drink item you can see. A composite dish \
(burger, burrito, curry served over rice) is ONE item; foods plated separately \
are separate items.
- If an item is a recognizable branded or chain-restaurant product (fast food, \
packaged snack with a visible label), set "brand" to the chain/brand and name \
the item as precisely as you can (e.g. brand "McDonald's", name "Big Mac").

Frame scope:
- Estimate ONLY items that are fully or mostly (more than half) visible in the \
photo. If food is cut off at the edge of the frame, exclude it and mention the \
exclusion in "overall_notes". Every analysis of the same photo must draw the \
same boundary.
- When an item IS mostly visible but still extends past the frame edge, \
estimate the portion INSIDE the frame only - never the whole object it belongs \
to. A cheese wheel cropped by the edge is scored on the visible wedge, not the \
whole wheel; state the visible fraction you assumed in that item's assumptions.

Portions:
- Estimate edible weight in grams. For liquids use 1 ml = 1 g.
- COUNT FIRST when the food is made of discrete units (grapes, berries, sushi \
pieces, tacos, eggs, cookies, nuggets, meatballs, pizza slices): count the \
units you can see, estimate hidden ones conservatively, set "unit_count", and \
compute grams as count x typical per-unit weight. State the count and \
per-unit weight in the item's assumptions. For bulk foods (rice, pasta, \
mashed potatoes, sauces) set "unit_count" to null.
- Judge scale from visible references: a dinner plate is ~27 cm across, a fork \
is ~19 cm long, a 12 oz can is ~12 cm tall, an adult hand is ~18 cm. Report \
which reference you used in "scale_reference".
- Consider plate coverage AND pile height/density. Rice and pasta are denser \
than they look; leafy salad is lighter.

Portion reference points:
- 85 g of cooked meat looks like a deck of cards or a palm (no fingers).
- 1 cup (~160 g) of cooked rice or pasta is about a baseball / a closed fist.
- Restaurant portions usually run 1.5-2x the label "serving size" - when a \
plate looks generous, size UP, not down.
- A "double" or "loaded" version of a composite item (burger, burrito) is \
typically +40-70% over the regular, not exactly 2x.
- Deep-frying adds absorbed oil (fries are ~300+ kcal/100g); baked or \
air-fried versions of the same food are much lower per gram.

Energy density:
- "kcal_per_100g" is for the food AS PREPARED, not raw. Assume typical \
home/restaurant preparation: food is usually cooked in oil or butter, and \
dressings/sauces are often present. Hidden fat is the single biggest source of \
undercounting - do not lowball it.

Confidence and assumptions:
- confidence "high": clear view, well-known food, decent scale cue. "medium": \
partly obscured, mixed dish, or weak scale cues. "low": significant guessing \
about the food or the amount.
- List the assumptions behind each item (preparation, hidden ingredients, what \
you based the portion on).

User-provided context:
- The request may include extra context typed by the person who took the photo \
(ingredients, cooking oil, preparation, brand, "it's all organic", portion \
knowledge). Treat it as ground truth over visual guesses - they can see and \
taste what the camera cannot - and adjust identification, portions, and \
kcal_per_100g accordingly. Reflect what you used in each item's assumptions.
- Context refines what is visible; it never adds items. Do not invent food \
that is not in the photo, even if the context mentions it. If the context \
contradicts the image, say so in "overall_notes".

If the photo contains no food or drink, return an empty "items" list and \
explain why in "overall_notes".
"""

USER_PROMPT = "Analyze this photo and estimate the calories of everything shown."


def prepare_image(
    source: str | Path | BinaryIO, max_px: int = MAX_IMAGE_PX
) -> tuple[str, str]:
    """Load, orient, downscale, and re-encode a photo. Returns (base64, media_type)."""
    img = Image.open(source)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > max_px:
        img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return data, "image/jpeg"


def build_messages(
    image_b64: str, media_type: str, hint: str | None = None
) -> list[dict]:
    text = USER_PROMPT
    if hint:
        text += (
            "\n\nExtra context from the person who took the photo (trust it - they "
            f'can see and taste what the camera cannot): "{hint}"'
        )
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": text},
            ],
        }
    ]


def structured_call(
    client,
    model: str,
    max_tokens: int,
    system: str,
    messages: list[dict],
    output_format,
    ledger=None,
    stage: str = "analyze",
):
    """One structured-output API call with refusal/empty handling."""
    if client is None:
        client = anthropic.Anthropic()

    started = time.monotonic()
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        output_format=output_format,
    )
    if ledger is not None:
        ledger.record(
            usage_mod.from_response(
                response, stage=stage, model=model, seconds=time.monotonic() - started
            )
        )

    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        explanation = getattr(details, "explanation", None) or "no explanation provided"
        raise RefusalError(f"the API declined to analyze this image: {explanation}")

    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        raise VisionError(
            f"model returned no parseable analysis (stop_reason={stop_reason})"
        )
    return parsed


def analyze_prepared(
    image_b64: str,
    media_type: str,
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
    hint: str | None = None,
    ledger=None,
) -> FoodAnalysis:
    """Run the vision analysis on an already-prepared image."""
    if hint:
        hint = hint.strip()[:HINT_MAX_CHARS] or None
    return structured_call(
        client=client,
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=build_messages(image_b64, media_type, hint=hint),
        output_format=FoodAnalysis,
        ledger=ledger,
        stage="analyze",
    )


def analyze_image(
    source: str | Path | BinaryIO,
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
    max_px: int = MAX_IMAGE_PX,
    hint: str | None = None,
) -> FoodAnalysis:
    """Run the vision call and return the validated FoodAnalysis."""
    image_b64, media_type = prepare_image(source, max_px=max_px)
    return analyze_prepared(
        image_b64, media_type, model=model, client=client, hint=hint
    )
