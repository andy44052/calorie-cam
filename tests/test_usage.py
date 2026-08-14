from types import SimpleNamespace

import pytest

from caloriecam import pipeline, report
from caloriecam.debate import Critique
from caloriecam.usage import CallUsage, UsageLedger, from_response


def _resp(inp, out, cache_read=0, cache_write=0):
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )
    )


def test_cost_math_opus():
    # 10k input @ $5/MTok + 2k output @ $25/MTok = 0.05 + 0.05 = $0.10
    call = CallUsage(stage="analyze", model="claude-opus-5", input_tokens=10_000, output_tokens=2_000)
    assert round(call.cost_usd, 4) == 0.1


def test_cost_math_haiku_is_five_times_cheaper():
    opus = CallUsage(stage="a", model="claude-opus-5", input_tokens=10_000, output_tokens=2_000)
    haiku = CallUsage(stage="a", model="claude-haiku-4-5", input_tokens=10_000, output_tokens=2_000)
    assert round(opus.cost_usd / haiku.cost_usd, 2) == 5.0


def test_cached_reads_are_cheap():
    plain = CallUsage(stage="a", model="claude-opus-5", input_tokens=10_000)
    cached = CallUsage(stage="a", model="claude-opus-5", cache_read_tokens=10_000)
    assert cached.cost_usd == pytest.approx(plain.cost_usd * 0.1)


def test_unknown_model_falls_back_to_default_price():
    call = CallUsage(stage="a", model="some-future-model", input_tokens=1_000_000)
    assert call.cost_usd == 5.0


def test_from_response_handles_missing_usage():
    call = from_response(SimpleNamespace(), stage="analyze", model="claude-opus-5", seconds=1.0)
    assert call.input_tokens == 0 and call.cost_usd == 0.0


def test_ledger_totals():
    ledger = UsageLedger()
    ledger.record(from_response(_resp(1000, 500), "analyze", "claude-opus-5", 2.0))
    ledger.record(from_response(_resp(2000, 300), "critic", "claude-opus-5", 3.0))
    data = ledger.as_dict()
    assert data["call_count"] == 2
    assert data["input_tokens"] == 3000
    assert data["output_tokens"] == 800
    assert data["total_seconds"] == 5.0
    assert data["total_cost_usd"] > 0


class _Msgs:
    def __init__(self, analysis):
        self._analysis = analysis
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        parsed = (
            Critique(challenges=[], overall_assessment="sound")
            if kwargs["output_format"] is Critique
            else self._analysis
        )
        return SimpleNamespace(
            parsed_output=parsed,
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=1500,
                output_tokens=400,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


class FakeClient:
    def __init__(self, analysis):
        self.messages = _Msgs(analysis)


def test_pipeline_records_usage_per_stage(photo_path, sample_analysis):
    meal, _ = pipeline.run(photo_path, client=FakeClient(sample_analysis))
    assert meal.usage is not None
    stages = [c["stage"] for c in meal.usage["calls"]]
    assert stages == ["analyze", "critic"]  # no challenges -> no reviser
    assert meal.usage["input_tokens"] == 3000
    assert meal.usage["total_cost_usd"] > 0


def test_usage_reaches_json_report(photo_path, sample_analysis):
    meal, _ = pipeline.run(photo_path, client=FakeClient(sample_analysis))
    payload = report.to_dict(meal, "meal.jpg")
    assert payload["usage"]["call_count"] == 2
    assert "total_cost_usd" in payload["usage"]
