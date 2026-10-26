"""Refuse a legacy budget file until its saved spend is imported."""
import hashlib
import os
import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from intelligence.llm.ledger import BudgetLedger
from intelligence.sqlite_import import import_database
from intelligence.storage import Database


def _legacy_ledger(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE accounts (
                scope TEXT PRIMARY KEY, currency TEXT NOT NULL, limit_amount TEXT NOT NULL,
                lifetime_limit TEXT
            );
            CREATE TABLE reservations (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, currency TEXT NOT NULL,
                period_start TEXT NOT NULL, reserved_amount TEXT NOT NULL, actual_amount TEXT,
                cost_source TEXT, request_id TEXT, status TEXT NOT NULL, usage_json TEXT,
                created_at TEXT NOT NULL, settled_at TEXT
            );
        """)
        db.execute("INSERT INTO accounts VALUES('legacy-paid-scope','RUB','10','25')")
        db.execute("""INSERT INTO reservations
            VALUES('legacy-reservation','legacy-paid-scope','RUB','2026-10-25T03:00:00+03:00',
            '1','0.75','provider_reported','legacy-request','settled','{}',
            '2026-10-25T10:00:00+03:00','2026-10-25T10:01:00+03:00')""")


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_DATABASE_URL"), reason="Postgres integration DSN is not configured")
def test_existing_sqlite_budget_blocks_cutover_until_explicit_import(tmp_path):
    path = tmp_path / "budget.sqlite3"
    _legacy_ledger(path)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    database = Database("budget", path)

    with database.connection() as db:
        assert db.execute(
            "SELECT count(*) FROM ai_budget.accounts WHERE scope=%s", ("legacy-paid-scope",)
        ).fetchone()[0] == 0
    with pytest.raises(RuntimeError, match="legacy_budget_import_required"):
        BudgetLedger(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash

    imported = import_database(path, "budget", path, confirm_quiesced=True)
    assert imported["status"] == "imported"
    ledger = BudgetLedger(path)
    summary = ledger.summary(
        "legacy-paid-scope", "RUB", datetime.fromisoformat("2026-10-25T10:00:00+03:00")
    )
    assert summary["spent"] == Decimal("0.75")
    assert summary["lifetime_spent"] == Decimal("0.75")
    assert summary["lifetime_limit"] == Decimal("25")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
