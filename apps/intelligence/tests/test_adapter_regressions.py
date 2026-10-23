"""Offline end-to-end checks of transport and spend accounting."""

from dataclasses import replace
from decimal import Decimal
import http.client
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from intelligence.llm.config import ProviderConfig
from intelligence.llm.ledger import BudgetExceeded, BudgetLedger
from intelligence.llm.service import LLMService
from intelligence.llm.transport import ChatCompletionTransport
from intelligence.llm.types import ProviderError


class Response:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        if self.error:
            raise self.error
        return self.body[:limit]


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.calls = 0
        self.request = None

    def open(self, request, timeout):
        self.calls += 1
        self.request = request
        if self.error:
            raise self.error
        return self.response


class AdapterRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = BudgetLedger(Path(self.temp.name) / "billing.sqlite3")
        self.config = ProviderConfig(
            "openrouter", "https://openrouter.ai/api/v1", "REGRESSION_KEY",
            "google/gemma-4-26b-a4b-it:free", "dev", "USD",
            Decimal("1"), Decimal("1"), Decimal("1"), 1, 100,
        )
        self.env = patch.dict(os.environ, {"REGRESSION_KEY": "test-secret-not-real"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def service(self, opener, config=None):
        config = config or self.config
        return LLMService(config, ledger=self.ledger,
                          transport=ChatCompletionTransport(config, opener))

    def call(self, service):
        return service.complete([{"role": "user", "content": "test"}], max_input_tokens=10)

    def test_timeout_has_one_attempt_and_keeps_reserved_cost(self):
        opener = Opener(error=URLError("test-secret-not-real"))
        with self.assertRaises(ProviderError) as caught:
            self.call(self.service(opener))
        self.assertNotIn("test-secret-not-real", str(caught.exception))
        self.assertEqual(opener.calls, 1)
        summary = self.ledger.summary("dev", "USD")
        self.assertEqual(summary["reserved"], Decimal("0.00011"))
        self.assertEqual(summary["uncertain_count"], 1)

    def test_budget_block_prevents_any_network_attempt(self):
        config = replace(self.config, daily_limit=Decimal("0.00001"))
        opener = Opener(error=AssertionError("must not send"))
        with self.assertRaises(BudgetExceeded):
            self.call(self.service(opener, config))
        self.assertEqual(opener.calls, 0)

    def test_free_route_is_sent_and_zero_tariff_is_trackable(self):
        body = json.dumps({
            "model": "google/gemma-4-26b-a4b-it",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "cost": 0},
        }).encode()
        opener = Opener(response=Response(body))
        config = replace(self.config, daily_limit=Decimal(0),
                         input_per_million=Decimal(0), output_per_million=Decimal(0))
        report = self.call(self.service(opener, config))
        self.assertEqual(json.loads(opener.request.data)["model"], config.model)
        self.assertEqual(report["cost"], Decimal(0))
        self.assertEqual(report["budget"]["settled_count"], 1)
        self.assertEqual(report["budget"]["reserved"], Decimal(0))

    def test_manual_hackathon_endpoint_switch_shares_pending_budget(self):
        config = replace(self.config, profile="hackathon",
                         base_url="https://changellenge.up.railway.app/v1",
                         model="google/gemma-4-26b-a4b-it", currency="RUB",
                         budget_scope="team", daily_limit=Decimal("0.00015"))
        main = Opener(error=TimeoutError())
        with self.assertRaises(ProviderError):
            self.call(self.service(main, config))
        backup = Opener(error=AssertionError("must not bypass the budget"))
        backup_config = replace(config, base_url="https://api.alfahack-gateway26.ru/v1")
        with self.assertRaises(BudgetExceeded):
            self.call(self.service(backup, backup_config))
        self.assertEqual(main.calls, 1)
        self.assertEqual(backup.calls, 0)

    def test_incomplete_http_read_is_sanitized_and_not_free(self):
        opener = Opener(response=Response(error=http.client.IncompleteRead(b"private body")))
        with self.assertRaises(ProviderError) as caught:
            self.call(self.service(opener))
        self.assertNotIn("private body", str(caught.exception))
        self.assertEqual(self.ledger.summary("dev", "USD")["uncertain_count"], 1)

    def test_rate_limit_response_does_not_retry(self):
        opener = Opener(error=HTTPError(self.config.base_url, 429, "private body", {}, None))
        with self.assertRaises(ProviderError) as caught:
            self.call(self.service(opener))
        self.assertEqual(opener.calls, 1)
        self.assertEqual(caught.exception.code, "provider_http_error")
        self.assertNotIn("private body", str(caught.exception))
        self.assertEqual(self.ledger.summary("dev", "USD")["uncertain_count"], 1)


if __name__ == "__main__":
    unittest.main()
