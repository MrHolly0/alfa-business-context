"""Exact Decimal pricing helpers for provider token tariffs."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _rate(value: Decimal, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative Decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite non-negative Decimal")
    return result


def _tokens(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class Pricing:
    """Rates are currency units per million tokens; estimation never uses floats."""

    input_per_million: Decimal
    output_per_million: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_per_million", _rate(self.input_per_million, "input_per_million"))
        object.__setattr__(self, "output_per_million", _rate(self.output_per_million, "output_per_million"))
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValueError("currency must be a non-empty string")
        object.__setattr__(self, "currency", self.currency.strip().upper())

    def estimate(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        prompt_tokens = _tokens(prompt_tokens, "prompt_tokens")
        completion_tokens = _tokens(completion_tokens, "completion_tokens")
        return (self.input_per_million * prompt_tokens + self.output_per_million * completion_tokens) / Decimal(1_000_000)
