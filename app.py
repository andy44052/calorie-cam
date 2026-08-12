"""CalorieCam web server - snap a photo on your phone, get calories back.

Run:  python app.py   (or start-web.cmd)
Then open the printed URL on any phone on the same Wi-Fi.
"""

import os
import secrets
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import anthropic  # noqa: E402
from fastapi import FastAPI, File, Header, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from PIL import UnidentifiedImageError  # noqa: E402

from caloriecam import pipeline, report  # noqa: E402
from caloriecam.vision import RefusalError, VisionError  # noqa: E402

MAX_UPLOAD_BYTES = 15 * 1024 * 1024

app = FastAPI(title="CalorieCam")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


def _required_pin() -> str:
    """When CALORIECAM_PIN is set (e.g. on a public host), /api/estimate needs it."""
    return os.environ.get("CALORIECAM_PIN", "").strip()


@app.post("/api/estimate")
async def estimate(
    photo: UploadFile = File(...),
    x_caloriecam_pin: str | None = Header(default=None),
) -> dict:
    required = _required_pin()
    if required and not secrets.compare_digest((x_caloriecam_pin or "").strip(), required):
        raise HTTPException(
            status_code=401, detail="This server requires an access PIN."
        )

    data = await photo.read()
    if not data:
        raise HTTPException(status_code=400, detail="The upload was empty - try again.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (15 MB max).")

    try:
        meal, _analysis = pipeline.run_bytes(data)
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

    return report.to_dict(meal, photo.filename or "photo")


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
