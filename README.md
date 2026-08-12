# CalorieCam

Take a photo of food, get a calorie estimate. Photo -> Claude vision (structured
JSON) -> known-foods database lookup -> sanity math -> itemized report with a
low/mid/high range.

Every line in the report carries a provenance tier:

| Tier | Meaning | Marker |
|---|---|---|
| `database_branded` | Recognized menu item; calories from `caloriecam/data/fastfood.json` (portion standardized) | `[high, db]` |
| `database_generic` | Common food; energy density from `caloriecam/data/generic.json`, portion from the photo | `[medium, db]` |
| `model_estimate` | No database match; the vision model's own numbers, widest range | `[low]` |

Database values were compiled from USDA data and restaurant disclosures; edit
the two JSON files to add chains or foods - no code changes needed.

## Setup (once)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # then paste your Anthropic API key into .env
```

## Usage - phone (the main way)

Double-click `start-web.cmd` (or run `.venv\Scripts\python.exe app.py`). It prints
two URLs:

- `http://localhost:8000` - on this PC
- `http://<your-lan-ip>:8000` - open this on your phone (same Wi-Fi)

Tap **Snap or pick a photo**, take the picture, wait ~20-60 s, get the breakdown.

Every analysis is **adversarially reviewed**: a skeptic pass challenges the
draft (missed sauces, undercounted portions, hallucinated items) and the lead
analyst must rule on each challenge with a stated reason before the final
numbers come back. The result shows how many challenges were raised and how
many led to corrections. Disable with `--no-debate` (CLI) or
`CALORIECAM_DEBATE=off` (server env) to cut cost/latency to a single call.
The optional **"Anything the camera can't see?"** box feeds extra knowledge into
the analysis - cooking oil, "all organic", "double chicken", "diet soda" - and
the model weights it as ground truth over visual guesses (but it can never
invent items that aren't in the photo). The CLI equivalent is `--hint "..."`.
If the phone can't reach it, allow Python through the Windows Firewall prompt
(private networks) the first time the server runs.

## Usage - command line

```powershell
.venv\Scripts\python.exe estimate.py path\to\photo.jpg
.venv\Scripts\python.exe estimate.py photo.jpg --json
.venv\Scripts\python.exe estimate.py photo.jpg --model claude-haiku-4-5
```

Example output:

```
CalorieCam  lunch.jpg
------------------------------------------------------------------
grilled chicken breast    150 g  ~ 248 kcal  (210-285)  [high]
white rice                200 g  ~ 260 kcal  (182-338)  [medium]
------------------------------------------------------------------
TOTAL                            ~ 508 kcal  (392-623)
scale reference: dinner plate
```

Estimates are ranges on purpose - portion size from a single photo is inherently
uncertain (roughly +/-20-30% is the realistic accuracy for any photo-based
calorie tool).

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```

All tests run offline (the API is mocked).

## Roadmap

- **Phase 1 (done):** core engine + CLI.
- **Phase 2 (done):** known-foods database (`caloriecam/data/`) with fuzzy lookup -
  recognized menu items use compiled published calories; common foods use
  database energy density; portion heuristics (deck-of-cards, fist, restaurant
  portions run 1.5-2x label size) baked into the vision prompt.
- **Phase 3 (done):** mobile web UI - FastAPI server (`app.py`) + dark-theme
  camera page (`static/index.html`): total card, per-item confidence pills,
  `db` badges for database-backed values. Launch with `start-web.cmd`.
