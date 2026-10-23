"""Stable provider-facing result and error types."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None = None
    reported_cost: Decimal | None = None
    cost_currency: str | None = None


@dataclass(frozen=True, slots=True)
class Completion:
    content: str
    model: str | None
    usage: Usage | None
    finish_reason: str | None
    response_id: str | None


class ProviderError(RuntimeError):
    """Sanitized provider/response error; never includes body, headers, or key."""

    def __init__(self, code: str, message: str, *, billing_uncertain: bool = False,
                 usage: Usage | None = None):
        super().__init__(message)
        self.code = code
        self.billing_uncertain = billing_uncertain
        self.usage = usage
