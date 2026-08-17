"""Meal history: a local SQLite log of every estimate, used two ways.

1. A diary - daily totals and lifetime spend come straight out of SQL.
2. A personal prior - when the same food shows up again, the fresh portion
   estimate is shrunk toward that person's own median portion, collapsing
   run-to-run noise at zero marginal API cost.

Storage doctrine (matters for correctness):
- ``items`` rows store the PRE-blend, post-clamp estimate - the raw signal.
  Storing blended values would feed each blend into the next until the
  history froze on its own output.
- ``meals`` totals store what the user was actually shown (post-blend),
  because that is what a daily total should sum and what a user's "actually
  it was ~400 kcal" correction refers to.
- Corrections never rewrite the stored estimate. The raw estimate is the
  training signal; the correction is a truth anchor applied at read time.
"""

import os
import sqlite3
import statistics
from pathlib import Path
from typing import Optional

from .text import name_key

_DEFAULT_PATH = Path(__file__).resolve().parent / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meals (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    total_low     INTEGER,
    total_mid     INTEGER,
    total_high    INTEGER,
    corrected_mid INTEGER,
    hint          TEXT,
    debate_ran    INTEGER,
    model         TEXT,
    cost_usd      REAL
);
CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY,
    meal_id        INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    name_raw       TEXT NOT NULL,
    name_norm      TEXT NOT NULL,
    brand          TEXT,
    grams          REAL,
    per_unit_grams REAL,
    unit_count     INTEGER,
    kcal_per_100g  REAL,
    kcal_low       REAL,
    kcal_mid       REAL,
    kcal_high      REAL,
    source         TEXT,
    confidence     TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_norm ON items(name_norm);
CREATE INDEX IF NOT EXISTS idx_meals_ts   ON meals(ts);
"""

# Shrinkage strength: weight on history = n / (n + K).
#   2 past meals -> 50%,  5 -> 71%,  10 -> 83%
BLEND_K = 2.0

# Blending needs at least this many past sightings of the food; a single prior
# meal is an anecdote, not a prior.
BLEND_MIN_PAST = 2

# Only the most recent sightings count - eating habits drift.
BLEND_WINDOW = 10


def blend_grams(new_grams: float, past: list[float], k: float = BLEND_K) -> float:
    """Shrink a fresh portion estimate toward the personal median."""
    if len(past) < BLEND_MIN_PAST:
        return new_grams
    med = statistics.median(past)
    w = len(past) / (len(past) + k)
    return w * med + (1.0 - w) * new_grams


class HistoryStore:
    """One SQLite file. Every method opens its own short-lived connection, so
    the store is safe to share across FastAPI worker threads."""

    def __init__(self, path: str | Path = _DEFAULT_PATH):
        self.path = Path(path)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # --- writing -------------------------------------------------------------

    def record(
        self,
        meal,
        analysis,
        model: str,
        hint: Optional[str] = None,
    ) -> int:
        """Store one finished estimate; returns the meal id.

        Called after the pipeline succeeds - every field is already in
        meal/analysis, zero extra API calls.
        """
        usage = meal.usage or {}
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO meals (total_low, total_mid, total_high, hint,"
                " debate_ran, model, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    meal.total_low,
                    meal.total_mid,
                    meal.total_high,
                    hint,
                    1 if meal.debate is not None else 0,
                    model,
                    usage.get("total_cost_usd"),
                ),
            )
            meal_id = cur.lastrowid
            for est, raw in zip(meal.items, analysis.items):
                # grams_raw is set when blending changed this item; store the
                # pre-blend value so the history never trains on itself.
                grams = est.grams_raw if est.grams_raw is not None else est.grams
                unblend = (
                    grams / est.grams if est.grams and est.grams_raw is not None else 1.0
                )
                conn.execute(
                    "INSERT INTO items (meal_id, name_raw, name_norm, brand, grams,"
                    " per_unit_grams, unit_count, kcal_per_100g, kcal_low, kcal_mid,"
                    " kcal_high, source, confidence)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        meal_id,
                        est.name,
                        name_key(est.name),
                        est.brand,
                        grams,
                        raw.per_unit_grams,
                        raw.unit_count,
                        est.kcal_per_100g,
                        est.kcal_low * unblend,
                        est.kcal_mid * unblend,
                        est.kcal_high * unblend,
                        est.source,
                        est.confidence,
                    ),
                )
        return meal_id

    def correct(self, meal_id: int, corrected_mid: int) -> bool:
        """Store the user's post-meal correction. Returns False on unknown id."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE meals SET corrected_mid = ? WHERE id = ?",
                (corrected_mid, meal_id),
            )
            return cur.rowcount > 0

    # --- reading -------------------------------------------------------------

    # A user correction rescales that meal's portions, but no honest portion
    # correction is 20x - a typo ("4000" for "400") must not poison the prior.
    _CORRECTION_RATIO_MIN, _CORRECTION_RATIO_MAX = 0.2, 5.0

    def past_portions(self, name: str, limit: int = BLEND_WINDOW) -> list[dict]:
        """Recent (grams, kcal_per_100g) for this food, most recent first.

        Corrections: "the total was really 400, not 500" implies that meal's
        portions ran ~20% hot, so its grams are scaled by the correction ratio
        at read time. The denominator is the meal's PRE-blend kcal total
        (SUM of stored item kcal, which record() keeps unblended) - the stored
        grams are pre-blend, so the ratio must be measured in the same frame.
        Dividing by the post-blend shown total would leak the blend's own
        output back into the training signal.
        """
        key = name_key(name)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.grams, i.kcal_per_100g, m.corrected_mid, m.total_mid,"
                " (SELECT SUM(kcal_mid) FROM items x WHERE x.meal_id = m.id)"
                " FROM items i JOIN meals m ON m.id = i.meal_id"
                " WHERE i.name_norm = ? AND i.grams IS NOT NULL AND i.grams > 0"
                " ORDER BY m.ts DESC, i.id DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        out = []
        for grams, kcal100, corrected, total_shown, total_raw in rows:
            denom = total_raw or total_shown
            if corrected and denom:
                ratio = max(
                    self._CORRECTION_RATIO_MIN,
                    min(self._CORRECTION_RATIO_MAX, corrected / denom),
                )
                grams = grams * ratio
            out.append({"grams": float(grams), "kcal_per_100g": kcal100})
        return out

    def past_grams(self, name: str, limit: int = BLEND_WINDOW) -> list[float]:
        return [row["grams"] for row in self.past_portions(name, limit)]

    def today_total(self) -> dict:
        """Sum of today's meals, preferring the user's corrections."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(corrected_mid, total_mid)), 0)"
                " FROM meals WHERE date(ts) = date('now', 'localtime')"
            ).fetchone()
        return {"meals": row[0], "kcal": int(row[1])}

    def daily_totals(self, days: int = 7) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date(ts), COUNT(*),"
                " SUM(COALESCE(corrected_mid, total_mid))"
                " FROM meals GROUP BY date(ts) ORDER BY date(ts) DESC LIMIT ?",
                (days,),
            ).fetchall()
        return [{"date": d, "meals": n, "kcal": int(k or 0)} for d, n, k in rows]

    def lifetime_cost_usd(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM meals").fetchone()
        return round(row[0], 4)


def default_store() -> Optional[HistoryStore]:
    """The store the app should use, honoring CALORIECAM_HISTORY.

    - unset          -> caloriecam/history.db (the normal case)
    - "off"/0/false  -> None (history disabled entirely)
    - any other value-> treated as a path (tests, alternate profiles)
    """
    setting = os.environ.get("CALORIECAM_HISTORY", "").strip()
    if setting.lower() in {"off", "0", "false", "no"}:
        return None
    if setting.lower() in {"on", "1", "true", "yes"}:
        # Someone mirroring CALORIECAM_DEBATE=on must not get a database in a
        # file literally named "on".
        setting = ""
    return HistoryStore(setting or _DEFAULT_PATH)
