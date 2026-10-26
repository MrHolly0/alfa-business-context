import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from intelligence.llm.config import OPENROUTER_BASE, ProviderConfig, load_config


def config(model="google/gemma-4-26b-a4b-it", profile="hackathon", **overrides):
    values = {
        "base_url": OPENROUTER_BASE if profile == "openrouter" else "https://changellenge.up.railway.app/v1",
        "api_key_env": "HACKATHON_API_KEY",
        "model": model,
        "budget_scope": "team",
        "currency": "USD" if profile == "openrouter" else "RUB",
        "daily_limit": "500" if profile == "hackathon" else "20",
        "input_per_million": "0.1",
        "output_per_million": "0.2",
        "timeout_seconds": 15,
        "max_output_tokens": 1000,
    }
    values.update(overrides)
    return {"default_profile": profile, "profiles": {profile: values}}


class ConfigTests(unittest.TestCase):
    def load(self, data):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "providers.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return load_config(path)

    def test_loads_decimal_rates_and_never_resolves_secret(self):
        result = self.load(config())
        self.assertIsInstance(result, ProviderConfig)
        self.assertEqual(result.output_per_million, Decimal("0.2"))
        self.assertNotIn("secret", repr(result).lower())

    def test_accepts_openrouter_free_gemma_and_preserves_route_suffix(self):
        result = self.load(config("google/gemma-4-26b-a4b-it:free", "openrouter"))
        self.assertEqual(result.model, "google/gemma-4-26b-a4b-it:free")

    def test_direct_constructor_cannot_bypass_model_or_endpoint_validation(self):
        with self.assertRaisesRegex(ValueError, "approved"):
            ProviderConfig("hackathon", "https://evil.example/v1", "KEY", "google/gemma-4-26b-a4b-it",
                           "team", "RUB", Decimal("500"), Decimal("0"), Decimal("0"), 10, 100)
        with self.assertRaisesRegex(ValueError, "allowlist"):
            ProviderConfig("hackathon", "https://changellenge.up.railway.app/v1", "KEY", "other/model",
                           "team", "RUB", Decimal("500"), Decimal("0"), Decimal("0"), 10, 100)

    def test_rejects_unknown_model_and_wrong_base(self):
        with self.assertRaisesRegex(ValueError, "allowlist"):
            self.load(config("openai/gpt-4o"))
        with self.assertRaisesRegex(ValueError, "approved"):
            self.load(config(base_url="https://evil.example/v1"))

    def test_rejects_free_suffix_outside_openrouter_gemma(self):
        with self.assertRaisesRegex(ValueError, "only OpenRouter Gemma"):
            self.load(config("z-ai/glm-5.3-flash:free", "openrouter"))

    def test_rates_are_required_and_non_negative(self):
        with self.assertRaisesRegex(ValueError, "explicit finite decimal"):
            self.load(config(output_per_million=None))
        with self.assertRaisesRegex(ValueError, "finite and non-negative"):
            self.load(config(input_per_million="-1"))

    def test_hackathon_unknown_pricing_cannot_be_configured_as_zero(self):
        with self.assertRaisesRegex(ValueError, "zero cannot represent unknown pricing"):
            self.load(config(input_per_million="0"))
        with self.assertRaisesRegex(ValueError, "zero cannot represent unknown pricing"):
            self.load(config(output_per_million="0"))

    def test_fixed_reserve_is_explicit_and_keeps_unknown_rates_null(self):
        values = config()["profiles"]["hackathon"]
        values.update({"pricing_mode": "fixed_reserve", "attempt_reserve_rub": "10",
                       "max_calls_total": 50, "lifetime_limit": "500",
                       "input_per_million": None, "output_per_million": None})
        loaded = self.load({"default_profile": "hackathon", "profiles": {"hackathon": values}})
        self.assertEqual(loaded.pricing_mode, "fixed_reserve")
        self.assertEqual(loaded.attempt_reserve_rub, Decimal("10"))
        self.assertEqual(loaded.max_calls_total, 50)
        self.assertIsNone(loaded.input_per_million)
        self.assertIsNone(loaded.output_per_million)

    def test_fixed_reserve_requires_positive_reserve_and_finite_call_cap(self):
        values = config()["profiles"]["hackathon"]
        values.update({"pricing_mode": "fixed_reserve", "attempt_reserve_rub": "0",
                       "max_calls_total": 50, "input_per_million": None,
                       "output_per_million": None})
        with self.assertRaisesRegex(ValueError, "positive attempt_reserve_rub"):
            self.load({"default_profile": "hackathon", "profiles": {"hackathon": values}})
        values["attempt_reserve_rub"] = "10"
        values["max_calls_total"] = 51
        with self.assertRaisesRegex(ValueError, "hackathon max_calls_total must be from 1 to 50"):
            self.load({"default_profile": "hackathon", "profiles": {"hackathon": values}})

    def test_hackathon_limit_is_capped_at_500_rub(self):
        with self.assertRaisesRegex(ValueError, "500 RUB"):
            self.load(config(daily_limit="500.01"))

    def test_rejects_http_and_unapproved_currency(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.load(config(base_url="http://changellenge.up.railway.app/v1"))
        with self.assertRaisesRegex(ValueError, "currency"):
            self.load(config(currency="USD"))


if __name__ == "__main__":
    unittest.main()
