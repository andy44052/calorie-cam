"""CalorieCam web server - snap a photo on your phone, get calories back.

Run:  python app.py   (or start-web.cmd)
Then open the printed URL on any phone on the same Wi-Fi.
"""

import logging
import os
import secrets
import socket
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import anthropic  # noqa: E402
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from PIL import UnidentifiedImageError  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

from caloriecam import __version__, history, pipeline, report  # noqa: E402
from caloriecam.config import DEFAULT_MODEL, HINT_MAX_CHARS  # noqa: E402
from caloriecam.vision import RefusalError, VisionError  # noqa: E402

MAX_UPLOAD_BYTES = 15 * 1024 * 1024

app = FastAPI(title="CalorieCam")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

# One shared store; each method opens its own connection, so this is
# thread-safe under FastAPI's worker pool. None when CALORIECAM_HISTORY=off.
_history = history.default_store()


@app.get("/")
def index() -> FileResponse:
    # no-cache = revalidate every load. Phones otherwise keep serving a stale
    # cached page after a deploy and never see UI changes.
    return FileResponse(
        ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


def _required_pin() -> str:
    """When CALORIECAM_PIN is set (e.g. on a public host), /api/estimate needs it."""
    return os.environ.get("CALORIECAM_PIN", "").strip()


def _debate_enabled() -> bool:
    """Adversarial review is on unless CALORIECAM_DEBATE is off/0/false/no."""
    return os.environ.get("CALORIECAM_DEBATE", "on").strip().lower() not in {
        "off", "0", "false", "no",
    }


def _skeptic_model() -> str | None:
    """Optional cheaper model for the critic+reviser calls (e.g. claude-haiku-4-5).

    Unset keeps the debate on the primary model - flip only after a benchmark
    comparison shows verdicts and accuracy hold.
    """
    return os.environ.get("CALORIECAM_SKEPTIC_MODEL", "").strip() or None


def _critic_count() -> int:
    try:
        return max(1, int(os.environ.get("CALORIECAM_CRITIC_COUNT", "1")))
    except ValueError:
        return 1


def _check_pin(x_caloriecam_pin: str | None) -> None:
    required = _required_pin()
    if not required:
        return
    # Compare as bytes: compare_digest raises TypeError on non-ASCII str, so a
    # single weird header byte would 500 instead of 401.
    supplied = (x_caloriecam_pin or "").strip().encode("utf-8", "replace")
    if not secrets.compare_digest(supplied, required.encode("utf-8")):
        raise HTTPException(status_code=401, detail="This server requires an access PIN.")


@app.post("/api/estimate")
async def estimate(
    photo: UploadFile = File(...),
    hint: str = Form(""),
    x_caloriecam_pin: str | None = Header(default=None),
) -> dict:
    _check_pin(x_caloriecam_pin)
    # One truncation for both the prompt and the history log, so the stored
    # hint always matches what the model actually saw.
    hint_val = hint.strip()[:HINT_MAX_CHARS] or None

    data = await photo.read()
    if not data:
        raise HTTPException(status_code=400, detail="The upload was empty - try again.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (15 MB max).")

    try:
        meal, analysis = pipeline.run_bytes(
            data,
            hint=hint_val,
            debate=_debate_enabled(),
            skeptic_model=_skeptic_model(),
            critic_count=_critic_count(),
            history=_history,
        )
    except UnidentifiedImageError:
        raise HTTPException(status_code=400, detail="That file is not a readable image.")
    except RefusalError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except VisionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except TypeError as exc:
        if "authentication" not in str(exc).lower():
            raise
        raise HTTPException(
            status_code=500, detail="No API key configured on the server (.env)."
        )
    except anthropic.AuthenticationError:
        raise HTTPException(
            status_code=500, detail="The server's API key was rejected - check .env."
        )
    except anthropic.RateLimitError:
        raise HTTPException(
            status_code=429, detail="Rate limited - wait a minute and try again."
        )
    except anthropic.APIStatusError as exc:
        raise HTTPException(status_code=502, detail=f"API error {exc.status_code}.")
    except anthropic.APIConnectionError:
        raise HTTPException(
            status_code=502, detail="The server could not reach the Claude API."
        )

    payload = report.to_dict(meal, photo.filename or "photo")
    # Record only real meals - a photo of a keyboard must not become a 0-kcal
    # "meal" inflating the daily count. And a history write failure must never
    # cost the user an estimate they already paid API tokens for: degrade to a
    # response without meal_id/today instead.
    if _history is not None and meal.items:
        try:
            payload["meal_id"] = _history.record(
                meal, analysis, model=DEFAULT_MODEL, hint=hint_val
            )
            payload["today"] = _history.today_total()
        except sqlite3.Error:
            logging.exception("history record failed; returning estimate without it")
            payload.pop("meal_id", None)
            payload.pop("today", None)
    return payload


class Correction(BaseModel):
    kcal: int = Field(gt=0, lt=100_000, description="What the meal really was")


@app.post("/api/meals/{meal_id}/correct")
def correct_meal(
    meal_id: int,
    correction: Correction,
    x_caloriecam_pin: str | None = Header(default=None),
) -> dict:
    """Store the user's "actually it was ~N kcal" for a logged meal.

    The raw estimate stays untouched - the correction is a truth anchor the
    portion-blending prior applies at read time.
    """
    _check_pin(x_caloriecam_pin)
    if _history is None:
        raise HTTPException(status_code=404, detail="History is disabled on this server.")
    if not _history.correct(meal_id, correction.kcal):
        raise HTTPException(status_code=404, detail="No such meal.")
    return {"ok": True, "meal_id": meal_id, "corrected_mid": correction.kcal,
            "today": _history.today_total()}


@app.get("/api/history/today")
def history_today(x_caloriecam_pin: str | None = Header(default=None)) -> dict:
    _check_pin(x_caloriecam_pin)
    if _history is None:
        raise HTTPException(status_code=404, detail="History is disabled on this server.")
    return _history.today_total()


@app.get("/api/history/daily")
def history_daily(x_caloriecam_pin: str | None = Header(default=None)) -> dict:
    _check_pin(x_caloriecam_pin)
    if _history is None:
        raise HTTPException(status_code=404, detail="History is disabled on this server.")
    return {"days": _history.daily_totals(), "lifetime_cost_usd": _history.lifetime_cost_usd()}


def _lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    print()
    print("  CalorieCam is starting...")
    print(f"  On this PC:      http://localhost:{port}")
    print(f"  On your phone:   http://{_lan_ip()}:{port}   (same Wi-Fi)")
    print()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
