"""Cross-process Postgres budget serialization; requires migrated test DSN."""
from datetime import datetime, timezone
from decimal import Decimal
import multiprocessing
import os
import uuid

import pytest

from intelligence.llm.ledger import BudgetExceeded, BudgetLedger


def _reserve_process(scope: str, start, barrier, outcomes):
    ledger = BudgetLedger("parallel-budget-test")
    barrier.wait(timeout=20)
    try:
        ledger.reserve(scope, "RUB", Decimal("100"), Decimal("1"), now=start, max_calls=3)
        outcomes.put(True)
    except BudgetExceeded:
        outcomes.put(False)


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_DATABASE_URL"), reason="Postgres integration DSN is not configured")
def test_postgres_call_cap_is_atomic_across_processes():
    scope = "test-pg-process-" + uuid.uuid4().hex
    start = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(8)
    outcomes_queue = context.Queue()
    processes = [context.Process(target=_reserve_process, args=(scope, start, barrier, outcomes_queue))
                 for _ in range(8)]
    for process in processes:
        process.start()
    outcomes = [outcomes_queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert sum(outcomes) == 3
    summary = BudgetLedger("parallel-budget-test").summary(scope, "RUB", start)
    assert summary["reserved"] == Decimal("3")
    assert summary["reservation_count"] == 3
