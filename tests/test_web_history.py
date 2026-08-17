"""Web endpoints for meal history: meal_id, corrections, daily totals."""

import io

import pytest
from fastapi.testclient import TestClient

import app as webapp
from caloriecam import lookup
from caloriecam.sanity import evaluate


@pytest.fixture
def web_client():
    return TestClient(webapp.app)


@pytest.fixture
def fake_pipeline(monkeypatch, sample_analysis):
    """Route run_bytes to a canned result and hand back the recording store."""
    def fake_run(data, **kwargs):
        meal = evaluate(sample_analysis, lookup.resolve_all(sample_analysis.items))
        meal.usage = {"total_cost_usd": 0.20}
        return meal, sample_analysis

    monkeypatch.setattr(webapp.pipeline, "run_bytes", fake_run)
    return webapp._history


def _post_photo(client, **kwargs):
    return client.post(
        "/api/estimate",
        files={"photo": ("meal.jpg", io.BytesIO(b"fake-image-bytes"), "image/jpeg")},
        **kwargs,
    )


def test_estimate_returns_meal_id_and_today(web_client, fake_pipeline):
    body = _post_photo(web_client).json()
    assert body["meal_id"] == 1
    assert body["today"]["meals"] == 1
    assert body["today"]["kcal"] == body["total_kcal"]["mid"]


def test_two_meals_accumulate_in_today(web_client, fake_pipeline):
    _post_photo(web_client)
    body = _post_photo(web_client).json()
    assert body["meal_id"] == 2
    assert body["today"]["meals"] == 2


def test_correction_round_trip(web_client, fake_pipeline):
    meal_id = _post_photo(web_client).json()["meal_id"]
    res = web_client.post(f"/api/meals/{meal_id}/correct", json={"kcal": 400})
    assert res.status_code == 200
    assert res.json()["today"]["kcal"] == 400  # corrections win in the daily sum
    assert fake_pipeline.past_grams("white rice")  # row still present, unrewritten


def test_correction_unknown_meal_404(web_client, fake_pipeline):
    assert web_client.post("/api/meals/99/correct", json={"kcal": 400}).status_code == 404


def test_correction_rejects_nonpositive_kcal(web_client, fake_pipeline):
    meal_id = _post_photo(web_client).json()["meal_id"]
    assert web_client.post(f"/api/meals/{meal_id}/correct", json={"kcal": 0}).status_code == 422


def test_correction_respects_pin(monkeypatch, web_client, fake_pipeline):
    monkeypatch.setenv("CALORIECAM_PIN", "4321")
    res = web_client.post("/api/meals/1/correct", json={"kcal": 400})
    assert res.status_code == 401
    res = web_client.post(
        "/api/meals/1/correct", json={"kcal": 400}, headers={"X-CalorieCam-Pin": "4321"}
    )
    assert res.status_code in (200, 404)  # authorized (404 only if no meal yet)


def test_today_and_daily_endpoints(web_client, fake_pipeline):
    _post_photo(web_client)
    today = web_client.get("/api/history/today").json()
    assert today["meals"] == 1
    daily = web_client.get("/api/history/daily").json()
    assert daily["days"][0]["meals"] == 1
    assert daily["lifetime_cost_usd"] == pytest.approx(0.20)


def test_history_disabled_still_estimates(monkeypatch, web_client, fake_pipeline):
    monkeypatch.setattr(webapp, "_history", None)
    body = _post_photo(web_client).json()
    assert "meal_id" not in body
    assert body["total_kcal"]["mid"] > 0
    assert web_client.get("/api/history/today").status_code == 404


def test_no_food_photo_is_not_recorded_as_a_meal(monkeypatch, web_client, empty_analysis):
    def fake_run(data, **kwargs):
        meal = evaluate(empty_analysis, [])
        meal.usage = {"total_cost_usd": 0.05}
        return meal, empty_analysis

    monkeypatch.setattr(webapp.pipeline, "run_bytes", fake_run)
    body = _post_photo(web_client).json()
    assert "meal_id" not in body  # a keyboard photo is not a meal
    assert webapp._history.today_total() == {"meals": 0, "kcal": 0}


def test_history_write_failure_degrades_instead_of_500(monkeypatch, web_client, fake_pipeline):
    import sqlite3

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(webapp._history, "record", boom)
    res = _post_photo(web_client)
    assert res.status_code == 200  # the paid estimate survives
    body = res.json()
    assert body["total_kcal"]["mid"] > 0
    assert "meal_id" not in body


def test_overlong_hint_is_truncated_before_storage(web_client, fake_pipeline):
    res = web_client.post(
        "/api/estimate",
        files={"photo": ("meal.jpg", io.BytesIO(b"fake-image-bytes"), "image/jpeg")},
        data={"hint": "x" * 5000},
    )
    assert res.status_code == 200
    with fake_pipeline._connect() as conn:
        stored = conn.execute("SELECT hint FROM meals").fetchone()[0]
    assert len(stored) == 500  # HINT_MAX_CHARS - matches what the model saw


def test_non_ascii_pin_header_is_401_not_500(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("CALORIECAM_PIN", "4321")
    with pytest.raises(HTTPException) as exc:
        webapp._check_pin("\xfc\xfc\xfc\xfc")  # latin-1 decoded garbage byte
    assert exc.value.status_code == 401


def test_index_is_served_with_no_cache(web_client):
    res = web_client.get("/")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"
