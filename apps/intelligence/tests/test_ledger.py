from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from datetime import datetime
from decimal import Decimal
import tempfile
from pathlib import Path
import unittest

from intelligence.llm.ledger import BudgetExceeded, BudgetLedger, CurrencyMismatch, ReservationError


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "budget.sqlite3"
        self.ledger = BudgetLedger(self.path)
        self.now = datetime.fromisoformat("2026-10-23T10:00:00+03:00")

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, amount="1", scope="dev", currency="RUB", limit="10", now=None):
        return self.ledger.reserve(scope, currency, Decimal(limit), Decimal(amount), now=now or self.now)

    def test_settlement_idempotent_and_persistent_after_restart(self):
        reservation = self.reserve()
        restarted = BudgetLedger(self.path)
        restarted.settle(reservation, Decimal("0.75"), "provider_reported", {"prompt_tokens": 4}, self.now)
        restarted.settle(reservation, Decimal("0.75"), "provider_reported", {"prompt_tokens": 4}, self.now)
        summary = BudgetLedger(self.path).summary("dev", "RUB", self.now)
        self.assertEqual(summary["spent"], Decimal("0.75"))
        self.assertEqual(summary["reserved"], Decimal(0))
        with self.assertRaises(ReservationError):
            restarted.settle(reservation, Decimal("0.5"), "rate_estimate", {"prompt_tokens": 4}, self.now)

    def test_uncertain_retains_full_reservation_and_blocks_excess(self):
        reservation = self.reserve("8", limit="10")
        self.ledger.mark_uncertain(reservation)
        summary = self.ledger.summary("dev", "RUB", self.now)
        self.assertEqual(summary["reserved"], Decimal("8"))
        with self.assertRaises(BudgetExceeded):
            self.reserve("3", limit="10")

    def test_daily_boundary_is_03_moscow(self):
        before = datetime.fromisoformat("2026-10-23T02:59:00+03:00")
        after = datetime.fromisoformat("2026-10-23T03:00:00+03:00")
        reservation = self.reserve("2", now=before)
        old = self.ledger.summary("dev", "RUB", before)
        self.assertEqual(old["spent"], Decimal(0))
        self.ledger.settle(reservation, Decimal("1.25"), "rate_estimate", now=before)
        self.assertEqual(self.ledger.summary("dev", "RUB", before)["spent"], Decimal("1.25"))
        self.assertEqual(self.ledger.summary("dev", "RUB", after)["spent"], Decimal(0))

    def test_unresolved_reservation_survives_rollover(self):
        before = datetime.fromisoformat("2026-10-23T02:59:00+03:00")
        after = datetime.fromisoformat("2026-10-23T03:00:00+03:00")
        reservation = self.reserve("4", limit="10", now=before)
        self.assertEqual(self.ledger.summary("dev", "RUB", after)["reserved"], Decimal("4"))
        self.ledger.settle(reservation, Decimal("3"), "provider_reported", now=after)
        self.assertEqual(self.ledger.summary("dev", "RUB", after)["reserved"], Decimal(0))

    def test_scope_isolation_and_currency_cannot_change_within_scope(self):
        self.reserve("9", scope="dev", limit="10")
        self.reserve("9", scope="hackathon", currency="USD", limit="10")
        with self.assertRaises(CurrencyMismatch):
            self.reserve("1", scope="dev", currency="USD", limit="10")
        self.assertEqual(self.ledger.summary("hackathon", "USD", self.now)["reserved"], Decimal("9"))

    def test_over_limit_actual_cost_is_recorded_and_blocks_future_reserve(self):
        reservation = self.reserve("2", limit="5")
        self.ledger.settle(reservation, Decimal("6"), "provider_reported", now=self.now)
        self.assertEqual(self.ledger.summary("dev", "RUB", self.now)["spent"], Decimal("6"))
        with self.assertRaises(BudgetExceeded):
            self.reserve("0.01", limit="5")

    def test_concurrent_reservations_are_serialized(self):
        def attempt(_):
            try:
                return self.reserve("2", limit="10")
            except BudgetExceeded:
                return None
        with ThreadPoolExecutor(max_workers=10) as pool:
            result = list(pool.map(attempt, range(10)))
        self.assertEqual(sum(item is not None for item in result), 5)
        self.assertEqual(self.ledger.summary("dev", "RUB", self.now)["reserved"], Decimal("10"))

    def test_lifetime_call_limit_counts_settled_and_uncertain_attempts(self):
        first = self.ledger.reserve("finite-calls", "RUB", Decimal("500"), Decimal("10"),
            now=self.now, lifetime_limit=Decimal("500"), max_total_calls=1)
        self.ledger.mark_uncertain(first, self.now)
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("finite-calls", "RUB", Decimal("500"), Decimal("10"),
                now=self.now, lifetime_limit=Decimal("500"), max_total_calls=1)

    def test_zero_tariff_and_zero_budget_are_allowed(self):
        reservation = self.ledger.reserve("dev", "RUB", Decimal(0), Decimal(0), now=self.now)
        self.ledger.settle(reservation, Decimal(0), "rate_estimate", now=self.now)
        summary = self.ledger.summary("dev", "RUB", self.now)
        self.assertEqual(summary["settled_count"], 1)
        self.assertEqual(summary["estimated_spent"], Decimal(0))
        self.assertFalse(summary["blocked"])
        with self.assertRaises(BudgetExceeded):
            self.reserve("0.01", limit="0")

    def test_summary_separates_reported_and_estimated_cost(self):
        reported = self.reserve("1")
        estimated = self.reserve("1")
        self.ledger.settle(reported, Decimal("0.3"), "provider_reported", now=self.now)
        self.ledger.settle(estimated, Decimal("0.2"), "rate_estimate", now=self.now)
        summary = self.ledger.summary("dev", "RUB", self.now)
        self.assertEqual(summary["reported_spent"], Decimal("0.3"))
        self.assertEqual(summary["estimated_spent"], Decimal("0.2"))
        self.assertEqual(summary["reservation_count"], 2)
        self.assertEqual(summary["settled_count"], 2)

    def test_in_memory_database_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "in-memory"):
            BudgetLedger(":memory:")

    @staticmethod
    def _process_reserve(path, barrier, queue):
        ledger = BudgetLedger(path)
        barrier.wait()
        try:
            ledger.reserve("process-test", "RUB", Decimal("10"), Decimal("2"))
            queue.put(True)
        except BudgetExceeded:
            queue.put(False)

    def test_multiple_processes_share_atomic_budget_account(self):
        ctx = multiprocessing.get_context("spawn")
        barrier, queue = ctx.Barrier(8), ctx.Queue()
        processes = [ctx.Process(target=self._process_reserve, args=(str(self.path), barrier, queue)) for _ in range(8)]
        for process in processes:
            process.start()
        outcomes = [queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(outcomes), 5)
        summary = self.ledger.summary("process-test", "RUB")
        self.assertEqual(summary["reserved"], Decimal("10"))

    def test_bad_now_and_missing_reservation_are_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.summary("dev", "RUB", datetime(2026, 10, 23, 10))
        with self.assertRaises(ReservationError):
            self.ledger.settle("missing", Decimal(1), "provider_reported")


if __name__ == "__main__":
    unittest.main()
