import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as webapp
from caloriecam.sanity import evaluate
from caloriecam.vision import RefusalError, VisionError


@pytest.fixture
def web_client():
    return TestClient(webapp.app)


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (600, 400), (150, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


def _post_photo(web_client, content: bytes, filename="meal.jpg", mime="image/jpeg"):
    return web_client.post(
        "/api/estimate", files={"photo": (filename, content, mime)}
    )


def test_health_endpoint(web_client):
    resp = web_client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": webapp.__version__}


def test_index_serves_page(web_client):
    resp = web_client.get("/")
    assert resp.status_code == 200
    assert "CalorieCam" in resp.text
    assert "photo-input" in resp.text


def test_estimate_happy_path(monkeypatch, web_client, sample_analysis):
    meal = evaluate(sample_analysis)
    monkeypatch.setattr(
        webapp.pipeline, "run_bytes", lambda data, **kw: (meal, sample_analysis)
    )
    resp = _post_photo(web_client, _jpeg_bytes())
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total_kcal"] == {"low": 392, "mid": 508, "high": 623}
    assert payload["image"] == "meal.jpg"
    assert len(payload["items"]) == 2


def test_missing_file_is_422(web_client):
    assert web_client.post("/api/estimate").status_code == 422


def test_empty_upload_rejected(web_client):
    resp = _post_photo(web_client, b"")
    assert resp.status_code == 400


def test_non_image_rejected_before_api_call(web_client):
    # No mocking: PIL rejects the bytes before any API client is built.
    resp = _post_photo(web_client, b"this is not an image", filename="notes.txt", mime="text/plain")
    assert resp.status_code == 400
    assert "not a readable image" in resp.json()["detail"]


def test_oversized_upload_rejected(web_client):
    resp = _post_photo(web_client, b"x" * (webapp.MAX_UPLOAD_BYTES + 1))
    assert resp.status_code == 413


def test_refusal_maps_to_422(monkeypatch, web_client):
    def boom(data, **kw):
        raise RefusalError("the API declined to analyze this image")

    monkeypatch.setattr(webapp.pipeline, "run_bytes", boom)
    resp = _post_photo(web_client, _jpeg_bytes())
    assert resp.status_code == 422
    assert "declined" in resp.json()["detail"]


def test_vision_error_maps_to_502(monkeypatch, web_client):
    def boom(data, **kw):
        raise VisionError("model returned no parseable analysis")

    monkeypatch.setattr(webapp.pipeline, "run_bytes", boom)
    resp = _post_photo(web_client, _jpeg_bytes())
    assert resp.status_code == 502


def test_hint_form_field_reaches_pipeline(monkeypatch, web_client, sample_analysis):
    captured = {}

    def fake_run(data, **kwargs):
        captured.update(kwargs)
        return (evaluate(sample_analysis), sample_analysis)

    monkeypatch.setattr(webapp.pipeline, "run_bytes", fake_run)
    resp = web_client.post(
        "/api/estimate",
        files={"photo": ("meal.jpg", _jpeg_bytes(), "image/jpeg")},
        data={"hint": "cooked in olive oil, all organic"},
    )
    assert resp.status_code == 200
    assert captured["hint"] == "cooked in olive oil, all organic"


def test_missing_hint_becomes_none(monkeypatch, web_client, sample_analysis):
    captured = {}

    def fake_run(data, **kwargs):
        captured.update(kwargs)
        return (evaluate(sample_analysis), sample_analysis)

    monkeypatch.setattr(webapp.pipeline, "run_bytes", fake_run)
    resp = _post_photo(web_client, _jpeg_bytes())
    assert resp.status_code == 200
    assert captured["hint"] is None


def test_pin_gate_blocks_without_pin(monkeypatch, web_client):
    monkeypatch.setenv("CALORIECAM_PIN", "4321")
    resp = _post_photo(web_client, _jpeg_bytes())
    assert resp.status_code == 401
    assert "PIN" in resp.json()["detail"]


def test_pin_gate_blocks_wrong_pin(monkeypatch, web_client):
    monkeypatch.setenv("CALORIECAM_PIN", "4321")
    resp = web_client.post(
        "/api/estimate",
        files={"photo": ("meal.jpg", _jpeg_bytes(), "image/jpeg")},
        headers={"X-CalorieCam-Pin": "9999"},
    )
    assert resp.status_code == 401


def test_pin_gate_allows_correct_pin(monkeypatch, web_client, sample_analysis):
    monkeypatch.setenv("CALORIECAM_PIN", "4321")
    meal = evaluate(sample_analysis)
    monkeypatch.setattr(
        webapp.pipeline, "run_bytes", lambda data, **kw: (meal, sample_analysis)
    )
    resp = web_client.post(
        "/api/estimate",
        files={"photo": ("meal.jpg", _jpeg_bytes(), "image/jpeg")},
        headers={"X-CalorieCam-Pin": "4321"},
    )
    assert resp.status_code == 200
    assert resp.json()["total_kcal"]["mid"] == 508


def test_no_pin_env_means_open_access(monkeypatch, web_client, sample_analysis):
    monkeypatch.delenv("CALORIECAM_PIN", raising=False)
    meal = evaluate(sample_analysis)
    monkeypatch.setattr(
        webapp.pipeline, "run_bytes", lambda data, **kw: (meal, sample_analysis)
    )
    resp = _post_photo(web_client, _jpeg_bytes())
    assert resp.status_code == 200


def test_missing_key_maps_to_500(monkeypatch, web_client):
    def boom(data, **kw):
        raise TypeError('"Could not resolve authentication method."')

    monkeypatch.setattr(webapp.pipeline, "run_bytes", boom)
    resp = _post_photo(web_client, _jpeg_bytes())
    assert resp.status_code == 500
    assert "API key" in resp.json()["detail"]
