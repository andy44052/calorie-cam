"""Phase 2: match vision output against the bundled known-foods database.

Menu items (fastfood.json) carry a total-kcal range for a standardized item, so
a hit replaces the grams x density math entirely. Generic foods (generic.json)
carry kcal/100g, so a hit replaces only the model's energy-density guess while
keeping its portion estimate.
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
GENERIC_THRESHOLD = 0.75

_STOPWORDS = {"a", "an", "the", "of", "with", "and", "on", "in", "style"}

# Per-unit items (pizza slices): scale the kcal by count when the model's gram
# estimate says there is clearly more than one unit on the plate.
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


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_norm(text).split()) - _STOPWORDS


def _score(item_name: str, candidate: str) -> float:
    a, b = _tokens(item_name), _tokens(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if b <= a:
        return 0.95
    jaccard = len(a & b) / len(a | b)
    ratio = SequenceMatcher(None, _norm(item_name), _norm(candidate)).ratio()
    return max(jaccard, ratio)


def _best_score(item_name: str, entry: dict) -> float:
    display = entry.get("item") or entry.get("name") or ""
    candidates = [display, *entry.get("aliases", [])]
    return max(_score(item_name, candidate) for candidate in candidates)


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
        score = _best_score(item.name, entry)
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
        score = _best_score(item.name, entry)
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
