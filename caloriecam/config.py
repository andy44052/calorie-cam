"""Central knobs for the pipeline."""

DEFAULT_MODEL = "claude-opus-5"

# Photos are downscaled to this long-edge size before upload. 1568px keeps the
# image around ~1.2-1.6k tokens; the API accepts larger but cost rises with it.
MAX_IMAGE_PX = 1568
JPEG_QUALITY = 85

# Room for adaptive thinking + the structured answer (thinking counts toward
# max_tokens on Opus 5).
MAX_TOKENS = 16000
