"""Truth capture (#7) and calibration (#5): gold labels in, fitted factors out,
and the guard rails that keep calibration from becoming a second error source."""

import json
import sqlite3

import pytest

import calibrate
from caloriecam import calibration
from caloriecam.history import HistoryStore
from caloriecam.sanity import evaluate
from caloriecam.schema import FoodAnalysis, FoodItem


def _item(name="pasta with tomato sauce", grams=300.0, kcal100=100.0):
    return FoodItem(
        name=name, portion_description="plate", estimated_grams=grams,
        kcal_per_100g=kcal100, confidence="medium", assumptions=[], brand=None,
    )


@pytest.fixture
def store(tmp_path):
    return HistoryStore(tmp_path / "h.db")


@pytest.fixture
def cal_file(tmp_path, monkeypatch):
    path = tmp_path / "calibration.json"
    monkeypatch.setenv("CALORIECAM_CALIBRATION", str(path))
    calibration.reload()
    yield path
    calibration.reload()


def _write_factors(path, factors, n=12):
    path.write_text(json.dumps(
        {"factors": factors, "fitted_on_meals": n, "fitted_at": "2026-08-18T00:00:00+00:00"}
    ), encoding="utf-8")
    calibration.reload()


# --- truth capture (#7) --------------------------------------------------------


def test_verified_correction_roundtrip(store):
    a = FoodAnalysis(items=[_item()], scale_reference="plate")
    meal = evaluate(a, [None])
    mid = store.record(meal, a, model="m")
    assert store.correct(mid, 290, verified=True)
    pairs = store.verified_truths()
    assert len(pairs) == 1
    assert pairs[0]["truth"] == 290
    assert pairs[0]["by_source"]["model_estimate"] == pytest.approx(300.0)


def test_unverified_corrections_are_not_gold(store):
    a = FoodAnalysis(items=[_item()], scale_reference="plate")
    meal = evaluate(a, [None])
    mid = store.record(meal, a, model="m")
    store.correct(mid, 250)  # eyeball opinion, default unverified
    assert store.verified_truths() == []


def test_legacy_database_is_migrated(tmp_path):
    """A DB created before the verified/cal_factor columns opens and works."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE meals (id INTEGER PRIMARY KEY, ts TEXT NOT NULL DEFAULT '2026-01-01T00:00:00',"
        " total_low INTEGER, total_mid INTEGER, total_high INTEGER, corrected_mid INTEGER,"
        " hint TEXT, debate_ran INTEGER, model TEXT, cost_usd REAL);"
        "CREATE TABLE items (id INTEGER PRIMARY KEY, meal_id INTEGER NOT NULL,"
        " name_raw TEXT NOT NULL, name_norm TEXT NOT NULL, brand TEXT, grams REAL,"
        " per_unit_grams REAL, unit_count INTEGER, kcal_per_100g REAL, kcal_low REAL,"
        " kcal_mid REAL, kcal_high REAL, source TEXT, confidence TEXT);"
        "INSERT INTO meals (total_low, total_mid, total_high) VALUES (90, 100, 110);"
        "INSERT INTO items (meal_id, name_raw, name_norm, grams, kcal_mid, source)"
        " VALUES (1, 'apple', 'apple', 180, 100, 'database_generic');"
    )
    conn.commit()
    conn.close()

    migrated = HistoryStore(db)  # must not raise
    assert migrated.correct(1, 95, verified=True)
    assert migrated.verified_truths()[0]["truth"] == 95


# --- calibration application (#5) ----------------------------------------------


def test_no_calibration_file_is_a_noop(cal_file):
    meal = evaluate(FoodAnalysis(items=[_item()], scale_reference="p"), [None])
    assert meal.items[0].cal_factor == 1.0
    assert meal.total_mid == 300
    assert not any("calibrated" in a for a in meal.items[0].assumptions)


def test_factors_scale_kcal_and_leave_a_note(cal_file):
    _write_factors(cal_file, {"model_estimate": 0.90})
    meal = evaluate(FoodAnalysis(items=[_item()], scale_reference="p"), [None])
    assert meal.items[0].cal_factor == 0.90
    assert meal.total_mid == 270
    assert any("calibrated x0.90" in a for a in meal.items[0].assumptions)


def test_absurd_factors_are_clamped(cal_file):
    _write_factors(cal_file, {"model_estimate": 0.4})
    meal = evaluate(FoodAnalysis(items=[_item()], scale_reference="p"), [None])
    assert meal.items[0].cal_factor == calibration.FACTOR_MIN


def test_history_stores_uncalibrated_kcal_via_cal_factor(cal_file, store):
    """Refits must see raw estimates - never their own previous output."""
    _write_factors(cal_file, {"model_estimate": 0.90})
    a = FoodAnalysis(items=[_item()], scale_reference="p")
    meal = evaluate(a, [None])
    mid = store.record(meal, a, model="m")
    store.correct(mid, 280, verified=True)
    (pair,) = store.verified_truths()
    # Shown total was 270 (calibrated); the fit input must be the raw 300.
    assert pair["by_source"]["model_estimate"] == pytest.approx(300.0)


# --- the fit (#5) ----------------------------------------------------------------


def _seed_gold(store, n, est_kcal, truth_kcal):
    for _ in range(n):
        a = FoodAnalysis(items=[_item(kcal100=est_kcal / 3.0)], scale_reference="p")
        meal = evaluate(a, [None])
        mid = store.record(meal, a, model="m")
        store.correct(mid, truth_kcal, verified=True)


def test_fit_refuses_below_min_pairs(store, cal_file, monkeypatch, capsys):
    monkeypatch.setattr(calibrate.history, "default_store", lambda: store)
    _seed_gold(store, calibration.MIN_PAIRS - 1, est_kcal=330, truth_kcal=300)
    assert calibrate.main(["fit"]) == 1
    assert "need 10" in capsys.readouterr().out


def test_fit_recovers_a_systematic_overcount(store, cal_file, monkeypatch, capsys):
    monkeypatch.setattr(calibrate.history, "default_store", lambda: store)
    # 12 meals, all estimated 330 when the measured truth was 300 (+10% bias).
    _seed_gold(store, 12, est_kcal=330, truth_kcal=300)
    assert calibrate.main(["fit", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "fitted on 12 verified meals" in out
    # Ridge shrinks toward 1.0, so the factor lands between the raw 0.909 and 1.
    assert "model_estimate       x0.9" in out
    assert "(dry run - nothing written)" in out


def test_fit_leaves_unseen_sources_alone(store, cal_file, monkeypatch, capsys):
    monkeypatch.setattr(calibrate.history, "default_store", lambda: store)
    _seed_gold(store, 12, est_kcal=330, truth_kcal=300)  # model_estimate only
    calibrate.main(["fit", "--dry-run"])
    out = capsys.readouterr().out
    assert "database_generic     x1.000" in out
    assert "database_branded     x1.000" in out
