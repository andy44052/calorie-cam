# CalorieCam — Architecture

Photo in, itemized calorie estimate out. The design principle running through
everything: **the model is used for perception, not arithmetic.** Claude
identifies food and estimates portions; every calorie number after that comes
from a database and deterministic Python that can be tested offline for free.

## Main flow

```mermaid
flowchart TD
    Photo["Photo + optional hint<br/>(phone, CLI, or benchmark)"] --> Prep

    Prep["prepare_image<br/>EXIF-rotate, 1568px, JPEG q85"] --> Vision

    Vision["<b>MODEL CALL 1 - vision</b><br/>analyze_prepared<br/>-> FoodAnalysis JSON"] --> Gate

    Gate{"needs_debate?<br/>unanchored item, 6+ items,<br/>or band > 35%"}
    Gate -->|no| Lookup
    Gate -->|yes| Critic

    Critic["<b>MODEL CALL 2 - critic</b><br/>finds concrete problems"] --> HasCh
    HasCh{"challenges<br/>raised?"}
    HasCh -->|no| Lookup
    HasCh -->|yes| Reviser["<b>MODEL CALL 3 - reviser</b><br/>rules on each, re-emits analysis"]
    Reviser -->|truncated| Fallback["fall back to draft<br/>reviser_truncated logged"]
    Reviser --> Lookup
    Fallback --> Lookup

    Lookup["<b>Database match</b> - lookup.py<br/>menu items, then generic foods<br/>+ 2 retry normalizations"] --> Sanity

    Sanity["<b>Portion + sanity</b> - sanity.py<br/>unit-band clamp, gram clamp,<br/>uncertainty bands, per-item source"] --> Blend

    Blend["<b>Personal portions</b> - history<br/>shrink repeat foods toward<br/>your median (skips counted/branded)"] --> Cal

    Cal["<b>Calibration</b> - calibration.py<br/>x factor per source<br/>(1.0 until fitted)"] --> Total

    Total["Totals + report<br/>low / mid / high"] --> Out

    Out["Web JSON, CLI text,<br/>or benchmark record"] --> Diary
    Diary[("history.db<br/>pre-blend estimates,<br/>corrections, gold labels")]

    Diary -.->|"past portions"| Blend
    Diary -.->|"verified meals"| Fit["calibrate.py fit<br/>ridge least squares"]
    Fit -.->|"writes"| CalFile[("calibration.json")]
    CalFile -.-> Cal

    DB[("generic.json 132<br/>fastfood.json 26<br/>units.json 69")]
    DB -.-> Lookup
    DB -.-> Sanity

    style Vision fill:#1e56d6,color:#fff
    style Critic fill:#1e56d6,color:#fff
    style Reviser fill:#1e56d6,color:#fff
    style Lookup fill:#e8edf5
    style Sanity fill:#e8edf5
    style Cal fill:#e8edf5
```

Blue = costs money (API calls). Everything else is deterministic Python and
free to run, which is why most testing happens offline.

## Cost shape

One photo = 1–3 model calls. The vision call always runs; the critic runs when
`needs_debate` says the draft is uncertain (in practice ~95% of photos); the
reviser runs only if the critic actually raised something. Measured average:
**18.8¢/photo**, dominated by output tokens, so cost scales with scene
complexity (4.9¢ for two apples, 45.6¢ for a charcuterie board).

## Validation loop

```mermaid
flowchart LR
    Change["code or data change"] --> Offline

    Offline["<b>free, every commit</b><br/>312 pytest tests<br/>incl. collision audit<br/>over every DB alias"] --> Replay

    Replay["<b>free, on demand</b><br/>replay past sweep output<br/>through the new matcher"] --> Sweep

    Sweep["<b>paid</b><br/>benchmark.py run<br/>N photos x M runs<br/>--max-cost caps spend"] --> Report

    Report["benchmark.py report<br/>vs truth + vs baseline"] --> Gates

    Gates{"ship gates<br/>coverage, MAPE, bias,<br/>spread, wrong matches, cost"}
    Gates -->|pass| Ship["deploy"]
    Gates -->|fail| Revert["revert to last<br/>measured-good state"]

    style Sweep fill:#1e56d6,color:#fff
```

The rule that matters: **paid sweeps confirm, they don't discover.** Offline
tests and replays answer most questions for $0; the sweep exists to validate.
And **one variable per sweep** — tuning a prompt against a run that also
changed the model produced a wrong conclusion once already (see
`docs/KNOWLEDGE_BASE.md` → Known limitations).
