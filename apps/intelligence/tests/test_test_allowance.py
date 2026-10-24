from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from intelligence.llm.ledger import BudgetExceeded, BudgetLedger


class LifetimeAllowanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = BudgetLedger(Path(self.temp.name) / "ledger.sqlite3")
        self.now = datetime.fromisoformat("2026-10-24T10:00:00+03:00")

    def tearDown(self):
        self.temp.cleanup()

    def test_lifetime_cap_counts_spend_across_daily_rollover(self):
        first = self.ledger.reserve("scope", "USD", "5", "0.7", now=self.now, lifetime_limit="1")
        self.ledger.settle(first, "0.7", "provider_reported", now=self.now)
        tomorrow = datetime.fromisoformat("2026-10-25T10:00:00+03:00")
        self.assertEqual(self.ledger.summary("scope", "USD", tomorrow)["spent"], Decimal(0))
        self.assertEqual(self.ledger.summary("scope", "USD", tomorrow)["lifetime_spent"], Decimal("0.7"))
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("scope", "USD", "5", "0.31", now=tomorrow, lifetime_limit="1")

    def test_concurrent_reservations_cannot_cross_lifetime_cap(self):
        def reserve(_):
            try:
                self.ledger.reserve("scope", "USD", "5", "0.6", now=self.now, lifetime_limit="1")
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(outcomes), 1)
        summary = self.ledger.summary("scope", "USD", self.now)
        self.assertEqual(summary["lifetime_reserved"], Decimal("0.6"))
        self.assertEqual(summary["lifetime_remaining"], Decimal("0.4"))

    def test_operator_limit_change_requires_current_values_and_preserves_usage(self):
        reservation = self.ledger.reserve("scope", "USD", "0.2", "0.1", now=self.now)
        self.ledger.settle(reservation, "0.1", "provider_reported", now=self.now)
        with self.assertRaisesRegex(ValueError, "expected values"):
            self.ledger.configure_limits("scope", "USD", daily_limit="5", lifetime_limit="5",
                expected_daily_limit="0.19", expected_lifetime_limit=None)
        self.ledger.configure_limits("scope", "USD", daily_limit="5", lifetime_limit="5",
            expected_daily_limit="0.2", expected_lifetime_limit=None)
        summary = self.ledger.summary("scope", "USD", self.now)
        self.assertEqual(summary["spent"], Decimal("0.1"))
        self.assertEqual(summary["lifetime_spent"], Decimal("0.1"))
        self.assertEqual(summary["lifetime_limit"], Decimal("5"))


if __name__ == "__main__":
    unittest.main()
