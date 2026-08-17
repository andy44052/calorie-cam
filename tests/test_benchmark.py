"""The benchmark harness must be correct BEFORE a paid sweep feeds it."""

import json

import pytest

import benchmark


def _run_record(photo, run, total, *, cost=0.05, source="database_generic",
                accepted=1, partial=0, rejected=0, debated=True, unit_count=None):
    return {
        "photo": photo, "run": run, "ok": True, "model": "claude-opus-5",
        "total": {"low": int(total * 0.8), "mid": total, "high": int(total * 1.2)},
        "items": [{
            "name": "test food", "brand": None, "grams": 200,
            "kcal_per_100g": 150, "kcal_mid": total, "confidence": "medium",
            "source": source, "unit_count": unit_count, "per_unit_grams": None,
            "assumptions": [],
        }],
        "scale_reference": "plate", "notes": None,
        "debate": {
            "ran": debated, "challenges": accepted + partial + rejected,
            "kinds": ["portion_too_low"] * (accepted + partial + rejected),
            "accepted": accepted, "partially_accepted": partial, "rejected": rejected,
        },
        "usage": {"total_cost_usd": cost, "call_count": 3, "total_seconds": 40},
        "seconds": 40.0,
    }


@pytest.fixture
def results(tmp_path):
    path = tmp_path / "results.jsonl"
    records = [
        _run_record("a.jpg", 1, 400), _run_record("a.jpg", 2, 420), _run_record("a.jpg", 3, 410),
        _run_record("b.jpg", 1, 900), _run_record("b.jpg", 2, 1100), _run_record("b.jpg", 3, 1000),
    ]
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def test_report_runs_and_summarizes(results, capsys):
    assert benchmark.main(["report", str(results)]) == 0
    out = capsys.readouterr().out
    assert "6 runs over 2 photos" in out
    assert "a.jpg" in out and "b.jpg" in out
    assert "stability:" in out
    assert "$0.30 total" in out  # 6 runs x $0.05


def test_spread_math():
    assert benchmark._spread([400, 420, 410]) == pytest.approx(4.878, rel=1e-3)
    assert benchmark._spread([100, 100, 100]) == 0.0
    assert benchmark._spread([500]) == 0.0
    assert benchmark._spread([]) == 0.0


def test_report_surfaces_debate_verdicts(results, capsys):
    """The scan's headline question: is the reviser a rubber stamp?"""
    benchmark.main(["report", str(results)])
    out = capsys.readouterr().out
    assert "verdicts:" in out
    assert "accepted" in out and "rejected" in out


def test_report_scores_against_truth(tmp_path, results, capsys):
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps({"a.jpg": {"kcal": 410}, "b.jpg": {"kcal": 500}}), encoding="utf-8")
    benchmark.main(["report", str(results), "--truth", str(truth)])
    out = capsys.readouterr().out
    assert "accuracy vs truth" in out
    assert "MAPE" in out and "bias" in out
    assert "HIGH" in out  # b.jpg estimates 1000 against a truth of 500


def test_truth_accepts_a_bare_number(tmp_path, results, capsys):
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps({"a.jpg": 410}), encoding="utf-8")
    benchmark.main(["report", str(results), "--truth", str(truth)])
    assert "MAPE" in capsys.readouterr().out


def test_report_compares_against_a_baseline(tmp_path, results, capsys):
    old = tmp_path / "old.jsonl"
    old.write_text("\n".join(json.dumps(r) for r in [
        _run_record("a.jpg", 1, 200), _run_record("a.jpg", 2, 600),
    ]), encoding="utf-8")
    benchmark.main(["report", str(results), "--compare", str(old)])
    out = capsys.readouterr().out
    assert "vs old.jsonl" in out
    assert "spread" in out


def test_failed_runs_are_excluded(tmp_path, capsys):
    path = tmp_path / "mixed.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        _run_record("a.jpg", 1, 400),
        {"photo": "b.jpg", "run": 1, "ok": False, "error": "boom"},
    ]), encoding="utf-8")
    benchmark.main(["report", str(path)])
    assert "1 runs over 1 photos" in capsys.readouterr().out


def test_skip_debate_runs_are_reported(tmp_path, capsys):
    path = tmp_path / "skipped.jsonl"
    path.write_text(json.dumps(
        _run_record("a.jpg", 1, 300, debated=False, accepted=0)
    ), encoding="utf-8")
    benchmark.main(["report", str(path)])
    assert "skip-debate gate fired on 1 runs" in capsys.readouterr().out


def test_database_coverage_is_reported(results, capsys):
    benchmark.main(["report", str(results)])
    out = capsys.readouterr().out
    assert "database:" in out
    assert "database_generic" in out
    assert "% of kcal" in out


def test_run_rejects_a_directory_with_no_images(tmp_path, capsys):
    assert benchmark.main(["run", str(tmp_path)]) == 1
    assert "no images found" in capsys.readouterr().err


def test_empty_results_file_is_an_error(tmp_path, capsys):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert benchmark.main(["report", str(path)]) == 1
