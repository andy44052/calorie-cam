# CalorieCam

Take a photo of food, get a calorie estimate.

**Live:** https://caloriecam-hwhz.onrender.com (PIN-protected — works from any
phone, any network, PC off).

Measured on a 23-photo benchmark: **~9% mean error, +1% bias, ~20.8¢ per photo.**
Estimates are reported as ranges on purpose — portion size from a single photo
is inherently uncertain, and the honest number is a band, not a point.

---

## How an estimate is produced

```
photo
  ├─ 1. vision analysis      Claude identifies foods, counts units, estimates
  │                          portions and energy density (structured JSON)
  ├─ 2. adversarial review   a skeptic challenges the draft; the lead analyst
  │                          rules on each challenge with a stated reason
  ├─ 3. database lookup      recognized foods get published/USDA values
  ├─ 4. unit-weight clamp    countable foods: grams = count x per-unit weight
  └─ 5. sanity math          plausibility clamps, uncertainty ranges, totals
```

Only step 1–2 cost money. Steps 3–5 are deterministic Python and fully tested
offline.

### Provenance — every line says where its number came from

| Tier | Meaning | Marker |
|---|---|---|
| `database_branded` | Recognized menu item; published calories, portion standardized | `[high, db]` |
| `database_generic` | Common food; USDA energy density, portion from the photo | `[medium, db]` |
| `model_estimate` | No database match; the model's own numbers, widest range | `[low]` |

A wrong database match is worse than no match — the model's own estimate is a
decent fallback — so the matcher is deliberately conservative and refuses
rather than guesses.

### Data files (edit these, no code changes needed)

| File | Contents |
|---|---|
| `caloriecam/data/generic.json` | ~95 everyday foods, kcal/100g (USDA) |
| `caloriecam/data/fastfood.json` | Chain menu items with published calories |
| `caloriecam/data/units.json` | 69 per-unit edible weight bands (USDA/FNDDS) |

`pytest` audits all three offline for alias collisions and dead aliases — see
Tests below.

---

## Setup (once)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # then paste your Anthropic API key into .env
```

## Usage — phone (the main way)

Use the deployed URL above, or run it locally: double-click `start-web.cmd`
(or `.venv\Scripts\python.exe app.py`). It prints two URLs — `localhost:8000`
for this PC and a LAN address for your phone on the same Wi-Fi. If the phone
can't connect, allow Python through the Windows Firewall prompt (private
networks) the first time.

Tap **Snap or pick a photo**, take the picture, wait ~20–60 s.

**The "Anything the camera can't see?" box is the highest-value thing you can
do for accuracy.** Cooking oil, "all organic", "double chicken", "diet soda",
"I only ate half" — the model treats it as ground truth over what it can see,
because you know things the camera can't. It can refine an estimate but can
never invent food that isn't in the photo.

## Usage — command line

```powershell
.venv\Scripts\python.exe estimate.py photo.jpg
.venv\Scripts\python.exe estimate.py photo.jpg --json
.venv\Scripts\python.exe estimate.py photo.jpg --hint "cooked in olive oil"
.venv\Scripts\python.exe estimate.py photo.jpg --no-debate   # 1 call, ~1/3 cost
```

```
CalorieCam  lunch.jpg
------------------------------------------------------------------
grilled chicken breast    150 g  ~ 248 kcal  (210-285)  [high, db]
white rice                200 g  ~ 260 kcal  (182-338)  [medium, db]
------------------------------------------------------------------
TOTAL                            ~ 508 kcal  (392-623)
debate: 2 challenge(s) raised - 1 led to corrections, 1 rejected
scale reference: dinner plate
```

## Cost

Measured, not estimated — every result carries a `usage` block with per-call
tokens, latency, and dollars.

| Photo | Cost | Calls |
|---|---|---|
| Simple (two apples) | 4.9¢ | 1–2 |
| Medium (rice bowl) | 11.8¢ | 3 |
| Complex (charcuterie board) | 45.6¢ | 3 |

Output/thinking tokens dominate (34–76%), so cost scales with scene complexity,
not photo size. Levers: `--no-debate` / `CALORIECAM_DEBATE=off` (~⅔ off), or a
cheaper `--model`.

## Configuration

| Env var | Effect |
|---|---|
| `ANTHROPIC_API_KEY` | Required. |
| `CALORIECAM_PIN` | If set, the API requires this PIN. Set it on any public deployment. |
| `CALORIECAM_DEBATE` | `off` disables adversarial review (cheaper, faster, less accurate). |
| `CALORIECAM_HISTORY` | `off` disables the meal diary + portion blending; a path uses that DB file. |
| `CALORIECAM_SKEPTIC_MODEL` | Run the critic/reviser on a cheaper model (e.g. `claude-haiku-4-5`). Benchmark before enabling. |
| `PORT` | Server port (default 8000). |

## Meal history and personal portions

Every estimate is logged to a local SQLite diary (`caloriecam/history.db`,
gitignored). That buys three things:

- **"Today so far"** — a running daily total on every result, corrections
  included.
- **Personal portion blending** — when the same food shows up again (2+ past
  sightings), the fresh portion estimate is shrunk toward *your* median
  portion, which collapses run-to-run noise at zero API cost. Items with
  counted units or published menu portions are never blended, a density check
  refuses to blend across name collisions ("grilled cheese" is not "cheese"),
  and the diary always stores the raw pre-blend estimate so the prior can
  never feed on its own output.
- **Corrections** — the "Know better?" box on each result stores what the
  meal really was; future estimates of that food lean on it. Tick **measured**
  when the number came from a kitchen scale or a package label rather than a
  guess: those become the gold labels calibration is fitted on.

### Calibration (measure your own bias, then remove it)

The pipeline stacks several deliberate upward nudges (portion sizing rules,
the skeptic's challenges, unit-weight clamps). Their combined residual is a
small systematic bias — measured at +2% on the benchmark. Once you have ten
or more **measured** meals in the diary, fit it out:

```powershell
.venv\Scripts\python.exe calibrate.py show    # gold-meal count + active factors
.venv\Scripts\python.exe calibrate.py fit     # fit, review, write
```

The fit solves for one multiplier per calorie source (database-generic,
database-branded, model-estimate), shrinks the result toward 1.0 (hard when
the sample is small), clamps every factor to ±15%, and leaves any source seen
in fewer than three meals untouched. Delete
`caloriecam/data/calibration.json` to turn calibration off entirely.

Two guards keep this from becoming a second source of error: the diary stores
each item's calibration factor and divides it back out when fitting, so a
refit never compounds the last one; and the printed before/after numbers are
in-sample, so judge a new fit on the *next* week of meals, not on the fit's
own report.

The adaptive debate gate also decides per photo whether the skeptic pass is
worth paying for: drafts where every item is database-anchored, few in number,
and tightly banded skip the second opinion.

Note for the cloud deployment: Render's free tier has an ephemeral disk, so
the diary resets on every deploy or spin-down. History shines on the local
server; give the service a persistent disk if you want it in the cloud.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```

198 tests, all offline (the API is mocked) — including a **collision audit**
that sweeps every alias in every data file for two failure modes: an alias
resolving to a food with wildly different calories ("banana bread" must not
weigh in as banana), and dead aliases that match nothing. It runs in ~2 seconds
and replaces what used to be a paid API benchmark sweep, so the database can
grow without silently regressing.

## Deploying

Push to GitHub; Render picks up `render.yaml` automatically. Set
`ANTHROPIC_API_KEY` and `CALORIECAM_PIN` in the Render dashboard. The free tier
sleeps after ~15 idle minutes, so the first photo after a nap takes an extra
minute.

## Accuracy: what's solved and what isn't

**Solved:** food identification (23/23 on the benchmark, including reading
brand names off packaging) and database matching (0 wrong matches in 69 runs,
after an adversarial audit of 515 probes).

**Partly solved:** run-to-run stability. Median spread between repeat runs of
the same photo is ~10%. The worst cases were fixed or halved — seasoned fries
76%→19%, grapes 67%→5%, lemon bars 33%→16%, brie 64%→27% — by pinning counts to
per-unit weight bands, scoping frame-edge items to the visible portion, and
requiring a stated count to agree with the stated grams.

**Not solved:** portion weight for amorphous piles (a mound of pasta has no
countable units and no standard weight). The remaining fix for that class is
sampling — run the analysis N times and take the median — which costs real
money per photo and hasn't been judged worth it yet.

**Known open items**
- Whole-dish vs. portion ambiguity: a pan pizza can be priced as the 3 visible
  slices or the whole pan; both readings are internally consistent.
- Whole-tray photos count everything visible, not one serving. Use the hint box
  ("I ate two slices").
- Prompt caching is scoped but unshipped — worth ~12–15% of cost, but it needs
  a prompt restructure and a benchmark run to validate.
