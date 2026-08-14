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

# Photos with at most this many items, all at high confidence, skip the
# adversarial debate: measured, the critic never found fault with them.
SKIP_DEBATE_MAX_ITEMS = 3
