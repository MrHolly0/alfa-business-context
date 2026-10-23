"""Process-safe local SQLite budget ledger for model calls.

Periods are [03:00, next day 03:00) Europe/Moscow. Settled calls are assigned
 to the period in which they were reserved. Unresolved reservations remain
 charged against the current period after rollover until settled; this prevents
 an in-flight request from becoming invisible at 03:00. The ledger only
 accounts for calls made through this file and database, not external users of
 the same provider key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


class BudgetError(RuntimeError):
    """Base class for ledger policy errors."""


class BudgetExceeded(BudgetError):
    pass


class CurrencyMismatch(BudgetError):
    pass


class ReservationError(BudgetError):
    pass


def _money(value: Decimal | int | str, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative amount")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative amount") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite non-negative amount")
    return result


def _aware(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(MOSCOW)


def _period(now: datetime | None) -> tuple[str, str]:
    local = _aware(now)
    start = local.replace(hour=3, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


class BudgetLedger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("in-memory databases are unsupported; use a file-backed SQLite path")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    scope TEXT PRIMARY KEY,
                    currency TEXT NOT NULL,
                    limit_amount TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    reserved_amount TEXT NOT NULL,
                    actual_amount TEXT,
                    cost_source TEXT,
                    request_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('reserved','uncertain','settled')),
                    usage_json TEXT,
                    created_at TEXT NOT NULL,
                    settled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS reservations_scope_period
                    ON reservations(scope, period_start, status);
            """)
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _scope(scope: str) -> str:
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope must be a non-empty string")
        return scope.strip()

    @staticmethod
    def _currency(currency: str) -> str:
        if not isinstance(currency, str) or not currency.strip():
            raise ValueError("currency must be a non-empty string")
        return currency.strip().upper()

    def _ensure_account(self, db: sqlite3.Connection, scope: str, currency: str, limit: Decimal) -> None:
        row = db.execute("SELECT currency, limit_amount FROM accounts WHERE scope=?", (scope,)).fetchone()
        if row is None:
            db.execute("INSERT INTO accounts(scope,currency,limit_amount) VALUES(?,?,?)", (scope, currency, str(limit)))
        elif row["currency"] != currency:
            raise CurrencyMismatch(f"scope {scope!r} is already tracked in {row['currency']}")
        elif Decimal(row["limit_amount"]) != limit:
            raise ValueError(f"scope {scope!r} already has a different configured limit")

    def reserve(self, scope: str, currency: str, limit: Decimal | int | str,
                amount: Decimal | int | str, request_id: str | None = None,
                now: datetime | None = None) -> str:
        scope, currency = self._scope(scope), self._currency(currency)
        limit, amount = _money(limit, "limit"), _money(amount, "amount")
        if limit < 0 or amount < 0:
            raise ValueError("limit and reservation amount must be non-negative")
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
            raise ValueError("request_id must be non-empty when provided")
        period_start, _ = _period(now)
        current = _aware(now).isoformat()
        reservation_id = str(uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_account(db, scope, currency, limit)
            rows = db.execute("""SELECT period_start,status,reserved_amount,actual_amount FROM reservations
                               WHERE scope=? AND (period_start=? OR status IN ('reserved','uncertain'))""",
                              (scope, period_start)).fetchall()
            committed = sum((Decimal(r["actual_amount"]) for r in rows if r["status"] == "settled" and r["period_start"] == period_start), Decimal(0))
            pending = sum((Decimal(r["reserved_amount"]) for r in rows if r["status"] != "settled"), Decimal(0))
            if committed + pending + amount > limit:
                raise BudgetExceeded(f"reservation would exceed {currency} budget for scope {scope!r}")
            db.execute("""INSERT INTO reservations(id,scope,currency,period_start,reserved_amount,request_id,status,created_at)
                          VALUES(?,?,?,?,?,?,?,?)""",
                       (reservation_id, scope, currency, period_start, str(amount), request_id, "reserved", current))
            db.commit()
            return reservation_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def settle(self, reservation_id: str, amount: Decimal | int | str, cost_source: str,
               usage: Mapping[str, Any] | None = None, now: datetime | None = None) -> None:
        amount = _money(amount, "amount")
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("reservation_id must be non-empty")
        if not isinstance(cost_source, str) or not cost_source.strip():
            raise ValueError("cost_source must be non-empty")
        try:
            usage_json = None if usage is None else json.dumps(usage, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("usage must be finite JSON-serializable metadata") from exc
        settled_at = _aware(now).isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise ReservationError("unknown reservation")
            if row["status"] == "settled":
                if Decimal(row["actual_amount"]) == amount and row["cost_source"] == cost_source.strip() and row["usage_json"] == usage_json:
                    db.commit()
                    return
                raise ReservationError("settlement conflicts with the existing settlement")
            db.execute("""UPDATE reservations SET status='settled',actual_amount=?,cost_source=?,usage_json=?,settled_at=?
                          WHERE id=?""", (str(amount), cost_source.strip(), usage_json, settled_at, reservation_id))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_uncertain(self, reservation_id: str, now: datetime | None = None) -> None:
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("reservation_id must be non-empty")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise ReservationError("unknown reservation")
            if row["status"] == "settled":
                raise ReservationError("settled reservation cannot become uncertain")
            db.execute("UPDATE reservations SET status='uncertain' WHERE id=?", (reservation_id,))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def summary(self, scope: str, currency: str, now: datetime | None = None) -> dict[str, Any]:
        scope, currency = self._scope(scope), self._currency(currency)
        period_start, period_end = _period(now)
        db = self._connect()
        try:
            db.execute("BEGIN")
            account = db.execute("SELECT currency,limit_amount FROM accounts WHERE scope=?", (scope,)).fetchone()
            if account is None:
                return {"scope": scope, "currency": currency, "period_start": period_start,
                        "period_end": period_end, "limit": Decimal(0), "spent": Decimal(0),
                        "reported_spent": Decimal(0), "estimated_spent": Decimal(0),
                        "reserved": Decimal(0), "remaining": Decimal(0), "reservation_count": 0,
                        "uncertain_count": 0, "settled_count": 0, "blocked": False}
            if account["currency"] != currency:
                raise CurrencyMismatch(f"scope {scope!r} is tracked in {account['currency']}")
            rows = db.execute("""SELECT * FROM reservations WHERE scope=? AND (period_start=? OR status IN ('reserved','uncertain'))""",
                              (scope, period_start)).fetchall()
            settled = [r for r in rows if r["status"] == "settled" and r["period_start"] == period_start]
            spent = sum((Decimal(r["actual_amount"]) for r in settled), Decimal(0))
            reported_spent = sum((Decimal(r["actual_amount"]) for r in settled if r["cost_source"] == "provider_reported"), Decimal(0))
            estimated_spent = sum((Decimal(r["actual_amount"]) for r in settled if r["cost_source"] == "rate_estimate"), Decimal(0))
            reserved = sum((Decimal(r["reserved_amount"]) for r in rows if r["status"] != "settled"), Decimal(0))
            limit = Decimal(account["limit_amount"])
            return {"scope": scope, "currency": currency, "period_start": period_start,
                    "period_end": period_end, "limit": limit, "spent": spent,
                    "reported_spent": reported_spent, "estimated_spent": estimated_spent,
                    "reserved": reserved, "remaining": max(Decimal(0), limit-spent-reserved),
                    "reservation_count": len(rows),
                    "uncertain_count": sum(r["status"] == "uncertain" for r in rows),
                    "settled_count": len(settled), "blocked": spent+reserved > limit}
        finally:
            db.close()
