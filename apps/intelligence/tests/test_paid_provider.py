import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from intelligence.llm.config import OPENROUTER_BASE, ProviderConfig, load_config
from intelligence.llm.ledger import BudgetLedger, BudgetExceeded
from intelligence.llm.service import LLMService
from intelligence.llm.transport import ChatCompletionTransport
from intelligence.llm.types import Completion, Usage

MODEL = "google/gemma-4-26b-a4b-it"


def paid_config(**overrides):
    values = dict(profile="openrouter", base_url=OPENROUTER_BASE, api_key_env="PAID_TEST_KEY",
        model=MODEL, budget_scope="paid-test", currency="USD", daily_limit=Decimal("0.20"),
        input_per_million=Decimal("10"), output_per_million=Decimal("20"), timeout_seconds=2,
        max_output_tokens=2048, allow_paid=True, max_request_cost=Decimal("0.01"))
    values.update(overrides)
    return ProviderConfig(**values)


class Response:
    def __init__(self, payload): self.payload = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, amount=-1): return self.payload[:amount]


class Opener:
    def __init__(self, payload=None):
        self.payload = payload or {"model": MODEL, "choices": [{"message": {"content": "ok"}}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": "0"}}
        self.request = None
    def open(self, request, timeout):
        self.request = request
        return Response(self.payload)


class FakeTransport:
    def __init__(self, usage): self.usage, self.calls = usage, 0
    def complete(self, messages, *, json_mode=False, max_output_tokens=None):
        self.calls += 1
        time.sleep(0.1)
        return Completion("ok", MODEL, self.usage, "stop", "id")


class PaidProviderTests(unittest.TestCase):
    def test_paid_requires_explicit_consent_and_safe_caps(self):
        for kwargs in (
            {"allow_paid": False}, {"daily_limit": Decimal("0.2001")},
            {"daily_limit": Decimal("0")}, {"max_request_cost": None},
            {"max_request_cost": Decimal("0.0101")},
            {"max_request_cost": Decimal("0.02"), "daily_limit": Decimal("0.02")},
            {"input_per_million": Decimal("0")},
        ):
            with self.assertRaises(ValueError): paid_config(**kwargs).ensure_spend_allowed()
        with self.assertRaisesRegex(ValueError, "allow_paid"):
            paid_config(allow_paid="true")
        with self.assertRaisesRegex(ValueError, "finite"):
            paid_config(max_request_cost="NaN")
        with self.assertRaisesRegex(ValueError, "allowlist"):
            paid_config(model="unknown/model")

    def test_paid_route_includes_provider_price_caps(self):
        opener = Opener()
        with patch.dict(os.environ, {"PAID_TEST_KEY": "not-a-real-key"}):
            ChatCompletionTransport(paid_config(), opener).complete([{"role": "user", "content": "hi"}])
        body = json.loads(opener.request.data)
        self.assertEqual(body["provider"], {"sort": "price", "require_parameters": True,
            "max_price": {"prompt": 10.0, "completion": 20.0, "request": 0}})

    def test_service_rejects_oversized_reservation_before_ledger_or_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = BudgetLedger(Path(temp) / "budget.sqlite3")
            transport = FakeTransport(Usage(1, 1))
            service = LLMService(paid_config(input_per_million=Decimal("100000")), ledger=ledger, transport=transport)
            with self.assertRaisesRegex(ValueError, "exceeds max_request_cost"):
                service.complete([{"role": "user", "content": "x"}], max_input_tokens=100, max_output_tokens=1)
            summary = ledger.summary("paid-test", "USD")
            self.assertEqual(summary["reserved"], 0)
            self.assertEqual(summary["reservation_count"], 0)
            self.assertEqual(transport.calls, 0)

    def test_unrepresentable_price_rates_fail_before_ledger_reservation(self):
        for rate in (Decimal("1e999"), Decimal("1e-999")):
            with tempfile.TemporaryDirectory() as temp:
                ledger = BudgetLedger(Path(temp) / "budget.sqlite3")
                transport = FakeTransport(Usage(1, 1))
                with self.assertRaisesRegex(ValueError, "representable as finite positive JSON"):
                    LLMService(paid_config(input_per_million=rate), ledger=ledger, transport=transport)
                self.assertEqual(ledger.summary("paid-test", "USD")["reservation_count"], 0)
                self.assertEqual(transport.calls, 0)

    def test_daily_limit_is_shared_across_concurrent_services(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "budget.sqlite3"
            cfg = paid_config(daily_limit=Decimal("0.0003"), max_request_cost=Decimal("0.00025"),
                              input_per_million=Decimal("1"), output_per_million=Decimal("1"))
            services = [LLMService(cfg, ledger_path=path, transport=FakeTransport(Usage(1, 1))) for _ in range(2)]
            def call(service):
                try:
                    service.complete([{"role": "user", "content": "x"}], max_input_tokens=100, max_output_tokens=100)
                    return "ok"
                except BudgetExceeded:
                    return "blocked"
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(call, services))
            self.assertEqual(sorted(outcomes), ["blocked", "ok"])
            summary = services[0].ledger.summary("paid-test", "USD")
            self.assertLessEqual(summary["spent"] + summary["reserved"], cfg.daily_limit)

    def test_unknown_usage_keeps_reservation_and_config_loads_optional_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = BudgetLedger(Path(temp) / "budget.sqlite3")
            service = LLMService(paid_config(), ledger=ledger, transport=FakeTransport(None))
            report = service.complete([{"role": "user", "content": "x"}], max_input_tokens=100, max_output_tokens=10)
            self.assertEqual(report["cost_source"], "unknown")
            self.assertGreater(report["budget"]["reserved"], 0)

        profile = {"base_url": OPENROUTER_BASE, "api_key_env": "KEY", "model": MODEL,
            "budget_scope": "diag", "currency": "USD", "daily_limit": "0.20",
            "input_per_million": "1", "output_per_million": "1", "timeout_seconds": 2,
            "max_output_tokens": 20, "allow_paid": False, "max_request_cost": "0.01"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "providers.json"
            path.write_text(json.dumps({"default_profile": "openrouter", "profiles": {"openrouter": profile}}))
            loaded = load_config(path)
            self.assertFalse(loaded.allow_paid)
            self.assertEqual(loaded.max_request_cost, Decimal("0.01"))
            with self.assertRaisesRegex(ValueError, "allow_paid"):
                LLMService(loaded, ledger_path=Path(temp) / "ledger.sqlite3")


if __name__ == "__main__": unittest.main()
