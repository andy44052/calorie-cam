"""Layer 3: turn a MealEstimate into CLI text or JSON-friendly dicts.

ASCII-only output so it renders on any Windows console codepage.
"""

from .sanity import SOURCE_MODEL, ItemEstimate, MealEstimate

SEP = "-" * 66
NAME_MAX = 30


def _tag(item: ItemEstimate) -> str:
    if item.source != SOURCE_MODEL:
        return f"[{item.confidence}, db]"
    return f"[{item.confidence}]"


def _display_name(item: ItemEstimate) -> str:
    name = item.name
    if item.brand and item.brand.lower() not in name.lower():
        name = f"{item.brand} {name}"
    if len(name) > NAME_MAX:
        name = name[: NAME_MAX - 3] + "..."
    return name


def to_text(meal: MealEstimate, source: str) -> str:
    lines = [f"CalorieCam  {source}", SEP]
    if not meal.items:
        lines.append("No food or drink detected.")
    else:
        width = max(len(_display_name(i)) for i in meal.items)
        width = max(width, len("TOTAL"))
        for item in meal.items:
            lines.append(
                f"{_display_name(item):<{width}}  {item.grams:>5d} g  "
                f"~{item.kcal_mid:>4d} kcal  ({item.kcal_low}-{item.kcal_high})  "
                f"{_tag(item)}"
            )
        lines.append(SEP)
        lines.append(
            f"{'TOTAL':<{width}}           ~{meal.total_mid:>4d} kcal  "
            f"({meal.total_low}-{meal.total_high})"
        )
    if meal.debate and meal.debate.get("challenges"):
        rulings = meal.debate.get("rulings", [])
        corrections = sum(
            1 for r in rulings if r.get("verdict") in ("accepted", "partially_accepted")
        )
        lines.append(
            f"debate: {len(meal.debate['challenges'])} challenge(s) raised - "
            f"{corrections} led to corrections, {len(rulings) - corrections} rejected"
        )
    if meal.scale_reference:
        lines.append(f"scale reference: {meal.scale_reference}")
    if meal.notes:
        lines.append(f"notes: {meal.notes}")
    return "\n".join(lines)


def to_dict(meal: MealEstimate, source: str) -> dict:
    return {
        "image": source,
        "total_kcal": {
            "low": meal.total_low,
            "mid": meal.total_mid,
            "high": meal.total_high,
        },
        "items": [
            {
                "name": item.name,
                "brand": item.brand,
                "grams": item.grams,
                "kcal_per_100g": item.kcal_per_100g,
                "kcal": {
                    "low": item.kcal_low,
                    "mid": item.kcal_mid,
                    "high": item.kcal_high,
                },
                "confidence": item.confidence,
                "source": item.source,
                "assumptions": item.assumptions,
            }
            for item in meal.items
        ],
        "scale_reference": meal.scale_reference,
        "notes": meal.notes,
        "debate": meal.debate,
        "usage": meal.usage,
    }
