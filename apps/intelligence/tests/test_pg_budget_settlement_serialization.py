"""A provider settlement and a competing reservation share one account lock."""
import os
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from intelligence.llm.ledger import BudgetExceeded, BudgetLedger
from intelligence.storage import Session


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_DATABASE_URL"), reason="Postgres integration DSN is not configured")
def test_reservation_waits_for_concurrent_settlement_accounting(tmp_path, monkeypatch):
    scope = "test-settlement-order-" + uuid.uuid4().hex
    ledger = BudgetLedger(tmp_path / "budget.sqlite3")
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    reservation_id = ledger.reserve(scope, "RUB", Decimal("10"), Decimal("1"), now=now)

    account_lock_acquired = threading.Event()
    release_settlement = threading.Event()
    reserve_finished = threading.Event()
    failures = []
    reserve_outcome = []
    original_lock = Session.lock

    def pause_after_account_lock(session, key="write"):
        original_lock(session, key)
        if key == "budget:" + scope:
            account_lock_acquired.set()
            if not release_settlement.wait(10):
                raise TimeoutError("test_release_settlement_timeout")

    monkeypatch.setattr(Session, "lock", pause_after_account_lock)

    def settle():
        try:
            ledger.settle(reservation_id, Decimal("8"), "provider_reported", now=now)
        except Exception as exc:
            failures.append(exc)

    def reserve_again():
        try:
            ledger.reserve(scope, "RUB", Decimal("10"), Decimal("3"), now=now)
            reserve_outcome.append("accepted")
        except BudgetExceeded:
            reserve_outcome.append("blocked")
        except Exception as exc:
            failures.append(exc)
        finally:
            reserve_finished.set()

    settlement_thread = threading.Thread(target=settle)
    settlement_thread.start()
    assert account_lock_acquired.wait(10), "settlement did not acquire the account lock"
    reserve_thread = threading.Thread(target=reserve_again)
    reserve_thread.start()
    try:
        assert not reserve_finished.wait(0.2), "reserve passed the held settlement account lock"
    finally:
        release_settlement.set()
    settlement_thread.join(10)
    reserve_thread.join(10)

    assert not settlement_thread.is_alive() and not reserve_thread.is_alive()
    assert failures == []
    assert reserve_outcome == ["blocked"]
    summary = ledger.summary(scope, "RUB", now)
    assert summary["spent"] == Decimal("8")
    assert summary["reserved"] == Decimal(0)
