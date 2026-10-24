"""Budget-aware service boundary for a single explicit provider attempt."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

from .config import ProviderConfig
from .ledger import BudgetLedger
from .pricing import Pricing
from .transport import ChatCompletionTransport
from .types import Completion, ProviderError

DEFAULT_LEDGER_PATH = Path(__file__).resolve().parents[2] / ".state" / "llm-budget.sqlite3"


def estimate_text_tokens(messages: Sequence[Mapping[str, str]]) -> int:
    """Conservative text-only planning estimate: UTF-8 bytes plus 4 tokens/message.

    This does not run the provider's tokenizer and is not a hard server-side token cap.
    """
    return max(1, sum(len(m["content"].encode("utf-8")) + 4 for m in messages))


class LLMService:
    def __init__(self, config: ProviderConfig, *, ledger: BudgetLedger | None = None,
                 ledger_path: str | Path | None = None, transport=None,
                 projected_hackathon_pricing: Pricing | None = None,
                 projected_hackathon_model: str | None = None):
        config.ensure_spend_allowed()
        if ledger is not None and ledger_path is not None:
            raise ValueError("provide ledger or ledger_path, not both")
        if ledger is None:
            ledger = BudgetLedger(ledger_path or DEFAULT_LEDGER_PATH)
        self.config = config
        self.ledger = ledger
        self.transport = transport or ChatCompletionTransport(config)
        self.pricing = Pricing(config.input_per_million, config.output_per_million, config.currency)
        if projected_hackathon_pricing and projected_hackathon_pricing.currency != "RUB":
            raise ValueError("projected hackathon pricing must be in RUB")
        self.projected_hackathon_pricing = projected_hackathon_pricing
        self.projected_hackathon_model = projected_hackathon_model

    def complete(self, messages: Sequence[Mapping[str, str]], *, max_input_tokens: int,
                 max_output_tokens: int | None = None, json_mode: bool = False) -> dict:
        if isinstance(max_input_tokens, bool) or not isinstance(max_input_tokens, int) or max_input_tokens < 1:
            raise ValueError("max_input_tokens must be a positive integer")
        if not messages:
            raise ValueError("messages must be non-empty")
        for message in messages:
            if (not isinstance(message, Mapping) or set(message) != {"role", "content"}
                    or message["role"] not in {"system", "user", "assistant"}
                    or not isinstance(message["content"], str)):
                raise ValueError("each message must contain only a valid role and text content")
        estimated_input = estimate_text_tokens(messages)
        if estimated_input > max_input_tokens:
            raise ValueError("input exceeds the configured text-token estimate limit")
        output_cap = self.config.max_output_tokens if max_output_tokens is None else max_output_tokens
        if isinstance(output_cap, bool) or not isinstance(output_cap, int) or not (1 <= output_cap <= self.config.max_output_tokens):
            raise ValueError("max_output_tokens must be within the configured limit")

        # Reserve the full configured output cap before the network call. Token counts
        # are planning estimates; the configured cap also goes to the transport.
        reserve = self.pricing.estimate(max_input_tokens, output_cap)
        if self.config.is_paid_openrouter and reserve > self.config.max_request_cost:
            raise ValueError("estimated paid OpenRouter request cost exceeds max_request_cost")
        reservation_id = self.ledger.reserve(self.config.budget_scope, self.config.currency,
            self.config.daily_limit, reserve, request_id=str(uuid4()),
            lifetime_limit=self.config.lifetime_limit)
        try:
            result = self.transport.complete(messages, json_mode=json_mode, max_output_tokens=output_cap)
        except ProviderError as exc:
            if exc.billing_uncertain:
                usage = getattr(exc, "usage", None)
                if usage is None:
                    self.ledger.mark_uncertain(reservation_id)
                elif usage.reported_cost is not None and usage.cost_currency == self.config.currency:
                    usage_meta = asdict(usage)
                    usage_meta["reported_cost"] = str(usage.reported_cost)
                    self.ledger.settle(reservation_id, usage.reported_cost, "provider_reported", usage=usage_meta)
                elif usage.reported_cost is None and usage.prompt_tokens is not None and usage.completion_tokens is not None:
                    estimate = self.pricing.estimate(usage.prompt_tokens, usage.completion_tokens)
                    self.ledger.settle(reservation_id, estimate, "rate_estimate", usage=asdict(usage))
                else:
                    # Never put an unexpected provider currency into this profile's ledger.
                    self.ledger.mark_uncertain(reservation_id)
            else:
                # Missing key and other pre-request validation errors cannot be billed.
                self.ledger.settle(reservation_id, Decimal(0), "not_sent")
            raise
        except Exception:
            # Unknown transport errors could have happened after request submission.
            self.ledger.mark_uncertain(reservation_id)
            raise

        usage = result.usage
        if usage is None:
            self.ledger.mark_uncertain(reservation_id)
            cost = None
            cost_source = "unknown"
        elif usage.reported_cost is not None:
            if usage.cost_currency != self.config.currency:
                self.ledger.mark_uncertain(reservation_id)
                raise ProviderError("cost_currency_mismatch", "Provider cost currency does not match the configured budget currency", billing_uncertain=True)
            cost = usage.reported_cost
            cost_source = "provider_reported"
            usage_meta = asdict(usage)
            usage_meta["reported_cost"] = str(usage.reported_cost)
            self.ledger.settle(reservation_id, cost, cost_source, usage=usage_meta)
        elif usage.prompt_tokens is not None and usage.completion_tokens is not None:
            cost = self.pricing.estimate(usage.prompt_tokens, usage.completion_tokens)
            cost_source = "rate_estimate"
            self.ledger.settle(reservation_id, cost, cost_source, usage=asdict(usage))
        else:
            self.ledger.mark_uncertain(reservation_id)
            cost = None
            cost_source = "unknown"

        summary = self.ledger.summary(self.config.budget_scope, self.config.currency)
        report = {
            "content": result.content,
            "model": result.model,
            "profile": self.config.profile,
            "response_id": result.response_id,
            "finish_reason": result.finish_reason,
            "usage": asdict(usage) if usage is not None else None,
            "cost": cost,
            "cost_currency": self.config.currency if cost is not None else None,
            "cost_source": cost_source,
            "budget": summary,
        }
        same_model = (self.projected_hackathon_model is not None and
                      self.projected_hackathon_model.removesuffix(":free") == self.config.model.removesuffix(":free"))
        if (self.projected_hackathon_pricing and usage is not None and same_model
                and usage.prompt_tokens is not None and usage.completion_tokens is not None):
            projected = self.projected_hackathon_pricing.estimate(usage.prompt_tokens, usage.completion_tokens)
            report["projected_hackathon_cost_rub"] = projected
            report["projected_hackathon_daily_capacity"] = (
                int(Decimal("500") / projected) if projected > 0 else None
            )
            report["projected_hackathon_basis"] = {
                "model": self.projected_hackathon_model,
                "usage_prompt_tokens": usage.prompt_tokens,
                "usage_completion_tokens": usage.completion_tokens,
                "input_per_million_rub": str(self.projected_hackathon_pricing.input_per_million),
                "output_per_million_rub": str(self.projected_hackathon_pricing.output_per_million),
                "rates_are_operator_configured": True,
            }
        return report
