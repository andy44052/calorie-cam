import json

import pytest

import estimate
from caloriecam import sanity


@pytest.fixture
def fake_meal(sample_analysis):
    return sanity.evaluate(sample_analysis)


def test_missing_file_exit_code(capsys, tmp_path):
    rc = estimate.main([str(tmp_path / "nope.jpg")])
    assert rc == 1
    assert "file not found" in capsys.readouterr().err


def test_missing_api_key_is_friendly(monkeypatch, capsys, photo_path):
    def boom(*args, **kwargs):
        raise TypeError(
            '"Could not resolve authentication method. Expected one of api_key, '
            'auth_token, or credentials to be set."'
        )

    monkeypatch.setattr(estimate.pipeline, "run", boom)
    rc = estimate.main([str(photo_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no Anthropic API key found" in err
    assert "Traceback" not in err


def test_unrelated_typeerror_still_raises(monkeypatch, photo_path):
    def boom(*args, **kwargs):
        raise TypeError("something else entirely")

    monkeypatch.setattr(estimate.pipeline, "run", boom)
    with pytest.raises(TypeError, match="something else"):
        estimate.main([str(photo_path)])


def test_happy_path_text(monkeypatch, capsys, photo_path, fake_meal, sample_analysis):
    monkeypatch.setattr(
        estimate.pipeline, "run", lambda *args, **kwargs: (fake_meal, sample_analysis)
    )
    rc = estimate.main([str(photo_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TOTAL" in out
    assert "grilled chicken breast" in out


def test_happy_path_json(monkeypatch, capsys, photo_path, fake_meal, sample_analysis):
    monkeypatch.setattr(
        estimate.pipeline, "run", lambda *args, **kwargs: (fake_meal, sample_analysis)
    )
    rc = estimate.main([str(photo_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_kcal"]["mid"] == 508
    assert len(payload["items"]) == 2
