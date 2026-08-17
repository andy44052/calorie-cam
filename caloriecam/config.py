"""Central knobs for the pipeline."""

DEFAULT_MODEL = "claude-opus-5"

# Photos are downscaled to this long-edge size before upload. 1568px keeps the
# image around ~1.2-1.6k tokens; the API accepts larger but cost rises with it.
MAX_IMAGE_PX = 1568
JPEG_QUALITY = 85

# Room for adaptive thinking + the structured answer (thinking counts toward
# max_tokens on Opus 5).
MAX_TOKENS = 16000

# User-typed context ("cooked in olive oil", "double chicken") is capped so a
# giant paste can't blow up the prompt.
HINT_MAX_CHARS = 500

# Adaptive debate gate: a photo earns the skeptic pass when the draft has an
# item with no database anchor, at least this many items (misses get likely),
# or a total uncertainty band wider than this fraction of the midpoint.
# Defaults from the improvement spec; calibrate against benchmark data once a
# sweep with per-verdict records exists.
DEBATE_MIN_ITEMS = 6
DEBATE_WIDTH_TRIGGER = 0.35
