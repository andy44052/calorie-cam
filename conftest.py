import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Point history at a throwaway location BEFORE any test imports app.py, whose
# module-level default_store() would otherwise create caloriecam/history.db in
# the working tree the moment the suite starts.
_HISTORY_SANDBOX = tempfile.mkdtemp(prefix="caloriecam-test-history-")
os.environ["CALORIECAM_HISTORY"] = str(Path(_HISTORY_SANDBOX) / "import-time.db")

from caloriecam.schema import FoodAnalysis, FoodItem  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_history(tmp_path, monkeypatch):
    """Every test gets its own empty history DB - no bleed between tests."""
    db = tmp_path / "history.db"
    monkeypatch.setenv("CALORIECAM_HISTORY", str(db))
    webapp = sys.modules.get("app")
    if webapp is not None and getattr(webapp, "_history", None) is not None:
        from caloriecam.history import HistoryStore

        monkeypatch.setattr(webapp, "_history", HistoryStore(db))
    yield


@pytest.fixture
def sample_analysis() -> FoodAnalysis:
    """Two-item meal with known math: totals are mid 508, low 392, high 623."""
    return FoodAnalysis(
        items=[
            FoodItem(
                name="grilled chicken breast",
                portion_description="one breast, half the plate",
                estimated_grams=150,
                kcal_per_100g=165,
                confidence="high",
                assumptions=["grilled with a little oil"],
                brand=None,
            ),
            FoodItem(
                name="white rice",
                portion_description="about one cup, piled",
                estimated_grams=200,
                kcal_per_100g=130,
                confidence="medium",
                assumptions=["steamed, no butter visible"],
                brand=None,
            ),
        ],
        scale_reference="dinner plate",
        overall_notes=None,
    )


@pytest.fixture
def empty_analysis() -> FoodAnalysis:
    return FoodAnalysis(
        items=[],
        scale_reference=None,
        overall_notes="The image shows a laptop keyboard; no food is visible.",
    )


@pytest.fixture
def photo_path(tmp_path) -> Path:
    """A real (blank) 2400x1600 JPEG on disk, larger than the resize limit."""
    from PIL import Image

    path = tmp_path / "meal.jpg"
    Image.new("RGB", (2400, 1600), (180, 120, 60)).save(path, "JPEG")
    return path
