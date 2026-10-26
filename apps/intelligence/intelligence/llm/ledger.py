"""PostgreSQL-backed exact budget ledger for model calls.

Periods are [03:00, next day 03:00) Europe/Moscow. Settled calls are assigned
to the period in which they were reserved. Unresolved reservations remain
charged against the current period after rollover until settled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..storage import Database

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


def _period(now: datetime | None) -> tuple[datetime, datetime]:
    local = _aware(now)
    start = local.replace(hour=3, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return start, start + timedelta(days=1)


def _call_cap(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("max_calls must be a non-negative integer")
    return value


class BudgetLedger:
    """Exact PostgreSQL budget ledger.

    ``path`` remains a logical identity for callers that used the SQLite API.
    Budget accounts deliberately share one deployment namespace across state
    directories so a provider cap cannot be bypassed by changing the path.
    """

    def __init__(self, path: str | Path, *, namespace: str | None = None):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("in-memory databases are unsupported; use PostgreSQL")
        self.database = Database("budget", path, namespace=namespace)
        # A legacy ledger can contain provider spend that is not yet represented
        # in PostgreSQL. Check only whether the old file exists; never open it
        # here. The one-time importer owns reading its read-only snapshot.
        if Path(path).is_file():
            with self.database.connection() as db:
                imported = db.execute(
                    "SELECT 1 FROM ai_meta.imports WHERE component=%s AND store_key=%s LIMIT 1",
                    ("budget", self.database.namespace),
                ).fetchone()
            if not imported:
                raise RuntimeError("legacy_budget_import_required")

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

    def _ensure_account(self, db, scope: str, currency: str, limit: Decimal,
                        lifetime_limit: Decimal | None) -> None:
        row = db.execute(
            "SELECT currency,limit_amount,lifetime_limit FROM ai_budget.accounts WHERE scope=%s FOR UPDATE",
            (scope,),
        ).fetchone()
        if row is None:
            db.execute(
                """INSERT INTO ai_budget.accounts(scope,currency,limit_amount,lifetime_limit)
                   VALUES(%s,%s,%s,%s) ON CONFLICT(store_key,scope) DO NOTHING""",
                (scope, currency, limit, lifetime_limit),
            )
            row = db.execute(
                "SELECT currency,limit_amount,lifetime_limit FROM ai_budget.accounts WHERE scope=%s FOR UPDATE",
                (scope,),
            ).fetchone()
        if row["currency"] != currency:
            raise CurrencyMismatch(f"scope {scope!r} is already tracked in {row['currency']}")
        if row["limit_amount"] != limit:
            raise ValueError(f"scope {scope!r} already has a different configured limit")
        actual_lifetime = row["lifetime_limit"]
        if actual_lifetime != lifetime_limit:
            raise ValueError(f"scope {scope!r} already has a different configured lifetime limit")

    def reserve(self, scope: str, currency: str, limit: Decimal | int | str,
                amount: Decimal | int | str, request_id: str | None = None,
                now: datetime | None = None, *, lifetime_limit: Decimal | int | str | None = None,
                max_calls: int | None = None, max_total_calls: int | None = None) -> str:
        scope, currency = self._scope(scope), self._currency(currency)
        limit, amount = _money(limit, "limit"), _money(amount, "amount")
        lifetime_limit = None if lifetime_limit is None else _money(lifetime_limit, "lifetime_limit")
        max_calls = _call_cap(max_calls)
        max_total_calls = _call_cap(max_total_calls)
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
            raise ValueError("request_id must be non-empty when provided")
        period_start, _ = _period(now)
        current = _aware(now).astimezone(timezone.utc)
        reservation_id = str(uuid4())
        with self.database.connection() as db:
            db.lock(key="budget:" + scope)
            self._ensure_account(db, scope, currency, limit, lifetime_limit)
            rows = db.execute(
                """SELECT period_start,status,reserved_amount,actual_amount
                   FROM ai_budget.reservations
                   WHERE scope=%s AND (period_start=%s OR status IN ('reserved','uncertain'))""",
                (scope, period_start),
            ).fetchall()
            # The query includes settled records only from this period; pending
            # records from prior periods remain visible until their settlement.
            committed = sum((r["actual_amount"] for r in rows if r["status"] == "settled"), Decimal(0))
            pending = sum((r["reserved_amount"] for r in rows if r["status"] != "settled"), Decimal(0))
            if committed + pending + amount > limit:
                raise BudgetExceeded(f"reservation would exceed {currency} budget for scope {scope!r}")
            if max_calls is not None:
                used_calls = db.execute(
                    "SELECT count(*) FROM ai_budget.reservations WHERE scope=%s AND period_start=%s",
                    (scope, period_start),
                ).fetchone()[0]
                if used_calls >= max_calls:
                    raise BudgetExceeded(f"reservation would exceed call limit for scope {scope!r}")
            if max_total_calls is not None:
                total_calls = db.execute(
                    "SELECT count(*) FROM ai_budget.reservations WHERE scope=%s", (scope,)
                ).fetchone()[0]
                if total_calls >= max_total_calls:
                    raise BudgetExceeded(f"reservation would exceed lifetime call limit for scope {scope!r}")
            if lifetime_limit is not None:
                lifetime_rows = db.execute(
                    "SELECT status,reserved_amount,actual_amount FROM ai_budget.reservations WHERE scope=%s",
                    (scope,),
                ).fetchall()
                lifetime_used = sum((r["actual_amount"] if r["status"] == "settled" else r["reserved_amount"]
                                     for r in lifetime_rows), Decimal(0))
                if lifetime_used + amount > lifetime_limit:
                    raise BudgetExceeded(f"reservation would exceed {currency} lifetime budget for scope {scope!r}")
            db.execute(
                """INSERT INTO ai_budget.reservations
                   (id,scope,currency,period_start,reserved_amount,request_id,status,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,'reserved',%s)""",
                (reservation_id, scope, currency, period_start, amount, request_id, current),
            )
        return reservation_id

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
        settled_at = _aware(now).astimezone(timezone.utc)
        with self.database.connection() as db:
            identity = db.execute(
                "SELECT scope FROM ai_budget.reservations WHERE id=%s", (reservation_id,)
            ).fetchone()
            if identity is None:
                raise ReservationError("unknown reservation")
            # Reserve and settlement must serialize on the same account lock.
            # If a provider reports more than the estimate while another call
            # is reserving, the next reserve must see the settled actual amount.
            db.lock(key="budget:" + identity["scope"])
            row = db.execute("SELECT * FROM ai_budget.reservations WHERE id=%s FOR UPDATE", (reservation_id,)).fetchone()
            if row is None:
                raise ReservationError("unknown reservation")
            if row["status"] == "settled":
                if (row["actual_amount"] == amount and row["cost_source"] == cost_source.strip()
                        and row["usage_json"] == usage_json):
                    return
                raise ReservationError("settlement conflicts with the existing settlement")
            db.execute(
                """UPDATE ai_budget.reservations SET status='settled',actual_amount=%s,cost_source=%s,
                   usage_json=%s,settled_at=%s WHERE id=%s""",
                (amount, cost_source.strip(), usage_json, settled_at, reservation_id),
            )

    def mark_uncertain(self, reservation_id: str, now: datetime | None = None) -> None:
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("reservation_id must be non-empty")
        with self.database.connection() as db:
            db.lock(key="reservation:" + reservation_id)
            row = db.execute("SELECT status FROM ai_budget.reservations WHERE id=%s FOR UPDATE", (reservation_id,)).fetchone()
            if row is None:
                raise ReservationError("unknown reservation")
            if row["status"] == "settled":
                raise ReservationError("settled reservation cannot become uncertain")
            db.execute("UPDATE ai_budget.reservations SET status='uncertain' WHERE id=%s", (reservation_id,))

    def summary(self, scope: str, currency: str, now: datetime | None = None) -> dict[str, Any]:
        scope, currency = self._scope(scope), self._currency(currency)
        period_start, period_end = _period(now)
        with self.database.connection() as db:
            account = db.execute(
                "SELECT currency,limit_amount,lifetime_limit FROM ai_budget.accounts WHERE scope=%s", (scope,)
            ).fetchone()
            if account is None:
                return {"scope": scope, "currency": currency, "period_start": period_start.isoformat(),
                        "period_end": period_end.isoformat(), "limit": Decimal(0), "spent": Decimal(0),
                        "reported_spent": Decimal(0), "estimated_spent": Decimal(0), "reserved": Decimal(0),
                        "remaining": Decimal(0), "reservation_count": 0, "uncertain_count": 0,
                        "settled_count": 0, "blocked": False, "lifetime_limit": None,
                        "lifetime_spent": Decimal(0), "lifetime_reserved": Decimal(0), "lifetime_remaining": None}
            if account["currency"] != currency:
                raise CurrencyMismatch(f"scope {scope!r} is tracked in {account['currency']}")
            rows = db.execute(
                """SELECT * FROM ai_budget.reservations
                   WHERE scope=%s AND (period_start=%s OR status IN ('reserved','uncertain'))""",
                (scope, period_start),
            ).fetchall()
            settled = [r for r in rows if r["status"] == "settled"]
            spent = sum((r["actual_amount"] for r in settled), Decimal(0))
            reported_spent = sum((r["actual_amount"] for r in settled if r["cost_source"] == "provider_reported"), Decimal(0))
            estimated_spent = sum((r["actual_amount"] for r in settled if r["cost_source"] == "rate_estimate"), Decimal(0))
            reserved = sum((r["reserved_amount"] for r in rows if r["status"] != "settled"), Decimal(0))
            limit_amount = account["limit_amount"]
            all_rows = db.execute(
                "SELECT status,reserved_amount,actual_amount FROM ai_budget.reservations WHERE scope=%s", (scope,)
            ).fetchall()
            lifetime_spent = sum((r["actual_amount"] for r in all_rows if r["status"] == "settled"), Decimal(0))
            lifetime_reserved = sum((r["reserved_amount"] for r in all_rows if r["status"] != "settled"), Decimal(0))
            lifetime_limit = account["lifetime_limit"]
            return {"scope": scope, "currency": currency, "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(), "limit": limit_amount, "spent": spent,
                    "reported_spent": reported_spent, "estimated_spent": estimated_spent,
                    "reserved": reserved, "remaining": max(Decimal(0), limit_amount-spent-reserved),
                    "reservation_count": len(rows), "uncertain_count": sum(r["status"] == "uncertain" for r in rows),
                    "settled_count": len(settled), "blocked": spent+reserved > limit_amount,
                    "lifetime_limit": lifetime_limit, "lifetime_spent": lifetime_spent,
                    "lifetime_reserved": lifetime_reserved,
                    "lifetime_remaining": None if lifetime_limit is None else
                        max(Decimal(0), lifetime_limit-lifetime_spent-lifetime_reserved)}

    def configure_limits(self, scope: str, currency: str, *, daily_limit: Decimal | int | str,
                         lifetime_limit: Decimal | int | str | None,
                         expected_daily_limit: Decimal | int | str,
                         expected_lifetime_limit: Decimal | int | str | None) -> None:
        """Explicit compare-and-set for operator-approved account caps; never resets usage."""
        scope, currency = self._scope(scope), self._currency(currency)
        daily = _money(daily_limit, "daily_limit")
        life = None if lifetime_limit is None else _money(lifetime_limit, "lifetime_limit")
        expected_daily = _money(expected_daily_limit, "expected_daily_limit")
        expected_life = None if expected_lifetime_limit is None else _money(expected_lifetime_limit, "expected_lifetime_limit")
        with self.database.connection() as db:
            db.lock(key="budget:" + scope)
            row = db.execute("SELECT currency,limit_amount,lifetime_limit FROM ai_budget.accounts WHERE scope=%s FOR UPDATE", (scope,)).fetchone()
            if row is None:
                raise ValueError(f"scope {scope!r} has no existing ledger account")
            if row["currency"] != currency:
                raise CurrencyMismatch(f"scope {scope!r} is tracked in {row['currency']}")
            if row["limit_amount"] != expected_daily or row["lifetime_limit"] != expected_life:
                raise ValueError("configured limits changed; inspect the ledger and retry with current expected values")
            db.execute("UPDATE ai_budget.accounts SET limit_amount=%s,lifetime_limit=%s WHERE scope=%s",
                       (daily, life, scope))
