"""CalorieCam CLI - estimate calories from photos of food.

Usage:
    python estimate.py photo.jpg [more_photos...] [--json] [--model MODEL]
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv()  # also pick up a .env in the current working directory

import anthropic  # noqa: E402

import sqlite3  # noqa: E402

from caloriecam import history, pipeline, report  # noqa: E402
from caloriecam.config import DEFAULT_MODEL, HINT_MAX_CHARS, MAX_IMAGE_PX  # noqa: E402
from caloriecam.vision import RefusalError, VisionError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="estimate", description="Estimate calories from a photo of food."
    )
    parser.add_argument("images", nargs="+", help="path(s) to food photo(s)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model id (default: {DEFAULT_MODEL})")
    parser.add_argument("--max-px", type=int, default=MAX_IMAGE_PX, help="long-edge resize before upload")
    parser.add_argument(
        "--hint",
        default=None,
        help='extra context the photo cannot show, e.g. "cooked in olive oil"',
    )
    parser.add_argument(
        "--no-debate",
        dest="debate",
        action="store_false",
        help="skip the adversarial second-opinion pass (faster, cheaper, less accurate)",
    )
    parser.add_argument(
        "--skeptic-model",
        default=None,
        help="run the critic+reviser on a cheaper model (e.g. claude-haiku-4-5)",
    )
    parser.add_argument(
        "--no-history",
        dest="history",
        action="store_false",
        help="don't log this meal or blend portions with past meals",
    )
    args = parser.parse_args(argv)

    store = history.default_store() if args.history else None

    failures = 0
    for image in args.images:
        try:
            meal, analysis = pipeline.run(
                image,
                model=args.model,
                hint=args.hint,
                debate=args.debate,
                skeptic_model=args.skeptic_model,
                history=store,
            )
        except FileNotFoundError:
            print(f"error: file not found: {image}", file=sys.stderr)
            failures += 1
            continue
        except TypeError as exc:
            # The SDK raises TypeError (not AuthenticationError) when it finds
            # no credentials at all.
            if "authentication" not in str(exc).lower():
                raise
            print(
                "error: no Anthropic API key found. Copy .env.example to .env and put "
                "your key in it (get one at https://platform.claude.com).",
                file=sys.stderr,
            )
            return 1
        except anthropic.AuthenticationError:
            print(
                "error: no valid Anthropic API key. Copy .env.example to .env and put "
                "your key in it (get one at https://platform.claude.com).",
                file=sys.stderr,
            )
            return 1
        except anthropic.NotFoundError:
            print(f"error: unknown model id '{args.model}'", file=sys.stderr)
            return 1
        except anthropic.RateLimitError:
            print("error: rate limited by the API - wait a minute and retry.", file=sys.stderr)
            failures += 1
            continue
        except anthropic.APIStatusError as exc:
            print(f"error: API returned {exc.status_code}: {exc.message}", file=sys.stderr)
            failures += 1
            continue
        except anthropic.APIConnectionError:
            print("error: could not reach the API - check your internet connection.", file=sys.stderr)
            failures += 1
            continue
        except RefusalError as exc:
            print(f"error: {exc}", file=sys.stderr)
            failures += 1
            continue
        except VisionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            failures += 1
            continue

        payload = report.to_dict(meal, image)
        if store is not None and meal.items:
            try:
                payload["meal_id"] = store.record(
                    meal, analysis, model=args.model,
                    hint=(args.hint or "")[:HINT_MAX_CHARS] or None,
                )
                payload["today"] = store.today_total()
            except sqlite3.Error as exc:
                print(f"warning: could not record meal in history ({exc})", file=sys.stderr)

        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(report.to_text(meal, image))
            if "today" in payload:
                today = payload["today"]
                print(f"today so far: ~{today['kcal']} kcal across {today['meals']} meal(s)")
            print()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
