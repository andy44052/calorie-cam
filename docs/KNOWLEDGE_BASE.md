# CalorieCam — Knowledge Base

Written so someone who has never seen this project can maintain it. Read
`ARCHITECTURE.md` first for the flow diagram.

## Modules and responsibilities

| Module | Responsibility | In → Out |
|---|---|---|
| `caloriecam/vision.py` | The vision system prompt, image prep, and the one wrapper every model call goes through (`structured_call`) | photo bytes → `FoodAnalysis` |
| `caloriecam/debate.py` | Critic + reviser prompts and the debate loop | draft → revised draft + debate record |
| `caloriecam/lookup.py` | Match food names to the database | `FoodItem` → `Resolution` or `None` |
| `caloriecam/units.py` | Per-unit weight bands (one grape, one sushi piece) | food name → `UnitBand` or `None` |
| `caloriecam/sanity.py` | Portion clamps, uncertainty bands, per-item provenance, totals | analysis + matches → `MealEstimate` |
| `caloriecam/history.py` | SQLite diary, personal portion priors, corrections, gold labels | meal → row; name → past portions |
| `caloriecam/calibration.py` | Applies fitted per-source multipliers | source → factor (1.0 if unfitted) |
| `caloriecam/pipeline.py` | Glue: orchestrates all of the above, owns `needs_debate` | photo → `(MealEstimate, FoodAnalysis)` |
| `caloriecam/report.py` | Formats results for CLI text and web JSON | `MealEstimate` → str / dict |
| `caloriecam/usage.py` | Token/latency/cost ledger per call | API response → cost record |
| `caloriecam/text.py` | Shared tokenizing/stemming (matcher, units, history keys) | — |
| `app.py` | FastAPI server, PIN gate, upload limits | HTTP → JSON |
| `estimate.py` | CLI | argv → stdout |
| `benchmark.py` | Paid sweeps + scoring report | photo dir → JSONL → report |
| `calibrate.py` | Fits calibration factors from gold meals | diary → `calibration.json` |
| `audit_db_misses.py` | Classifies why foods missed the database | sweep JSONL → CSV |

**Data files** (edit freely, no code changes needed): `generic.json` (132 foods,
kcal/100g), `fastfood.json` (26 menu items, total kcal), `units.json` (69
per-unit weight bands).

## Model and prompt dependencies

- **Model:** `claude-opus-5` for all three calls (`config.DEFAULT_MODEL`).
  Measured best — see baseline below. `--skeptic-model` / `--model` swap models
  and `--critic-count N` runs a critic ensemble; both default off.
- **Prompts:** `vision.SYSTEM_PROMPT` (identification, frame scope, whole-dish
  rule, count-first portions, bulk-food geometry, density guidance),
  `debate.CRITIC_SYSTEM`, `debate.REVISER_SYSTEM`, and
  `debate.EAGER_CRITIC_SUPPLEMENT` (auto-appended only when the critic runs on
  a cheaper model than the lead).
- **Structured output:** every call uses `client.messages.parse` with a Pydantic
  schema. Pydantic `Field` constraints (ge/le) are **stripped** from enforced
  schemas — validate in Python, never rely on the schema.

## Fallback logic

| Failure | Behaviour |
|---|---|
| Reviser output truncated (`TruncatedError`) | Return the **draft**, log `reviser_truncated`. Never lose a paid estimate. |
| Critic raises nothing | Skip the reviser (saves a call) |
| No food in photo | Skip debate, skip diary write — a keyboard photo is not a 0-kcal meal |
| No database match | Keep the model's numbers, mark `model_estimate`, widen the range |
| Diary write fails (locked DB, disk full) | Return the estimate without `meal_id`/`today` |
| API refuses (`stop_reason: refusal`) | `RefusalError` → HTTP 422 |
| Non-ASCII PIN header byte | 401, not a 500 |

## Database matching rules

**Governing rule: a wrong match is worse than no match.** The model's own
estimate is a decent fallback; a confidently wrong database value is not.

- Match = entry tokens are a **subset** of item tokens, scored jaccard-style;
  thresholds 0.80 (menu) / 0.78 (generic).
- Leftover words must be **harmless modifiers** (`_MODIFIERS`: cooking methods,
  sizes, counts, colours, vine/cluster). Otherwise a single ingredient claims a
  composed dish — "banana bread" is not banana.
- `loose_match: true` marks entries that legitimately absorb ingredient words
  (combined dishes only).
- `exclude_tokens` veto a match outright. **Single words only — multi-word
  excludes silently never fire.** Check them against the entry's own aliases:
  adding "shells" to the taco entry would kill its own "hard shell taco".
- Two retry normalizations when the direct match fails: **strip parentheticals**
  and **take the head of a garnish clause** ("french fries with seasoning").
  Both re-check excludes against the *original* name, so stripping can never
  un-veto. The head retry refuses when the discarded tail names a real food.
- **Rejected mechanism — do not re-propose:** accepting subset matches when
  calorie density roughly agrees. Red-teamed and measured — composed dishes sit
  within 1.6x of their own ingredients (sandwich vs bread 1.16x, butter
  crackers vs butter 1.43x), so density cannot certify identity.

## Portion estimation rules

1. **Count first** for discrete foods: `unit_count x per_unit_grams`, clamped to
   a USDA band from `units.json`. Counts are far more repeatable than eyeballing.
2. A band applies **only if it weighs the unit that was counted** — 12 avocado
   *slices* must not be priced as 12 whole avocados (`_PIECE_WORDS`).
3. A stated count is trusted only if `count x serving` lands within 45% of the
   model's own gram estimate (pan-pizza guard).
4. **Bulk foods** get a geometry chain: plate fraction x depth → volume → grams,
   with cooked densities supplied in the prompt.
5. Grams clamp to 1–2000 g; ranges widen for low confidence or model estimates.

## Personal portions and calibration

- **Blending:** repeat foods (2+ past sightings, last 10) shrink toward the
  user's median with weight `n/(n+2)`. Skips counted and branded items. Refuses
  when past density disagrees with today's item by >1.6x (name-key collision
  guard: "grilled cheese" must not borrow plain cheese's portions).
- **Corrections** apply at *read* time and never rewrite the stored estimate.
  Ratio clamped 0.2–5x so a typo cannot poison the prior.
- **Calibration** multiplies each source's kcal by a fitted factor. Inactive
  until `calibration.json` exists. `calibrate.py fit` needs ≥10 **measured**
  meals, clamps to ±15%, shrinks toward 1.0 on small samples, skips sources
  seen <3 times.
- **Shared doctrine: the system never trains on its own output.** The diary
  stores pre-blend grams; each item records the calibration factor applied and
  the fit divides it back out.

## Current benchmark baseline (Run A: 23 photos x 3 runs, $12.81)

| Metric | Value |
|---|---|
| Average error vs verified meals | **8%** |
| Bias | **+2%** |
| Truth photos in tolerance | **6 / 6** |
| Calories database-backed | **74%** |
| Wrong food matches | **0** |
| Cost per photo | **18.8¢** |
| Run-to-run spread (median) | 11% (12%→11% for meals ≥300 kcal) |

Cheaper configurations, same photos: Haiku critic 6.6¢ but 14% error / −10%
bias; full Sonnet 11.5¢, 18% / +8%; Sonnet critic 13¢, 16% / −11%. **The
expensive critic's aggressive challenging is where the accuracy comes from** —
it changes the answer in 64 of 68 runs.

## Regression cases (why these tests exist)

| Test file | Bug it prevents |
|---|---|
| `test_collision_audit.py` | Any alias resolving to a >2x different food; dead aliases; multi-word excludes. Replaces a paid 515-probe red team, runs in ~2s |
| `test_coverage_growth.py` | Steak claiming creamy steak pasta; butter claiming butter crackers; 12 slices weighed as 12 whole fruits |
| `test_history.py` | Correcting a blended meal corrupting the prior; branded items blending; digit-split history keys |
| `test_calibration.py` | Calibration compounding its own output; fitting below 10 meals; legacy DB migration |
| `test_debate.py` | Truncated reviser destroying a paid estimate; ensemble dedupe |
| `test_skip_debate.py` | Colour variants counting as unmatched |

## Known limitations

- **Amorphous piles** (a mound of pasta) are the weakest class — no countable
  units, no standard weight. The only real fix is median-of-N sampling, which
  costs money per photo.
- **Upward pressure is systemic and unresolved.** 92% of critic challenges push
  estimates up; unit-band clamps moved 15 items up vs 3 down. That was correct
  when the app undercounted by 6%; now it lands on already-accurate database
  values. Calibration is the intended fix, pending gold meals.
- **Grapes berry-count drift** — counts rose 90–100 → 110–164 between sweeps,
  +55% vs truth. Deliberately unpatched; needs its own measurement.
- **The truth set is 6 photos.** Every accuracy number rests on them. The
  "measured" checkbox exists to grow it from real meals.
- **Render's free tier has an ephemeral disk** — the diary resets on every
  deploy. Diary features are local-server-first until a persistent disk exists.
- **Method warning:** a prompt rule was once tuned against a sweep that had also
  swapped the model. The signal was confounded and the conclusion wrong.
  **One variable per sweep.**

## Running, testing, deploying

```powershell
# setup
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # then add ANTHROPIC_API_KEY

# run
start-web.cmd                                   # web server, phone-accessible
.venv\Scripts\python.exe estimate.py photo.jpg  # CLI

# test - all offline, no API cost
.venv\Scripts\python.exe -m pytest -q           # 312 tests, ~15s

# benchmark - COSTS MONEY, always set --max-cost
.venv\Scripts\python.exe benchmark.py run photos\ --runs 3 --out r.jsonl --max-cost 15
.venv\Scripts\python.exe benchmark.py report r.jsonl --truth truth.json --compare runA.jsonl

# calibration
.venv\Scripts\python.exe calibrate.py show      # gold-meal count
.venv\Scripts\python.exe calibrate.py fit       # needs >= 10 measured meals
```

**Deploy:** push to GitHub; Render picks up `render.yaml`. Set
`ANTHROPIC_API_KEY` and `CALORIECAM_PIN` in the dashboard.

**Env vars:** `CALORIECAM_PIN`, `CALORIECAM_DEBATE=off`,
`CALORIECAM_HISTORY=off|path`, `CALORIECAM_SKEPTIC_MODEL`,
`CALORIECAM_CRITIC_COUNT`, `CALORIECAM_CALIBRATION`, `PORT`.

**Gotchas that cost real debugging time:** data files must be read `utf-8-sig`
(Windows editors add a BOM); `secrets.compare_digest` raises on non-ASCII str
(compare bytes); the SDK validates structured-output JSON *before* `stop_reason`
is visible, so truncation surfaces as a `ValidationError`, not a clean signal.
