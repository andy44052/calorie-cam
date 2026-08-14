"""Per-call token/cost accounting.

Every API call records what it spent so cost work targets the real dominant
line instead of a guess. Prices are USD per million tokens; update when
Anthropic pricing changes.
"""

import threading
from dataclasses import dataclass, field

# (input, output) USD per million tokens.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
DEFAULT_PRICE = (5.0, 25.0)

# Cached-prefix reads bill at a fraction of the input rate; writes at a premium.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class CallUsage:
    stage: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0

    @property
    def cost_usd(self) -> float:
        price_in, price_out = PRICES.get(self.model, DEFAULT_PRICE)
        return (
            self.input_tokens * price_in
            + self.cache_read_tokens * price_in * CACHE_READ_MULTIPLIER
            + self.cache_write_tokens * price_in * CACHE_WRITE_MULTIPLIER
            + self.output_tokens * price_out
        ) / 1_000_000

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "seconds": round(self.seconds, 1),
            "cost_usd": round(self.cost_usd, 5),
        }


@dataclass
class UsageLedger:
    """Collects the calls made for one photo."""

    calls: list[CallUsage] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, usage: CallUsage) -> None:
        with self._lock:
            self.calls.append(usage)

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    def as_dict(self) -> dict:
        return {
            "calls": [c.as_dict() for c in self.calls],
            "call_count": len(self.calls),
            "input_tokens": sum(c.input_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "cache_read_tokens": sum(c.cache_read_tokens for c in self.calls),
            "total_cost_usd": round(self.total_cost_usd, 5),
            "total_seconds": round(sum(c.seconds for c in self.calls), 1),
        }


def from_response(response, stage: str, model: str, seconds: float) -> CallUsage:
    """Build a CallUsage from an SDK response's usage block (absent -> zeros)."""
    usage = getattr(response, "usage", None)
    return CallUsage(
        stage=stage,
        model=model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        seconds=seconds,
    )
