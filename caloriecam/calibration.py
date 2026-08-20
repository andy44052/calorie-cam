"""Per-source calibration: multiply out systematic bias, measured not guessed.

The pipeline stacks several deliberate upward nudges (prompt sizing rules, the
skeptic's challenges, unit-weight clamps). Their combined residual is a small
systematic bias that drifts as the system evolves - Run A measured +2% overall.
Calibration corrects it with per-source multiplicative factors FITTED on the
user's own verified meals (weighed or label-known, marked via the correction
box), never hand-tuned.

The factors live in ``caloriecam/data/calibration.json``; when the file is
absent (the default), every factor is 1.0 and this module is a no-op. Fit
with ``python calibrate.py fit`` - it refuses to emit factors from fewer than
MIN_PAIRS verified meals and clamps everything to a modest band, because a
calibration layer must never become a second source of error.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

_DEFAULT_PATH = Path(__file__).parent / "data" / "calibration.json"

# An honest global bias correction is a few percent; anything outside this
# band means the fit is wrong (too few meals, a mislabeled truth), not the app.
FACTOR_MIN, FACTOR_MAX = 0.85, 1.15

# Fewer verified meals than this and the fit is an anecdote.
MIN_PAIRS = 10


def _path() -> Path:
    override = os.environ.get("CALORIECAM_CALIBRATION", "").strip()
    return Path(override) if override else _DEFAULT_PATH


@lru_cache(maxsize=1)
def _load() -> dict:
    path = _path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def reload() -> None:
    """Drop the cache (tests, and after `calibrate.py fit` writes new factors)."""
    _load.cache_clear()


def factor_for(source: str) -> float:
    raw = (_load().get("factors") or {}).get(source, 1.0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return max(FACTOR_MIN, min(FACTOR_MAX, value))


def provenance() -> str:
    """Short human-readable origin for assumption notes."""
    meta = _load()
    n = meta.get("fitted_on_meals")
    date = meta.get("fitted_at", "")[:10]
    if n:
        return f"fitted on {n} verified meals{f' ({date})' if date else ''}"
    return "unfitted"
