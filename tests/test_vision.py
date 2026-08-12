import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from caloriecam import vision
from caloriecam.config import HINT_MAX_CHARS, MAX_TOKENS
from caloriecam.schema import FoodAnalysis
from caloriecam.vision import (
    RefusalError,
    VisionError,
    analyze_image,
    build_messages,
    prepare_image,
)


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def _ok_response(analysis):
    return SimpleNamespace(parsed_output=analysis, stop_reason="end_turn")


# --- prepare_image -----------------------------------------------------------


def test_large_image_downscaled(photo_path):
    b64, media_type = prepare_image(photo_path, max_px=1568)
    raw = base64.standard_b64decode(b64)
    assert media_type == "image/jpeg"
    assert raw[:2] == b"\xff\xd8"  # JPEG magic bytes
    img = Image.open(io.BytesIO(raw))
    assert max(img.size) == 1568
    assert img.size[0] / img.size[1] == pytest.approx(2400 / 1600, rel=0.01)


def test_small_image_not_upscaled(tmp_path):
    path = tmp_path / "small.jpg"
    Image.new("RGB", (400, 300), (10, 20, 30)).save(path, "JPEG")
    b64, _ = prepare_image(path, max_px=1568)
    img = Image.open(io.BytesIO(base64.standard_b64decode(b64)))
    assert img.size == (400, 300)


def test_png_with_alpha_converted(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGBA", (500, 500), (10, 20, 30, 128)).save(path, "PNG")
    b64, media_type = prepare_image(path)
    assert media_type == "image/jpeg"
    img = Image.open(io.BytesIO(base64.standard_b64decode(b64)))
    assert img.mode == "RGB"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        prepare_image(tmp_path / "nope.jpg")


def test_hint_appended_to_user_text():
    messages = build_messages("QUJD", "image/jpeg", hint="cooked in olive oil")
    text = messages[0]["content"][1]["text"]
    assert text.startswith(vision.USER_PROMPT)
    assert "cooked in olive oil" in text


def test_no_hint_leaves_prompt_unchanged():
    messages = build_messages("QUJD", "image/jpeg")
    assert messages[0]["content"][1]["text"] == vision.USER_PROMPT


def test_hint_passed_through_and_truncated(photo_path, sample_analysis):
    client = FakeClient(_ok_response(sample_analysis))
    analyze_image(photo_path, client=client, hint="x" * 900)
    (call,) = client.messages.calls
    text = call["messages"][0]["content"][1]["text"]
    assert "x" * HINT_MAX_CHARS in text
    assert "x" * (HINT_MAX_CHARS + 1) not in text


# --- build_messages ----------------------------------------------------------


def test_message_structure():
    messages = build_messages("QUJD", "image/jpeg")
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    image_block, text_block = messages[0]["content"]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/jpeg"
    assert image_block["source"]["data"] == "QUJD"
    assert text_block["type"] == "text"
    assert text_block["text"] == vision.USER_PROMPT


# --- analyze_image -----------------------------------------------------------


def test_analyze_returns_parsed_analysis(photo_path, sample_analysis):
    client = FakeClient(_ok_response(sample_analysis))
    result = analyze_image(photo_path, client=client)
    assert isinstance(result, FoodAnalysis)
    assert result is sample_analysis


def test_analyze_request_shape(photo_path, sample_analysis):
    client = FakeClient(_ok_response(sample_analysis))
    analyze_image(photo_path, model="claude-haiku-4-5", client=client)
    (call,) = client.messages.calls
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] == MAX_TOKENS
    assert call["output_format"] is FoodAnalysis
    assert call["system"] == vision.SYSTEM_PROMPT
    assert len(call["messages"]) == 1
    assert call["messages"][0]["content"][0]["type"] == "image"


def test_refusal_raises(photo_path):
    response = SimpleNamespace(
        parsed_output=None,
        stop_reason="refusal",
        stop_details=SimpleNamespace(explanation="policy declined"),
    )
    with pytest.raises(RefusalError, match="policy declined"):
        analyze_image(photo_path, client=FakeClient(response))


def test_missing_parsed_output_raises(photo_path):
    response = SimpleNamespace(parsed_output=None, stop_reason="max_tokens")
    with pytest.raises(VisionError, match="max_tokens"):
        analyze_image(photo_path, client=FakeClient(response))
