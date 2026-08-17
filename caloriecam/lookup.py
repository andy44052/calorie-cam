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
from functools import lru_cache
from pathlib import Path
from typing import Optional

from . import text as text_mod
from .sanity import SOURCE_DB_BRANDED, SOURCE_DB_GENERIC
from .schema import FoodItem

_DATA_DIR = Path(__file__).parent / "data"

# Menu names are short and distinctive, so demand a closer match than for
# generic foods (avoids "french fries" latching onto a specific size).
MENU_THRESHOLD = 0.80
GENERIC_THRESHOLD = 0.78

_STOPWORDS = text_mod.STOPWORDS

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
    "ten", "single", "couple", "few", "toasted", "seasoned", "type",
    "mixed", "marinated", "assorted", "cured",
})

# Per-unit items (pizza slices, tacos): scale the kcal by count when the
# model's gram estimate says there is clearly more than one unit.
_SCALE_MIN_RATIO = 1.6
_MAX_UNITS = 8

# A stated unit_count is only trusted when count x serving_g lands within this
# fraction of the model's own gram estimate (see the pan-pizza regression:
# unit_count=3 visible slices vs grams=whole 630 g pan).
_COUNT_GRAMS_TOLERANCE = 0.45


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


_norm = text_mod.norm
_stem = text_mod.stem
_tokens = text_mod.tokens


def _leftover_allowed(leftover: frozenset, extra_ok: frozenset) -> bool:
    return all(t.isdigit() or t in _MODIFIERS or t in extra_ok for t in leftover)


def _score(
    item_tokens: frozenset,
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
        # bread") must not match.
        return len(item_tokens & b) / len(item_tokens | b)
    # Word-overlap only. Character-level similarity was removed deliberately:
    # it matched "chicken broth" to "chicken burrito" and "beef stew" to
    # "beef steak" - letters lie about food.
    return len(item_tokens & b) / len(item_tokens | b)


def _entry_score(
    item_name: str,
    entry: dict,
    extra_ok: frozenset = frozenset(),
    veto_tokens: Optional[frozenset] = None,
) -> float:
    item_tokens = _tokens(item_name)
    # Item tokens are stemmed, so exclude tokens must be stemmed too or
    # plural excludes ("fries") silently never fire. On retry variants the
    # excludes are checked against the ORIGINAL full name (veto_tokens) -
    # stripping "(banana)" must not un-veto the bread entry.
    guarded = veto_tokens if veto_tokens is not None else item_tokens
    if any(_stem(t) in guarded for t in entry.get("exclude_tokens", [])):
        return 0.0
    loose = bool(entry.get("loose_match"))
    display = entry.get("item") or entry.get("name") or ""
    candidates = [display, *entry.get("aliases", [])]
    return max(
        _score(item_tokens, candidate, loose, extra_ok) for candidate in candidates
    )


@lru_cache(maxsize=1)
def _menu_items() -> tuple[dict, ...]:
    payload = json.loads((_DATA_DIR / "fastfood.json").read_text(encoding="utf-8-sig"))
    return tuple(payload["menu_items"])


@lru_cache(maxsize=1)
def _generic_foods() -> tuple[dict, ...]:
    payload = json.loads((_DATA_DIR / "generic.json").read_text(encoding="utf-8-sig"))
    return tuple(payload["foods"])


def _brand_ok(item: FoodItem, entry: dict) -> bool:
    entry_brand = entry.get("brand")
    if not entry_brand:
        return True
    if entry.get("iconic") and not (item.brand or "").strip():
        # The item NAME is globally unambiguous (Big Mac, Whopper), so a
        # missing brand field is fine - but a stated conflicting brand still
        # blocks below.
        return True
    wanted = _tokens(entry_brand)
    present = _tokens(item.brand or "") | _tokens(item.name)
    return wanted <= present


def _display_name(entry: dict) -> str:
    if entry.get("brand"):
        return f"{entry['brand']} {entry['item']}"
    return entry["item"]


def match_menu_item(
    item: FoodItem, veto_tokens: Optional[frozenset] = None
) -> Optional[Resolution]:
    best: Optional[dict] = None
    best_score = 0.0
    for entry in _menu_items():
        if not _brand_ok(item, entry):
            continue
        # Brand words in the item name ("McDonald's Big Mac") are not a
        # mismatch signal.
        extra_ok = _tokens(entry.get("brand") or "")
        score = _entry_score(item.name, entry, extra_ok, veto_tokens)
        # A brand-specific entry whose brand check passed outranks a brandless
        # entry at the same score ("In-N-Out Cheeseburger" -> the In-N-Out
        # entry, not the generic hamburger).
        if score >= MENU_THRESHOLD and entry.get("brand"):
            score += 0.01
        if score > best_score:
            best, best_score = entry, score
    if best is None or best_score < MENU_THRESHOLD:
        return None

    count = 1
    serving = best.get("serving_g")
    if best.get("unit_scalable") and serving:
        stated = getattr(item, "unit_count", None)
        ratio = item.estimated_grams / serving
        implied = min(_MAX_UNITS, max(1, round(ratio))) if ratio >= _SCALE_MIN_RATIO else 1
        if stated and 1 <= stated <= _MAX_UNITS:
            # Trust the model's count only when its own gram estimate agrees.
            # A stated 3 slices against a whole-pan gram figure means one of
            # the two is wrong; the grams carry the calories, so they win.
            if abs(stated * serving - item.estimated_grams) <= _COUNT_GRAMS_TOLERANCE * item.estimated_grams:
                count = stated
            else:
                count = implied
        else:
            count = implied

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


def match_generic(
    item: FoodItem, veto_tokens: Optional[frozenset] = None
) -> Optional[Resolution]:
    best: Optional[dict] = None
    best_score = 0.0
    for entry in _generic_foods():
        score = _entry_score(item.name, entry, veto_tokens=veto_tokens)
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


# --- retry normalizations (reviewed & approved 2026-08-17) -------------------
# Two retries only. The aggressive subset+density mechanism was red-teamed and
# REJECTED: composed dishes converge to within 1.6x of their own ingredients
# (sandwich~bread, butter crackers~butter), so density cannot certify identity.

_PAREN_RE = re.compile(r"[(\[][^)\]]*[)\]]")
# Words that introduce a garnish/serving clause, not a different food.
_HEAD_RE = re.compile(
    r"\s+(?:with|topped|served|garnished|drizzled|dusted|sprinkled)\s+", re.IGNORECASE
)


@lru_cache(maxsize=1)
def _db_food_vocab() -> frozenset:
    """Every non-modifier token that appears in any DB name or alias."""
    vocab: set[str] = set()
    for entry in _generic_foods():
        for alias in [entry["name"], *entry.get("aliases", [])]:
            vocab.update(t for t in _tokens(alias) if t not in _MODIFIERS)
    for entry in _menu_items():
        for alias in [entry["item"], *entry.get("aliases", [])]:
            vocab.update(t for t in _tokens(alias) if t not in _MODIFIERS)
    return frozenset(vocab)


def _retry_variants(name: str) -> list[str]:
    """Safe alternate spellings of a verbose item name, most conservative first."""
    variants: list[str] = []
    stripped = _PAREN_RE.sub(" ", name).strip()
    if stripped and _tokens(stripped) != _tokens(name):
        variants.append(stripped)

    # Head of a garnish clause: "french fries with seasoning" -> "french
    # fries". Only when the discarded tail is garnish-sized (<=2 core tokens)
    # and names no DB food - "chicken with rice" must NOT collapse to chicken.
    parts = _HEAD_RE.split(stripped or name, maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        head, tail = parts[0].strip(), parts[1]
        tail_core = {
            t for t in _tokens(tail) if t not in _MODIFIERS and not t.isdigit()
        }
        if len(tail_core) <= 2 and not (tail_core & _db_food_vocab()):
            if _tokens(head) not in (_tokens(name), *map(_tokens, variants)):
                variants.append(head)
    return variants


def resolve(item: FoodItem) -> Optional[Resolution]:
    direct = match_menu_item(item) or match_generic(item)
    if direct is not None:
        return direct
    original_tokens = _tokens(item.name)
    for variant in _retry_variants(item.name):
        retry = item.model_copy(update={"name": variant})
        res = match_menu_item(retry, original_tokens) or match_generic(
            retry, original_tokens
        )
        if res is not None:
            return res
    return None


def resolve_all(items: list[FoodItem]) -> list[Optional[Resolution]]:
    return [resolve(item) for item in items]
