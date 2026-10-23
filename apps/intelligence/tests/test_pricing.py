import unittest
from decimal import Decimal

from intelligence.llm.pricing import Pricing


class PricingTests(unittest.TestCase):
    def test_estimate_uses_decimal_per_million_rates(self):
        pricing = Pricing(Decimal("2.50"), Decimal("10"), "rub")
        self.assertEqual(pricing.estimate(100_000, 20_000), Decimal("0.45"))
        self.assertEqual(pricing.currency, "RUB")

    def test_rejects_invalid_rates_and_token_counts(self):
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-0.1"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Pricing(value, Decimal("1"), "USD")
        pricing = Pricing(Decimal("0"), Decimal("0"), "USD")
        for value in (-1, True, 1.5, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pricing.estimate(value, 0)


if __name__ == "__main__":
    unittest.main()
