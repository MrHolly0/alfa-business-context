"""Explicit fixture-only stand-in for the private Core job API.

This is a contract test server, not a production job owner. It stores seeded jobs,
leases and accepted results durably in SQLite so a real Worker/CoreClient can be
exercised without a Core installation.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _Body(BaseModel):
    model_config = ConfigDict(extra="allow")


class SeedRequest(_Body):
    job: dict[str, Any]


class UpdateVersionsRequest(_Body):
    tenant_scope: str
    company_id: str
    context_version: int = Field(ge=1)
    source_policy_version: int = Field(ge=1)


class ScheduleRequest(_Body):
    schedule_id: str = Field(min_length=1, max_length=256)
    task: dict[str, Any]
    interval_seconds: int = Field(ge=60, le=31_536_000)


class CoreStand:
    """SQLite-backed fixture state and job state machine."""

    def __init__(self, state_path: str | Path, *, clock: Callable[[], datetime] = _now,
                 lease_seconds: int = 60):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lease_seconds = max(1, int(lease_seconds))
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
              generation INTEGER NOT NULL, tenant_scope TEXT NOT NULL, company_id TEXT NOT NULL,
              current_context INTEGER NOT NULL, current_policy INTEGER NOT NULL,
              lease_until TEXT, result_hash TEXT, accepted_result TEXT,
              cancel_requested INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
              message_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL,
              published INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS published (
              tenant_scope TEXT NOT NULL, job_id TEXT NOT NULL, company_id TEXT NOT NULL, event_id TEXT NOT NULL,
              message_id TEXT NOT NULL, payload TEXT NOT NULL,
              revision INTEGER NOT NULL, created_at TEXT NOT NULL, input_signature TEXT
              ,PRIMARY KEY(tenant_scope,company_id,event_id)
            );
            CREATE TABLE IF NOT EXISTS publication_history (
              tenant_scope TEXT NOT NULL, company_id TEXT NOT NULL, event_id TEXT NOT NULL,
              input_signature TEXT NOT NULL, assessment_revision INTEGER NOT NULL,
              job_id TEXT NOT NULL, message_id TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(tenant_scope,company_id,event_id,input_signature)
            );
            CREATE TABLE IF NOT EXISTS company_versions (
              tenant_scope TEXT NOT NULL, company_id TEXT NOT NULL, context_version INTEGER NOT NULL,
              PRIMARY KEY(tenant_scope,company_id)
            );
            CREATE TABLE IF NOT EXISTS schedules (
              schedule_id TEXT PRIMARY KEY, task TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
              next_due TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
            );
            """)
            published_columns = {row[1] for row in db.execute("PRAGMA table_info(published)")}
            if "input_signature" not in published_columns:
                db.execute("ALTER TABLE published ADD COLUMN input_signature TEXT")

    def _connect(self):
        db = sqlite3.connect(str(self.state_path), timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _validate_job(job: dict[str, Any]) -> tuple[str, str, int, int]:
        kind = job.get("schema_version")
        if kind not in ("analysis-job-v1", "ai-job-v1"):
            raise ValueError("schema_version must be analysis-job-v1 or ai-job-v1")
        if not isinstance(job.get("message_id"), str) or not job["message_id"]:
            raise ValueError("message_id is required")
        for name in ("job_id", "tenant_scope", "company_id", "trace_id", "deadline", "lease_until"):
            if not isinstance(job.get(name), str) or not job[name]:
                raise ValueError(f"{name} is required")
        if kind == "analysis-job-v1" and not isinstance(job.get("analysis"), dict):
            raise ValueError("analysis object is required")
        if kind == "ai-job-v1" and not isinstance(job.get("task"), dict):
            raise ValueError("task object is required")
        versions = job.get("input_versions")
        if not isinstance(versions, dict):
            raise ValueError("input_versions object is required")
        context, policy = versions.get("context_version"), versions.get("source_policy_version")
        if type(context) is not int or context < 1 or type(policy) is not int or policy < 1:
            raise ValueError("input_versions requires positive context_version and source_policy_version")
        if not isinstance(job.get("scope"), dict) or not isinstance(job.get("budget"), dict):
            raise ValueError("scope and budget objects are required")
        generation = job.get("lease_generation", 1)
        if type(generation) is not int or generation < 1:
            raise ValueError("lease_generation must be a positive integer")
        _iso(datetime.fromisoformat(job["deadline"].replace("Z", "+00:00")))
        _iso(datetime.fromisoformat(job["lease_until"].replace("Z", "+00:00")))
        return job["tenant_scope"], job["company_id"], context, policy

    def seed_job(self, job: dict[str, Any]) -> dict[str, Any]:
        tenant, company, context, policy = self._validate_job(job)
        payload = json.dumps(job, ensure_ascii=False, sort_keys=True)
        now = _iso(self.clock())
        with self._lock, self._connect() as db:
            prior = db.execute("SELECT payload FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if prior:
                if prior["payload"] != payload:
                    raise ValueError("job_id already seeded with different content")
                return {"accepted": True, "job_id": job["job_id"], "duplicate": True}
            db.execute("INSERT INTO jobs(job_id,payload,status,generation,tenant_scope,company_id,current_context,current_policy,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (job["job_id"], payload, "queued", job.get("lease_generation", 1), tenant, company, context, policy, now))
            snapshots=job.get("task",{}).get("businesses",[]) if job["schema_version"]=="ai-job-v1" else []
            current={company:context,**{item["company_id"]:item["context_version"] for item in snapshots}}
            for company_id, version in current.items():
                db.execute("INSERT INTO company_versions VALUES(?,?,?) ON CONFLICT(tenant_scope,company_id) DO UPDATE SET context_version=MAX(company_versions.context_version,excluded.context_version)",
                           (tenant,company_id,version))
        return {"accepted": True, "job_id": job["job_id"], "duplicate": False}

    def claim(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            now = self.clock()
            expired = db.execute("SELECT job_id,payload,generation,lease_until FROM jobs WHERE status='leased'").fetchall()
            for old in expired:
                if datetime.fromisoformat(old["lease_until"].replace("Z", "+00:00")) <= now:
                    envelope = json.loads(old["payload"])
                    deadline = datetime.fromisoformat(envelope["deadline"].replace("Z", "+00:00"))
                    next_status = "expired" if deadline <= now else "queued"
                    next_generation = old["generation"] + 1 if next_status == "queued" else old["generation"]
                    db.execute("UPDATE jobs SET status=?,generation=?,lease_until=NULL,updated_at=? WHERE job_id=?",
                               (next_status, next_generation, _iso(now), old["job_id"]))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 ORDER BY updated_at,job_id LIMIT 1").fetchone()
            if not row:
                return None
            job = json.loads(row["payload"])
            deadline = datetime.fromisoformat(job["deadline"].replace("Z", "+00:00"))
            until = min(now + timedelta(seconds=self.lease_seconds), deadline)
            if until <= now:
                db.execute("UPDATE jobs SET status='expired',updated_at=? WHERE job_id=?", (_iso(now), row["job_id"]))
                return None
            generation = row["generation"]
            job["lease_generation"] = generation
            job["lease_until"] = _iso(until)
            db.execute("UPDATE jobs SET status='leased',lease_until=?,updated_at=? WHERE job_id=?",
                       (_iso(until), _iso(now), row["job_id"]))
            return {"job": job}

    def heartbeat(self, job_id: str, generation: int) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["status"] != "leased" or row["generation"] != generation or row["cancel_requested"]:
                return {"accepted": False, "code": "stale_lease", "lease_generation": generation}
            now = self.clock()
            old_until = datetime.fromisoformat(row["lease_until"].replace("Z", "+00:00"))
            deadline = datetime.fromisoformat(json.loads(row["payload"])["deadline"].replace("Z", "+00:00"))
            if old_until <= now or deadline <= now:
                return {"accepted": False, "code": "stale_lease", "lease_generation": generation}
            until = min(now + timedelta(seconds=self.lease_seconds), deadline)
            db.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE job_id=?", (_iso(until), _iso(now), job_id))
            return {"accepted": True, "lease_generation": generation, "lease_until": _iso(until)}

    def submit_result(self, result: dict[str, Any]) -> tuple[dict[str, Any], int]:
        job_id = result.get("job_id")
        if not isinstance(job_id, str):
            return {"accepted": False, "code": "invalid_result"}, 422
        digest = _digest(result)
        now = _iso(self.clock())
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return {"accepted": False, "code": "unknown_job"}, 404
            if row["result_hash"]:
                if row["result_hash"] == digest:
                    prior = json.loads(row["accepted_result"])
                    return {"accepted": True, "message_id": prior.get("message_id"), "duplicate": True}, 200
                return {"accepted": False, "code": "conflict"}, 409
            job = json.loads(row["payload"])
            expected_schema = "analysis-result-v1" if job["schema_version"] == "analysis-job-v1" else "ai-result-v1"
            if result.get("schema_version") != expected_schema:
                return {"accepted": False, "code": "invalid_result"}, 422
            if result.get("tenant_scope") != row["tenant_scope"] or result.get("company_id") != row["company_id"]:
                return {"accepted": False, "code": "authorization_denied"}, 403
            if result.get("lease_generation") != row["generation"] or row["status"] != "leased" or row["cancel_requested"]:
                return {"accepted": False, "code": "stale_lease"}, 409
            lease = datetime.fromisoformat(row["lease_until"].replace("Z", "+00:00"))
            deadline = datetime.fromisoformat(job["deadline"].replace("Z", "+00:00"))
            if lease <= self.clock() or deadline <= self.clock():
                return {"accepted": False, "code": "stale_lease"}, 409
            versions = result.get("input_versions")
            if versions != job.get("input_versions") or versions.get("context_version") != row["current_context"] or versions.get("source_policy_version") != row["current_policy"]:
                return {"accepted": False, "code": "conflict", "reason": "stale_input_versions"}, 409
            if job["schema_version"]=="ai-job-v1":
                for business in job["task"].get("businesses",[]):
                    current=db.execute("SELECT context_version FROM company_versions WHERE tenant_scope=? AND company_id=?",
                                       (row["tenant_scope"],business["company_id"])).fetchone()
                    if current and current[0]!=business["context_version"]:
                        return {"accepted":False,"code":"conflict","reason":"stale_business_context"},409
            candidate = result.get("candidate")
            if not isinstance(candidate, dict):
                return {"accepted": False, "code": "invalid_result"}, 422
            candidate_versions = candidate.get("versions", {})
            # Legacy candidate uses `context`/`source_policy`; ai-result-v1 contract
            # uses the envelope's *_version names. Validate where candidate includes them.
            if candidate_versions and (
                candidate_versions.get("context", candidate_versions.get("context_version")) != row["current_context"]
                or candidate_versions.get("source_policy", candidate_versions.get("source_policy_version")) != row["current_policy"]
            ):
                return {"accepted": False, "code": "conflict", "reason": "candidate_versions_stale"}, 409
            message_id = result.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                return {"accepted": False, "code": "invalid_result"}, 422
            revisions = candidate.get("revisions", [])
            if not isinstance(revisions, list):
                return {"accepted": False, "code": "invalid_result"}, 422
            if job["schema_version"]=="ai-job-v1":
                expected_businesses={item["company_id"]:item["context_version"] for item in job["task"].get("businesses",[])}
                scope_ids=set(expected_businesses)|{row["company_id"]}
                if any(not isinstance(item,dict) or item.get("company_id") not in scope_ids for item in revisions):
                    return {"accepted":False,"code":"authorization_denied"},403
                for item in candidate.get("candidates",[]):
                    if (not isinstance(item,dict) or item.get("company_id") not in scope_ids or
                        item.get("tenant_id")!=row["tenant_scope"]):
                        return {"accepted":False,"code":"authorization_denied"},403
                    expected=expected_businesses.get(item["company_id"],row["current_context"])
                    if item.get("versions",{}).get("context")!=expected:
                        return {"accepted":False,"code":"conflict","reason":"stale_business_context"},409
            db.execute("INSERT INTO outbox(message_id,job_id,payload,created_at) VALUES(?,?,?,?)",
                       (message_id, job_id, json.dumps(result, ensure_ascii=False, sort_keys=True), now))
            publications = [item for item in revisions if isinstance(item, dict) and item.get("publication_revision_required") is True]
            if candidate.get("publication_revision_required") is True or (
                candidate.get("schema_version") != "ai-data-result-v1"
                and "publication_revision_required" not in candidate
            ):
                publications.append({"company_id": candidate.get("company_id", row["company_id"]),
                                     "event_id": candidate.get("request_id", job_id), "publication_revision_required": True})
            for revision_data in publications:
                company_id, event_id = revision_data.get("company_id"), revision_data.get("event_id")
                if not isinstance(company_id, str) or not isinstance(event_id, str):
                    continue
                signature = revision_data.get("input_signature")
                proof = None
                if isinstance(signature, str) and signature:
                    proof = db.execute("SELECT assessment_revision FROM publication_history WHERE tenant_scope=? AND company_id=? AND event_id=? AND input_signature=?",
                                       (row["tenant_scope"], company_id, event_id, signature)).fetchone()
                if proof:
                    # A retry under a new job/message still refers to an already
                    # published analytical input signature; do not bump revisions.
                    continue
                existing = db.execute("SELECT revision FROM published WHERE tenant_scope=? AND company_id=? AND event_id=?",
                                      (row["tenant_scope"], company_id, event_id)).fetchone()
                revision = (existing["revision"] if existing else 0) + 1
                db.execute("INSERT INTO published(tenant_scope,job_id,company_id,event_id,message_id,payload,revision,created_at,input_signature) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_scope,company_id,event_id) DO UPDATE SET job_id=excluded.job_id,message_id=excluded.message_id,payload=excluded.payload,revision=excluded.revision,created_at=excluded.created_at,input_signature=excluded.input_signature",
                           (row["tenant_scope"], job_id, company_id, event_id, message_id, json.dumps(revision_data, ensure_ascii=False, sort_keys=True), revision, now, signature if isinstance(signature, str) else None))
                if isinstance(signature, str) and signature:
                    db.execute("INSERT INTO publication_history(tenant_scope,company_id,event_id,input_signature,assessment_revision,job_id,message_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                               (row["tenant_scope"], company_id, event_id, signature, revision, job_id, message_id, now))
            db.execute("UPDATE outbox SET published=1 WHERE message_id=?", (message_id,))
            db.execute("UPDATE jobs SET status='complete',result_hash=?,accepted_result=?,updated_at=? WHERE job_id=?",
                       (digest, json.dumps(result, ensure_ascii=False, sort_keys=True), now, job_id))
        return {"accepted": True, "message_id": message_id, "duplicate": False}, 200

    def failure(self, job_id: str, generation: int, code: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["status"] != "leased" or row["generation"] != generation:
                return {"accepted": False, "code": "stale_lease"}
            db.execute("UPDATE jobs SET status='failed',updated_at=? WHERE job_id=?", (_iso(self.clock()), job_id))
        return {"accepted": True, "job_id": job_id, "code": code[:128]}

    def inspect(self) -> dict[str, Any]:
        with self._connect() as db:
            jobs = [dict(row) for row in db.execute("SELECT job_id,status,generation,tenant_scope,company_id,current_context,current_policy,lease_until,cancel_requested,updated_at FROM jobs ORDER BY job_id")]
            published = [dict(row) for row in db.execute("SELECT tenant_scope,job_id,company_id,event_id,message_id,revision,created_at FROM published ORDER BY tenant_scope,company_id,event_id")]
            proofs = [dict(row) for row in db.execute("SELECT tenant_scope,company_id,event_id,input_signature,assessment_revision,job_id,message_id,created_at FROM publication_history ORDER BY tenant_scope,company_id,event_id,assessment_revision")]
            outbox = [dict(row) for row in db.execute("SELECT message_id,job_id,published,created_at FROM outbox ORDER BY created_at")]
        return {"jobs": jobs, "outbox": outbox, "published": published, "publication_history": proofs}

    def published_assessments(self, tenant_scope: str, company_id: str, *, limit: int = 600) -> list[dict[str, Any]]:
        """Return Core publication proofs suitable for a new DataTask snapshot."""
        limit = max(0, min(600, int(limit)))
        with self._connect() as db:
            rows = db.execute("SELECT company_id,event_id,input_signature,assessment_revision FROM publication_history WHERE tenant_scope=? AND company_id=? ORDER BY event_id,assessment_revision DESC LIMIT ?",
                              (tenant_scope, company_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return {"accepted": False, "code": "unknown_job"}
            if row["status"] in ("complete", "failed", "cancelled", "expired"):
                return {"accepted": False, "code": "already_terminal"}
            db.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,updated_at=? WHERE job_id=?", (_iso(self.clock()), job_id))
        return {"accepted": True, "job_id": job_id}

    def update_versions(self, tenant_scope: str, company_id: str, context_version: int, source_policy_version: int) -> int:
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO company_versions VALUES(?,?,?) ON CONFLICT(tenant_scope,company_id) DO UPDATE SET context_version=MAX(company_versions.context_version,excluded.context_version)",
                       (tenant_scope,company_id,context_version))
            cursor = db.execute("UPDATE jobs SET current_context=?,current_policy=?,updated_at=? WHERE tenant_scope=? AND company_id=? AND status IN ('queued','leased')",
                                (context_version, source_policy_version, _iso(self.clock()), tenant_scope, company_id))
            return cursor.rowcount

    def add_schedule(self, schedule_id: str, task: dict[str, Any], interval_seconds: int) -> None:
        if interval_seconds < 60:
            raise ValueError("schedule interval must be at least 60 seconds")
        from .workflow import DataTask
        parsed = DataTask.model_validate(task)
        if parsed.operation != "collect":
            raise ValueError("fixture schedule only accepts collect tasks")
        if parsed.budget.max_model_calls:
            raise ValueError("fixture schedules must have max_model_calls=0")
        task = parsed.model_dump(mode="json")
        task["knowledge_mode"] = "latest"
        task["trigger"] = "scheduled"
        # Validate the concrete scheduled form before persisting it.
        task = DataTask.model_validate(task).model_dump(mode="json")
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO schedules(schedule_id,task,interval_seconds,next_due) VALUES(?,?,?,?) ON CONFLICT(schedule_id) DO UPDATE SET task=excluded.task,interval_seconds=excluded.interval_seconds",
                       (schedule_id, json.dumps(task, ensure_ascii=False, sort_keys=True), interval_seconds, _iso(self.clock())))

    def enqueue_due_schedules(self) -> list[str]:
        """Turn each due fixture schedule into at most one durable ordinary job."""
        now = self.clock()
        made: list[str] = []
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_due<=? ORDER BY schedule_id", (_iso(now),)).fetchall()
            for row in rows:
                task = json.loads(row["task"])
                from .schemas import AnalysisBudget
                task["budget"]=AnalysisBudget.model_validate(task.get("budget",{})).model_copy(update={"max_model_calls":0}).model_dump(mode="json")
                from .collection import SourcePolicySnapshot
                from .workflow import DataTask
                parsed_task = DataTask.model_validate(task)
                context = int(task["context_version"])
                policy = int(task["source_policy_version"])
                source_policy = parsed_task.source_policy
                source_ids = source_policy.effective_source_ids()
                # Advance the task's knowledge cutoff to the actual due collection
                # instant; the scheduled snapshot is still immutable during its run.
                task["as_of"] = _iso(now)
                task["trigger"] = "scheduled"
                task["knowledge_mode"] = "latest"
                task["request_id"] = hashlib.sha256(
                    f"{row['schedule_id']}:{row['next_due']}".encode("utf-8")
                ).hexdigest()
                proof_company_ids = list(dict.fromkeys(
                    [parsed_task.company_id, *(item.company_id for item in parsed_task.businesses)]
                ))
                proofs: list[dict[str, Any]] = []
                for proof_company_id in proof_company_ids:
                    rows_for_company = db.execute(
                        "SELECT company_id,event_id,input_signature,assessment_revision "
                        "FROM publication_history WHERE tenant_scope=? AND company_id=? "
                        "ORDER BY event_id,assessment_revision DESC LIMIT 600",
                        (parsed_task.tenant_id, proof_company_id),
                    ).fetchall()
                    proofs.extend(dict(item) for item in rows_for_company)
                task["published_assessments"] = proofs[:600]
                job_id = "schedule-" + uuid.uuid4().hex
                deadline = now + timedelta(seconds=max(60, int(row["interval_seconds"])))
                job = {
                    "schema_version": "ai-job-v1", "message_id": "schedule-message-" + uuid.uuid4().hex,
                    "job_id": job_id, "tenant_scope": task["tenant_id"], "company_id": task["company_id"],
                    "trace_id": "schedule-trace-" + uuid.uuid4().hex, "lease_generation": 1,
                    "input_versions": {"context_version": context, "source_policy_version": policy, "event_versions": []},
                    "deadline": _iso(deadline), "lease_until": _iso(min(now + timedelta(seconds=self.lease_seconds), deadline)),
                    "budget": {"max_model_calls": 0,
                               "max_tokens": task.get("budget", {}).get("max_total_tokens", 1),
                               "cost_reservation_rub": 0, "cost_reservation_usd": 0},
                    "scope": {"source_ids": source_ids, "document_revision_refs": []}, "task": task,
                }
                tenant, company, current_context, current_policy = self._validate_job(job)
                db.execute("INSERT INTO jobs(job_id,payload,status,generation,tenant_scope,company_id,current_context,current_policy,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (job_id, json.dumps(job, ensure_ascii=False, sort_keys=True), "queued", 1, tenant, company, current_context, current_policy, _iso(now)))
                db.execute("UPDATE schedules SET next_due=? WHERE schedule_id=?", (_iso(now + timedelta(seconds=row["interval_seconds"])), row["schedule_id"]))
                made.append(job_id)
        return made


def create_app(state_path: str | Path, *, fixture_mode: bool = False,
               operator_token: str | None = None,
               clock: Callable[[], datetime] = _now,
               lease_seconds: int = 60) -> FastAPI:
    if not fixture_mode:
        raise ValueError("CoreStand only runs with explicit fixture_mode=True")
    token = operator_token if operator_token is not None else secrets.token_urlsafe(32)
    stand = CoreStand(state_path, clock=clock, lease_seconds=lease_seconds)

    @asynccontextmanager
    async def lifespan(app):
        stopping = asyncio.Event()

        async def schedule_loop():
            while not stopping.is_set():
                try:
                    stand.enqueue_due_schedules()
                except Exception:
                    # Fixture process must stay alive; durable due rows are retried.
                    pass
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=1)
                except asyncio.TimeoutError:
                    continue

        task = asyncio.create_task(schedule_loop())
        try:
            yield
        finally:
            stopping.set()
            await task

    app = FastAPI(title="Core contract stand (fixture mode)", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.core_stand = stand
    app.state.operator_token = token

    def auth(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(authorization[7:], token):
            raise HTTPException(401, "unauthorized")

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "fixture"}

    @app.post("/internal/v1/analysis-jobs/claim")
    def claim(_: None = Depends(auth)):
        job = stand.claim()
        return {"job": job["job"]} if job else {"job": None}

    @app.post("/internal/v1/analysis-jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str, body: dict[str, Any], _: None = Depends(auth)):
        generation = body.get("lease_generation")
        if type(generation) is not int or generation < 1:
            raise HTTPException(422, "invalid_lease_generation")
        return stand.heartbeat(job_id, generation)

    @app.post("/internal/v1/analysis-jobs/{job_id}/result")
    def result(job_id: str, body: dict[str, Any], _: None = Depends(auth)):
        if body.get("job_id") != job_id:
            raise HTTPException(422, "job_id_mismatch")
        payload, status = stand.submit_result(body)
        if status >= 400:
            raise HTTPException(status, payload)
        return payload

    @app.post("/internal/v1/analysis-jobs/{job_id}/failure")
    def failure(job_id: str, body: dict[str, Any], _: None = Depends(auth)):
        generation, code = body.get("lease_generation"), body.get("code")
        if type(generation) is not int or not isinstance(code, str):
            raise HTTPException(422, "invalid_failure")
        return stand.failure(job_id, generation, code)

    def operator(request: Request, x_operator_token: str | None = Header(default=None)):
        import ipaddress
        peer = request.client.host if request.client else ""
        try:
            loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        if not loopback or not x_operator_token or not hmac.compare_digest(x_operator_token, token):
            raise HTTPException(401, "operator_unauthorized")

    @app.post("/fixture/seed")
    def seed(body: SeedRequest, _: None = Depends(operator)):
        try:
            return stand.seed_job(body.job)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/fixture/schedules")
    def schedule(body: ScheduleRequest, _: None = Depends(operator)):
        try:
            stand.add_schedule(body.schedule_id, body.task, body.interval_seconds)
            return {"accepted": True, "schedule_id": body.schedule_id,
                    "interval_seconds": body.interval_seconds}
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/fixture/state")
    def inspect(_: None = Depends(operator)):
        return stand.inspect()

    @app.get("/fixture/published-assessments")
    def published_assessments(tenant_scope: str, company_id: str, _: None = Depends(operator)):
        return {"items": stand.published_assessments(tenant_scope, company_id)}

    @app.post("/fixture/jobs/{job_id}/cancel")
    def cancel(job_id: str, _: None = Depends(operator)):
        return stand.cancel(job_id)

    @app.post("/fixture/versions")
    def versions(body: UpdateVersionsRequest, _: None = Depends(operator)):
        count = stand.update_versions(body.tenant_scope, body.company_id, body.context_version, body.source_policy_version)
        return {"accepted": True, "updated_jobs": count}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixture-only Core job contract stand")
    parser.add_argument("--state-dir", default="/tmp/alfa-core-stand")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1", help="loopback only")
    parser.add_argument("--fixture-mode", action="store_true", help="required explicit opt-in")
    parser.add_argument("--seed", help="seed one job envelope JSON before serving")
    parser.add_argument("--schedule", help="register one ai-data-task-v1 collection JSON before serving")
    parser.add_argument("--schedule-id", help="stable fixture schedule ID (default: task request_id)")
    parser.add_argument("--interval", type=int, help="fixture schedule interval; minimum 60 seconds")
    args = parser.parse_args(argv)
    import ipaddress
    if not ipaddress.ip_address(args.host).is_loopback:
        parser.error("stand may bind only to a loopback address")
    if not args.fixture_mode:
        parser.error("--fixture-mode is required")
    import uvicorn
    token = os.environ.get("CORE_STAND_OPERATOR_TOKEN") or secrets.token_urlsafe(32)
    app = create_app(Path(args.state_dir) / "core-stand.sqlite3", fixture_mode=True, operator_token=token)
    stand = app.state.core_stand
    try:
        if args.seed:
            stand.seed_job(json.loads(Path(args.seed).read_text(encoding="utf-8")))
        if args.schedule:
            if args.interval is None:
                parser.error("--schedule requires --interval (minimum 60)")
            task_data = json.loads(Path(args.schedule).read_text(encoding="utf-8"))
            schedule_id = args.schedule_id or task_data.get("request_id")
            if not schedule_id:
                parser.error("--schedule task must contain request_id or use --schedule-id")
            stand.add_schedule(schedule_id, task_data, args.interval)
        elif args.interval is not None or args.schedule_id:
            parser.error("--interval and --schedule-id require --schedule")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(f"invalid fixture input: {type(exc).__name__}")
    print(f"Core fixture stand on http://{args.host}:{args.port}; operator token: {token}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
