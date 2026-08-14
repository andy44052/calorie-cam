"""Per-unit weight bands for countable foods.

The measured failure mode this attacks: counts are stable across runs but
per-unit weight guesses are not (lemon bars counted 15/15/15 every time while
the implied weight swung 40-53 g/bar, a 33% spread in the total).

Philosophy matches the rest of the sanity layer - clamp into a plausible band,
never override with a point value. If the model's per-unit weight is defensible
we keep it; only clearly-out-of-band guesses get pulled to the edge.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .text import MODIFIERS
from .text import tokens as _tokens

_DATA = Path(__file__).parent / "data" / "units.json"

# Colour and varietal words change what a food IS for calorie purposes but not
# what one unit WEIGHS, so the unit matcher tolerates them where the food
# matcher would not. A red grape and a green grape weigh the same.
_UNIT_MODIFIERS = MODIFIERS | {
    "red", "green", "black", "white", "purple", "yellow", "golden", "brown",
    "wine", "table", "seedless", "pitted", "organic", "ripe",
}


@dataclass
class UnitBand:
    name: str
    low_g: float
    typical_g: float
    high_g: float

    def clamp(self, grams: float) -> float:
        return max(self.low_g, min(self.high_g, grams))


@lru_cache(maxsize=1)
def _bands() -> tuple[tuple[frozenset, UnitBand], ...]:
    if not _DATA.exists():
        return ()
    payload = json.loads(_DATA.read_text(encoding="utf-8"))
    out = []
    for entry in payload["units"]:
        band = UnitBand(
            name=entry["name"],
            low_g=float(entry["low_g"]),
            typical_g=float(entry["typical_g"]),
            high_g=float(entry["high_g"]),
        )
        for alias in [entry["name"], *entry.get("aliases", [])]:
            out.append((_tokens(alias), band))
    # Longest alias first so "salmon nigiri" beats "nigiri".
    out.sort(key=lambda pair: len(pair[0]), reverse=True)
    return tuple(out)


def find_band(food_name: str) -> Optional[UnitBand]:
    """Best unit-weight band for a food name, or None.

    An alias matches when its tokens appear in the food name AND the leftover
    words are harmless modifiers. Without the leftover rule an ingredient row
    hijacks any dish containing it - "potato samosa" would weigh a whole
    potato, the same failure the food matcher hit with "chili fries".
    Longest alias wins, so "baby carrot" beats "carrot".
    """
    item_tokens = _tokens(food_name)
    if not item_tokens:
        return None
    for alias_tokens, band in _bands():
        if not alias_tokens or not alias_tokens <= item_tokens:
            continue
        leftover = item_tokens - alias_tokens
        if all(t.isdigit() or t in _UNIT_MODIFIERS for t in leftover):
            return band
    return None
