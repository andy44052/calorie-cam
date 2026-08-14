"""Offline collision audit — the red-team as a test, not a $10 API sweep.

The 515-probe adversarial audit that found the "chili fries" and "potato
samosa" class of bug cost real money and an hour of agent time. Its core
question is answerable offline in under a second: does any alias in the
database resolve to an entry whose calories are wildly different from the
entry that owns it?

This runs on every commit, so the "0 wrong matches in 69 runs" invariant
survives the database growing 5-10x.
"""

import json
from pathlib import Path

import pytest

from caloriecam import units
from caloriecam.lookup import _generic_foods, _menu_items, resolve
from caloriecam.schema import FoodItem

# Two entries are "compatible" if their energy densities are within this
# factor. Real collisions are 2.6x (chili/fries) to 3.7x (banana/banana
# bread); legitimate near-neighbours like cheddar/brie sit at 1.3x.
MAX_KCAL_RATIO = 2.0

DATA = Path(__file__).parent.parent / "caloriecam" / "data"


def _probe(name: str, brand: str | None = None, grams: float = 150.0):
    return resolve(
        FoodItem(
            name=name,
            portion_description="audit probe",
            estimated_grams=grams,
            kcal_per_100g=200,
            confidence="medium",
            assumptions=[],
            brand=brand,
        )
    )


def _menu_density(entry: dict) -> float | None:
    """kcal/100g for a menu item, so it can be compared with generic foods."""
    serving = entry.get("serving_g")
    if not serving:
        return None
    return entry["kcal"]["mid"] * 100.0 / serving


def _resolution_density(res, owner_serving: float | None) -> float | None:
    if res is None:
        return None
    if res.kcal_per_100g is not None:
        return res.kcal_per_100g
    if res.kcal_mid is not None and res.serving_g:
        return res.kcal_mid * 100.0 / res.serving_g
    if res.kcal_mid is not None and owner_serving:
        return res.kcal_mid * 100.0 / owner_serving
    return None


def _incompatible(a: float | None, b: float | None) -> bool:
    if a is None or b is None or a <= 0 or b <= 0:
        return False
    return max(a, b) / min(a, b) > MAX_KCAL_RATIO


# --- the audit ---------------------------------------------------------------


def test_no_generic_alias_resolves_to_a_wildly_different_food():
    """Every alias must land on an entry with comparable energy density.

    This is the "banana bread must not weigh in as banana" invariant,
    checked across the whole database instead of one probe at a time.
    """
    failures = []
    for entry in _generic_foods():
        owner_density = entry["kcal_per_100g"]
        for alias in entry["aliases"]:
            res = _probe(alias)
            got = _resolution_density(res, None)
            if _incompatible(owner_density, got):
                failures.append(
                    f"{alias!r} (owned by {entry['name']!r} @ {owner_density} kcal/100g) "
                    f"-> {res.matched_name!r} @ {got:.0f} kcal/100g"
                )
    assert not failures, "alias collisions:\n  " + "\n  ".join(failures)


def test_no_menu_alias_resolves_to_a_wildly_different_item():
    failures = []
    for entry in _menu_items():
        owner_density = _menu_density(entry)
        brand = entry.get("brand")
        for alias in entry["aliases"]:
            res = _probe(alias, brand=brand, grams=entry.get("serving_g") or 200.0)
            got = _resolution_density(res, entry.get("serving_g"))
            if _incompatible(owner_density, got):
                failures.append(
                    f"{alias!r} (owned by {entry['item']!r} @ {owner_density:.0f} kcal/100g) "
                    f"-> {res.matched_name!r} @ {got:.0f} kcal/100g"
                )
    assert not failures, "menu alias collisions:\n  " + "\n  ".join(failures)


def test_no_unit_alias_resolves_to_a_wildly_different_unit():
    """Same invariant for the per-unit weight table.

    A unit-weight collision is just as costly: it multiplies by the count.
    """
    payload = json.loads((DATA / "units.json").read_text(encoding="utf-8-sig"))
    failures = []
    for entry in payload["units"]:
        owner = entry["typical_g"]
        for alias in entry["aliases"]:
            band = units.find_band(alias)
            if band is None:
                continue
            if max(owner, band.typical_g) / min(owner, band.typical_g) > MAX_KCAL_RATIO:
                failures.append(
                    f"{alias!r} (owned by {entry['name']!r} @ {owner} g) "
                    f"-> {band.name!r} @ {band.typical_g} g"
                )
    assert not failures, "unit collisions:\n  " + "\n  ".join(failures)


def test_no_dead_aliases_in_generic_foods():
    """An alias that matches nothing is coverage the author only thinks exists.

    Usually caused by the entry's own exclude_tokens shooting down one of its
    aliases (add "chili fries" to chili con carne, whose exclude list contains
    "fries", and it silently never fires).
    """
    dead = []
    for entry in _generic_foods():
        for alias in entry["aliases"]:
            if _probe(alias) is None:
                dead.append(f"{alias!r} on {entry['name']!r}")
    assert not dead, "aliases that match nothing:\n  " + "\n  ".join(dead)


def test_no_dead_aliases_in_menu_items():
    dead = []
    for entry in _menu_items():
        for alias in entry["aliases"]:
            res = _probe(alias, brand=entry.get("brand"), grams=entry.get("serving_g") or 200.0)
            if res is None:
                dead.append(f"{alias!r} on {entry['item']!r}")
    assert not dead, "menu aliases that match nothing:\n  " + "\n  ".join(dead)


def test_no_dead_aliases_in_units():
    payload = json.loads((DATA / "units.json").read_text(encoding="utf-8-sig"))
    dead = [
        f"{alias!r} on {entry['name']!r}"
        for entry in payload["units"]
        for alias in entry["aliases"]
        if units.find_band(alias) is None
    ]
    assert not dead, "unit aliases that match nothing:\n  " + "\n  ".join(dead)


# --- structural invariants ---------------------------------------------------


def test_no_alias_is_claimed_by_two_entries_with_different_calories():
    """A duplicated alias makes the match depend on file order."""
    owners: dict[str, list[tuple[str, float]]] = {}
    for entry in _generic_foods():
        for alias in entry["aliases"]:
            owners.setdefault(alias, []).append(
                (entry["name"], entry["kcal_per_100g"])
            )
    clashes = [
        f"{alias!r}: " + ", ".join(f"{n} @ {k}" for n, k in claims)
        for alias, claims in owners.items()
        if len(claims) > 1
        and _incompatible(max(k for _, k in claims), min(k for _, k in claims))
    ]
    assert not clashes, "alias claimed by incompatible entries:\n  " + "\n  ".join(clashes)


@pytest.mark.parametrize("filename", ["generic.json", "fastfood.json", "units.json"])
def test_exclude_tokens_are_single_words(filename):
    """Multi-word exclude tokens silently never fire (comparison is per token)."""
    payload = json.loads((DATA / filename).read_text(encoding="utf-8-sig"))
    rows = payload.get("foods") or payload.get("menu_items") or payload.get("units")
    bad = [
        (row.get("name") or row.get("item"), token)
        for row in rows
        for token in row.get("exclude_tokens", [])
        if " " in token
    ]
    assert not bad, f"multi-word exclude tokens never match: {bad}"


@pytest.mark.parametrize("filename", ["generic.json", "fastfood.json", "units.json"])
def test_no_empty_or_duplicate_aliases(filename):
    payload = json.loads((DATA / filename).read_text(encoding="utf-8-sig"))
    rows = payload.get("foods") or payload.get("menu_items") or payload.get("units")
    for row in rows:
        name = row.get("name") or row.get("item")
        aliases = row.get("aliases", [])
        assert aliases, f"{name} has no aliases"
        assert all(a.strip() for a in aliases), f"{name} has a blank alias"
        assert len(aliases) == len(set(aliases)), f"{name} has duplicate aliases"


def test_regression_corpus_still_matches():
    """The specific probes that caught real shipped bugs, as one sweep."""
    known_bugs = [
        # (query, brand, a kcal/100g it must NOT return)
        ("french fries with chili seasoning", None, 120),   # chili con carne
        ("banana bread", None, 89),                          # banana
        ("beef and broccoli", None, 35),                     # broccoli
        ("carrot cake", None, 41),                           # carrot
        ("apple pie", None, 52),                             # apple
        ("tomato soup", None, 18),                           # tomato
        ("chicken fried rice", None, 130),                   # plain rice
        ("cauliflower fried rice", None, 175),               # fried rice
        ("zucchini lasagna", None, 140),                     # lasagna
        ("chicken vegetable soup", None, 110),               # chicken stir-fry
        ("beef stew", None, 270),                            # steak
        ("chicken broth", None, 650),                        # burrito (menu)
        ("egg white omelette", None, 185),                   # cheese omelet
        ("raw oats", None, 71),                              # cooked oatmeal
        ("diet coke", None, 41),                             # regular soda
        ("smoked salmon", None, 205),                        # cooked salmon
        ("sweet potato", None, 280),                         # sweet potato fries
    ]
    failures = []
    for query, brand, forbidden in known_bugs:
        res = _probe(query, brand=brand)
        got = _resolution_density(res, None)
        if got is not None and abs(got - forbidden) < 1:
            failures.append(f"{query!r} regressed to {forbidden} kcal/100g")
    assert not failures, "known bugs came back:\n  " + "\n  ".join(failures)
