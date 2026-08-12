"""Phase 2: match vision output against the bundled known-foods database.

Menu items (fastfood.json) carry a total-kcal range for a standardized item, so
a hit replaces the grams x density math entirely. Generic foods (generic.json)
carry kcal/100g, so a hit replaces only the model's energy-density guess while
keeping its portion estimate.

Matching philosophy: a wrong match is worse than no match (the model's own
estimate is a decent fallback). A single-ingredient entry must never claim a
combined dish - "beef and broccoli" is not broccoli, "banana bread" is not
banana. An alias may therefore match *inside* a longer item name only when the
leftover words are harmless modifiers ("steamed", "large", counts), or when the
entry itself is a combined dish that absorbs ingredient words (loose_match).
"""

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .sanity import SOURCE_DB_BRANDED, SOURCE_DB_GENERIC
from .schema import FoodItem

_DATA_DIR = Path(__file__).parent / "data"

# Menu names are short and distinctive, so demand a closer match than for
# generic foods (avoids "french fries" latching onto a specific size).
MENU_THRESHOLD = 0.80
GENERIC_THRESHOLD = 0.78

_STOPWORDS = {"a", "an", "the", "of", "with", "and", "on", "in", "style"}

# Words that never change WHAT a food is (only how it looks or how much).
# Deliberately absent: fried, breaded, crispy, creamy, buttered, loaded,
# glazed, sweetened - those change the calories.
_MODIFIERS = frozenset({
    "cooked", "raw", "fresh", "plain", "homemade",
    "steamed", "boiled", "grilled", "baked", "roasted", "broiled", "poached",
    "sliced", "diced", "chopped", "shredded", "cut",
    "piece", "slice", "cup", "bowl", "plate", "glass", "can", "bottle",
    "serving", "portion", "order", "side", "handful", "bunch", "bit",
    "small", "medium", "large", "big", "mini", "regular", "whole", "half",
    "skinless", "boneless", "hot", "cold", "warm", "iced", "leftover",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "single", "couple", "few",
})

# Per-unit items (pizza slices, tacos): scale the kcal by count when the
# model's gram estimate says there is clearly more than one unit.
_SCALE_MIN_RATIO = 1.6
_MAX_UNITS = 8


@dataclass
class Resolution:
    source: str
    matched_name: str
    count: int = 1
    kcal_low: Optional[int] = None
    kcal_mid: Optional[int] = None
    kcal_high: Optional[int] = None
    kcal_per_100g: Optional[float] = None
    serving_g: Optional[float] = None


def _norm(text: str) -> str:
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    return " ".join(text.split())


def _stem(token: str) -> str:
    # Cheap plural folding: "eggs"->"egg", "slices"->"slice". Both sides of
    # every comparison go through this, so consistency matters more than
    # linguistic correctness.
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        _stem(t) for t in _norm(text).split() if t not in _STOPWORDS
    )


def _leftover_allowed(leftover: frozenset, extra_ok: frozenset) -> bool:
    return all(t.isdigit() or t in _MODIFIERS or t in extra_ok for t in leftover)


def _score(
    item_tokens: frozenset,
    item_norm: str,
    candidate: str,
    loose: bool,
    extra_ok: frozenset,
) -> float:
    b = _tokens(candidate)
    if not item_tokens or not b:
        return 0.0
    if item_tokens == b:
        return 1.0
    if b <= item_tokens:
        if loose or _leftover_allowed(item_tokens - b, extra_ok):
            return 0.95
        # A smaller food name inside a bigger dish name ("banana" in "banana
        # bread"): never let the string-similarity ratio rescue it.
        return len(item_tokens & b) / len(item_tokens | b)
    jaccard = len(item_tokens & b) / len(item_tokens | b)
    ratio = SequenceMatcher(None, item_norm, _norm(candidate)).ratio()
    return max(jaccard, ratio)


def _entry_score(item_name: str, entry: dict, extra_ok: frozenset = frozenset()) -> float:
    item_tokens = _tokens(item_name)
    if any(t in item_tokens for t in entry.get("exclude_tokens", [])):
        return 0.0
    item_norm = _norm(item_name)
    loose = bool(entry.get("loose_match"))
    display = entry.get("item") or entry.get("name") or ""
    candidates = [display, *entry.get("aliases", [])]
    return max(
        _score(item_tokens, item_norm, candidate, loose, extra_ok)
        for candidate in candidates
    )


@lru_cache(maxsize=1)
def _menu_items() -> tuple[dict, ...]:
    payload = json.loads((_DATA_DIR / "fastfood.json").read_text(encoding="utf-8"))
    return tuple(payload["menu_items"])


@lru_cache(maxsize=1)
def _generic_foods() -> tuple[dict, ...]:
    payload = json.loads((_DATA_DIR / "generic.json").read_text(encoding="utf-8"))
    return tuple(payload["foods"])


def _brand_ok(item: FoodItem, entry: dict) -> bool:
    entry_brand = entry.get("brand")
    if not entry_brand:
        return True
    wanted = _tokens(entry_brand)
    present = _tokens(item.brand or "") | _tokens(item.name)
    return wanted <= present


def _display_name(entry: dict) -> str:
    if entry.get("brand"):
        return f"{entry['brand']} {entry['item']}"
    return entry["item"]


def match_menu_item(item: FoodItem) -> Optional[Resolution]:
    best: Optional[dict] = None
    best_score = 0.0
    for entry in _menu_items():
        if not _brand_ok(item, entry):
            continue
        # Brand words in the item name ("McDonald's Big Mac") are not a
        # mismatch signal.
        extra_ok = _tokens(entry.get("brand") or "")
        score = _entry_score(item.name, entry, extra_ok)
        if score > best_score:
            best, best_score = entry, score
    if best is None or best_score < MENU_THRESHOLD:
        return None

    count = 1
    serving = best.get("serving_g")
    if best.get("unit_scalable") and serving:
        ratio = item.estimated_grams / serving
        if ratio >= _SCALE_MIN_RATIO:
            count = min(_MAX_UNITS, max(1, round(ratio)))

    kcal = best["kcal"]
    return Resolution(
        source=SOURCE_DB_BRANDED,
        matched_name=_display_name(best),
        count=count,
        kcal_low=kcal.get("low", kcal["mid"]),
        kcal_mid=kcal["mid"],
        kcal_high=kcal.get("high", kcal["mid"]),
        serving_g=serving,
    )


def match_generic(item: FoodItem) -> Optional[Resolution]:
    best: Optional[dict] = None
    best_score = 0.0
    for entry in _generic_foods():
        score = _entry_score(item.name, entry)
        if score > best_score:
            best, best_score = entry, score
    if best is None or best_score < GENERIC_THRESHOLD:
        return None
    return Resolution(
        source=SOURCE_DB_GENERIC,
        matched_name=best["name"],
        kcal_per_100g=best["kcal_per_100g"],
        serving_g=best.get("typical_serving_g"),
    )


def resolve(item: FoodItem) -> Optional[Resolution]:
    return match_menu_item(item) or match_generic(item)


def resolve_all(items: list[FoodItem]) -> list[Optional[Resolution]]:
    return [resolve(item) for item in items]
