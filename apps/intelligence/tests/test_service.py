import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from intelligence.llm.config import ProviderConfig
from intelligence.llm.ledger import BudgetLedger
from intelligence.llm.pricing import Pricing
from intelligence.llm.service import LLMService
from intelligence.llm.types import Completion, ProviderError, Usage


def config():
    return ProviderConfig("openrouter", "https://openrouter.ai/api/v1", "UNUSED_KEY",
        "google/gemma-4-26b-a4b-it:free", "test-openrouter", "USD", Decimal("1"),
        Decimal("0.1"), Decimal("0.2"), 1.0, 100)


class FakeTransport:
    def __init__(self, result=None, error=None, callback=None):
        self.result, self.error, self.callback = result, error, callback

    def complete(self, messages, *, json_mode=False, max_output_tokens=None):
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        return self.result


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = BudgetLedger(Path(self.temp.name) / "budget.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_reserves_before_transport_and_uses_reported_currency_cost(self):
        seen = []
        transport = FakeTransport(
            Completion("answer", "google/gemma-4-26b-a4b-it:free",
                Usage(10, 20, reported_cost=Decimal("0.000003"), cost_currency="USD"), "stop", "id"),
            callback=lambda: seen.append(self.ledger.summary("test-openrouter", "USD")["reserved"]))
        service = LLMService(config(), ledger=self.ledger, transport=transport,
            projected_hackathon_pricing=Pricing(Decimal("2"), Decimal("5"), "RUB"),
            projected_hackathon_model=config().model)
        report = service.complete([{"role": "user", "content": "hello"}], max_input_tokens=100)
        self.assertGreater(seen[0], 0)
        self.assertEqual(report["cost_source"], "provider_reported")
        self.assertEqual(report["cost"], Decimal("0.000003"))
        self.assertEqual(report["cost_currency"], "USD")
        self.assertEqual(report["projected_hackathon_cost_rub"], Decimal("0.00012"))
        self.assertEqual(report["budget"]["spent"], Decimal("0.000003"))

    def test_estimates_missing_provider_cost_in_profile_currency(self):
        service = LLMService(config(), ledger=self.ledger,
            transport=FakeTransport(Completion("answer", "model", Usage(10, 20), "stop", None)))
        report = service.complete([{"role": "user", "content": "hello"}], max_input_tokens=100)
        self.assertEqual(report["cost_source"], "rate_estimate")
        self.assertEqual(report["cost"], Decimal("0.000005"))

    def test_missing_usage_retains_reservation_as_uncertain(self):
        service = LLMService(config(), ledger=self.ledger,
            transport=FakeTransport(Completion("answer", "model", None, "stop", None)))
        report = service.complete([{"role": "user", "content": "hello"}], max_input_tokens=100)
        self.assertIsNone(report["cost"])
        self.assertGreater(report["budget"]["reserved"], 0)

    def test_uncertain_provider_error_retains_reservation(self):
        service = LLMService(config(), ledger=self.ledger,
            transport=FakeTransport(error=ProviderError("timeout", "timed out", billing_uncertain=True)))
        with self.assertRaises(ProviderError):
            service.complete([{"role": "user", "content": "hello"}], max_input_tokens=100)
        self.assertGreater(self.ledger.summary("test-openrouter", "USD")["reserved"], 0)

    def test_response_error_with_usage_records_known_reported_cost(self):
        service = LLMService(config(), ledger=self.ledger,
            transport=FakeTransport(error=ProviderError("truncated_response", "truncated",
                billing_uncertain=True,
                usage=Usage(10, 20, reported_cost=Decimal("0.000003"), cost_currency="USD"))))
        with self.assertRaises(ProviderError):
            service.complete([{"role": "user", "content": "hello"}], max_input_tokens=100)
        summary = self.ledger.summary("test-openrouter", "USD")
        self.assertEqual(summary["spent"], Decimal("0.000003"))
        self.assertEqual(summary["reported_spent"], Decimal("0.000003"))
        self.assertEqual(summary["reserved"], 0)

    def test_provider_error_cost_without_token_counts_records_actual_charge(self):
        service = LLMService(config(), ledger=self.ledger,
            transport=FakeTransport(error=ProviderError("truncated_response", "truncated",
                billing_uncertain=True,
                usage=Usage(None, None, reported_cost=Decimal("0.000007"), cost_currency="USD"))))
        with self.assertRaises(ProviderError):
            service.complete([{"role": "user", "content": "hello"}], max_input_tokens=100)
        summary = self.ledger.summary("test-openrouter", "USD")
        self.assertEqual(summary["reported_spent"], Decimal("0.000007"))
        self.assertEqual(summary["reserved"], 0)

    def test_cost_without_token_counts_is_reported_but_not_projected(self):
        service = LLMService(config(), ledger=self.ledger,
            transport=FakeTransport(Completion("answer", "model",
                Usage(None, None, reported_cost=Decimal("0.000007"), cost_currency="USD"), "stop", None)),
            projected_hackathon_pricing=Pricing(Decimal("2"), Decimal("5"), "RUB"),
            projected_hackathon_model=config().model)
        report = service.complete([{"role": "user", "content": "hello"}], max_input_tokens=100)
        self.assertEqual(report["cost"], Decimal("0.000007"))
        self.assertNotIn("projected_hackathon_cost_rub", report)

    def test_usage_without_cost_or_tokens_retains_reservation(self):
        service = LLMService(config(), ledger=self.ledger,
            transport=FakeTransport(Completion("answer", "model", Usage(None, None), "stop", None)))
        report = service.complete([{"role": "user", "content": "hello"}], max_input_tokens=100)
        self.assertEqual(report["cost_source"], "unknown")
        self.assertGreater(report["budget"]["reserved"], 0)

    def test_rejects_input_over_limit_before_reserving(self):
        service = LLMService(config(), ledger=self.ledger,
            transport=FakeTransport(result=Completion("", "m", Usage(1, 1), None, None)))
        with self.assertRaises(ValueError):
            service.complete([{"role": "user", "content": "long prompt"}], max_input_tokens=3)
        self.assertEqual(self.ledger.summary("test-openrouter", "USD")["reserved"], 0)


if __name__ == "__main__":
    unittest.main()
