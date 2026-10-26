"""Fixed-reserve billing remains honest when the gateway omits tariffs."""
from decimal import Decimal
import unittest

from intelligence.llm.config import ProviderConfig
from intelligence.llm.service import LLMService
from intelligence.llm.transport import ChatCompletionTransport
from intelligence.llm.types import Completion, Usage


def config():
    return ProviderConfig("hackathon", "https://changellenge.up.railway.app/v1", "UNUSED_TEST_KEY",
        "google/gemma-4-26b-a4b-it", "test-fixed-reserve", "RUB", Decimal("500"), None, None,
        3, 256, False, None, Decimal("500"), None, "fixed_reserve", Decimal("10"), 50)


class Ledger:
    def __init__(self):
        self.calls = []
        self.settlements = []
        self.uncertain = []

    def reserve(self, scope, currency, limit, amount, request_id=None, *, lifetime_limit=None, max_total_calls=None):
        self.calls.append({"scope": scope, "currency": currency, "limit": limit, "amount": amount,
                           "lifetime_limit": lifetime_limit, "max_total_calls": max_total_calls})
        return "reservation-1"

    def settle(self, reservation_id, amount, cost_source, usage=None):
        self.settlements.append((reservation_id, amount, cost_source, usage))

    def mark_uncertain(self, reservation_id):
        self.uncertain.append(reservation_id)

    def summary(self, scope, currency):
        return {"scope": scope, "currency": currency, "limit": Decimal("500"),
                "spent": self.settlements[-1][1] if self.settlements else Decimal("0"),
                "reserved": Decimal("10") if self.uncertain else Decimal("0"),
                "uncertain_count": len(self.uncertain)}


class Transport:
    def __init__(self, result):
        self.result = result

    def complete(self, *_args, **_kwargs):
        return self.result


class FixedReserveTests(unittest.TestCase):
    def test_unknown_token_usage_keeps_fixed_reserve_and_never_estimates_zero(self):
        ledger = Ledger()
        service = LLMService(config(), ledger=ledger,
            transport=Transport(Completion("synthetic result", "google/gemma-4-26b-a4b-it",
                Usage(40, 20), "stop", "response-1")))
        report = service.complete([{"role": "user", "content": "synthetic test"}], max_input_tokens=100)
        self.assertEqual(ledger.calls[0]["amount"], Decimal("10"))
        self.assertEqual(ledger.calls[0]["max_total_calls"], 50)
        self.assertEqual(ledger.calls[0]["lifetime_limit"], Decimal("500"))
        self.assertEqual(ledger.uncertain, ["reservation-1"])
        self.assertEqual(ledger.settlements, [])
        self.assertIsNone(report["cost"])
        self.assertEqual(report["cost_source"], "unknown")
        self.assertEqual(report["budget"]["reserved"], Decimal("10"))

    def test_explicit_rub_cost_settles_actual_charge(self):
        ledger = Ledger()
        service = LLMService(config(), ledger=ledger,
            transport=Transport(Completion("synthetic result", "google/gemma-4-26b-a4b-it",
                Usage(40, 20, reported_cost=Decimal("0.42"), cost_currency="RUB"), "stop", "response-2")))
        report = service.complete([{"role": "user", "content": "synthetic test"}], max_input_tokens=100)
        self.assertEqual(ledger.settlements[0][1:3], (Decimal("0.42"), "provider_reported"))
        self.assertEqual(ledger.uncertain, [])
        self.assertEqual(report["cost"], Decimal("0.42"))
        self.assertEqual(report["cost_currency"], "RUB")

    def test_foreign_reported_cost_keeps_content_and_rub_reserve_uncertain(self):
        ledger = Ledger()
        service = LLMService(config(), ledger=ledger,
            transport=Transport(Completion("synthetic result", "google/gemma-4-26b-a4b-it",
                Usage(40, 20, reported_cost=Decimal("0.0042"), cost_currency="USD"), "stop", "response-3")))
        report = service.complete([{"role": "user", "content": "synthetic test"}], max_input_tokens=100)
        self.assertEqual(report["content"], "synthetic result")
        self.assertIsNone(report["cost"])
        self.assertEqual(report["cost_source"], "unknown")
        self.assertEqual(report["external_billing"], {"reported_cost": "0.0042", "currency": "USD",
            "source": "provider_reported_unconverted"})
        self.assertEqual(ledger.uncertain, ["reservation-1"])
        self.assertEqual(ledger.settlements, [])
        self.assertEqual(report["budget"]["reserved"], Decimal("10"))

    def test_transport_only_accepts_gateway_cost_with_explicit_rub_currency(self):
        transport = ChatCompletionTransport(config(), opener=object())
        usage = transport._parse_usage({"prompt_tokens": 2, "completion_tokens": 3,
            "cost": "0.3", "cost_currency": "RUB"})
        self.assertEqual(usage.reported_cost, Decimal("0.3"))
        self.assertEqual(usage.cost_currency, "RUB")
        unknown = transport._parse_usage({"prompt_tokens": 2, "completion_tokens": 3, "cost": "0.3"})
        self.assertIsNone(unknown.reported_cost)
        self.assertIsNone(unknown.cost_currency)
        usd = transport._parse_usage({"prompt_tokens": 2, "completion_tokens": 3,
            "cost": "0.003", "cost_currency": "USD"})
        self.assertEqual(usd.reported_cost, Decimal("0.003"))
        self.assertEqual(usd.cost_currency, "USD")


if __name__ == "__main__":
    unittest.main()
