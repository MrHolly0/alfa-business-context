"""Durable candidate and Core result outbox storage."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class ArtifactStore:
    """SQLite-backed request idempotency and result outbox; never stores credentials."""
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("ArtifactStore requires durable SQLite path")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candidates (
                  tenant_id TEXT NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                  response TEXT NOT NULL, PRIMARY KEY (tenant_id, request_id));
                CREATE TABLE IF NOT EXISTS reservations (
                  tenant_id TEXT NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                  PRIMARY KEY (tenant_id, request_id));
                CREATE TABLE IF NOT EXISTS outbox (
                  message_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS outbox_dead (
                  message_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL,
                  reason TEXT NOT NULL, failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS job_runs (
                  job_id TEXT NOT NULL, lease_generation INTEGER NOT NULL, input_hash TEXT NOT NULL,
                  status TEXT NOT NULL, message_id TEXT, payload TEXT, reason TEXT,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY(job_id, lease_generation));
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_candidate(self, tenant_id: str, request_id: str, body_hash: str):
        with self._db() as db:
            row = db.execute("SELECT * FROM candidates WHERE tenant_id=? AND request_id=?", (tenant_id, request_id)).fetchone()
        if not row: return None
        if row["payload_hash"] != body_hash: raise ValueError("idempotency_conflict")
        return json.loads(row["response"])

    def save_candidate(self, tenant_id: str, request_id: str, body_hash: str, response: Any):
        encoded = canonical_json(response)
        with self._db() as db:
            try:
                db.execute("INSERT INTO candidates VALUES (?,?,?,?)", (tenant_id, request_id, body_hash, encoded))
                db.execute("DELETE FROM reservations WHERE tenant_id=? AND request_id=?", (tenant_id, request_id))
            except sqlite3.IntegrityError:
                row = db.execute("SELECT payload_hash,response FROM candidates WHERE tenant_id=? AND request_id=?", (tenant_id, request_id)).fetchone()
                if row["payload_hash"] != body_hash: raise ValueError("idempotency_conflict")
                return json.loads(row["response"])
        return json.loads(encoded)

    def reserve_candidate(self, tenant_id: str, request_id: str, body_hash: str) -> bool:
        """Atomically reserve one execution; matching in-flight retries never run the model twice."""
        with self._db() as db:
            if db.execute("SELECT 1 FROM candidates WHERE tenant_id=? AND request_id=?", (tenant_id, request_id)).fetchone():
                return False
            try:
                db.execute("INSERT INTO reservations VALUES (?,?,?)", (tenant_id, request_id, body_hash))
                return True
            except sqlite3.IntegrityError:
                row = db.execute("SELECT payload_hash FROM reservations WHERE tenant_id=? AND request_id=?", (tenant_id, request_id)).fetchone()
                if row["payload_hash"] != body_hash: raise ValueError("idempotency_conflict")
                return False

    def list_reservations(self):
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT tenant_id,request_id,payload_hash FROM reservations ORDER BY tenant_id,request_id")]

    def reset_candidate(self, tenant_id: str, request_id: str, *, force: bool = False):
        """Operator recovery only after stopping the caller and reviewing uncertain provider spend."""
        if force is not True: raise ValueError("explicit force confirmation is required")
        with self._db() as db:
            deleted=db.execute("DELETE FROM reservations WHERE tenant_id=? AND request_id=?",(tenant_id,request_id)).rowcount
            if not deleted: raise KeyError("request reservation not found")

    def enqueue(self, message_id: str, job_id: str, payload: Any):
        encoded = canonical_json(payload)
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO outbox(message_id,job_id,payload) VALUES (?,?,?)", (message_id, job_id, encoded))
            row = db.execute("SELECT payload FROM outbox WHERE message_id=?", (message_id,)).fetchone()
            if row["payload"] != encoded: raise ValueError("outbox message id payload conflict")
        return json.loads(encoded)

    def begin_job(self, job_id: str, generation: int, input_hash: str) -> dict:
        """Fence duplicate executions, including process restarts after uncertain model spend."""
        with self._db() as db:
            inserted = db.execute("INSERT OR IGNORE INTO job_runs(job_id,lease_generation,input_hash,status) VALUES (?,?,?,'running')",
                       (job_id, generation, input_hash)).rowcount == 1
            row = db.execute("SELECT * FROM job_runs WHERE job_id=? AND lease_generation=?", (job_id,generation)).fetchone()
            if row["input_hash"] != input_hash: raise ValueError("job generation input conflict")
            if inserted: return {"status":"started"}
            if row["status"] == "reset":
                db.execute("UPDATE job_runs SET status='running',reason=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND lease_generation=?",(job_id,generation))
                return {"status":"started"}
            return {"status":row["status"], "message_id":row["message_id"], "payload":json.loads(row["payload"]) if row["payload"] else None, "reason":row["reason"]}

    def complete_job(self, job_id: str, generation: int, input_hash: str, message_id: str, payload: Any):
        """Persist a candidate and its outbox message atomically before network submission."""
        encoded = canonical_json(payload)
        with self._db() as db:
            row = db.execute("SELECT * FROM job_runs WHERE job_id=? AND lease_generation=?",(job_id,generation)).fetchone()
            if not row or row["input_hash"] != input_hash: raise ValueError("job run reservation missing or changed")
            if row["status"] == "complete":
                if row["message_id"] != message_id or row["payload"] != encoded: raise ValueError("completed job payload conflict")
                return json.loads(row["payload"])
            if row["status"] != "running": raise ValueError("job run is not executable")
            db.execute("INSERT OR IGNORE INTO outbox(message_id,job_id,payload) VALUES (?,?,?)",(message_id,job_id,encoded))
            pending = db.execute("SELECT payload FROM outbox WHERE message_id=?",(message_id,)).fetchone()
            if not pending or pending["payload"] != encoded: raise ValueError("outbox message id payload conflict")
            db.execute("UPDATE job_runs SET status='complete',message_id=?,payload=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND lease_generation=?",
                       (message_id,encoded,job_id,generation))
        return json.loads(encoded)

    def list_job_runs(self):
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT job_id,lease_generation,input_hash,status,message_id,reason,updated_at FROM job_runs ORDER BY updated_at")]

    def reset_job_run(self, job_id: str, generation: int, *, force: bool = False):
        """Explicitly authorize rerun of an interrupted generation after operator review."""
        if force is not True: raise ValueError("explicit force confirmation is required")
        with self._db() as db:
            row=db.execute("SELECT status FROM job_runs WHERE job_id=? AND lease_generation=?",(job_id,generation)).fetchone()
            if not row: raise KeyError("job run not found")
            if row["status"] not in ("running", "dead"): raise ValueError("only interrupted/dead runs may be reset")
            db.execute("UPDATE job_runs SET status='reset',reason='operator_reset',updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND lease_generation=?",(job_id,generation))

    def quarantine_job_run(self, job_id: str, generation: int, reason: str):
        with self._db() as db:
            db.execute("UPDATE job_runs SET status='dead',reason=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND lease_generation=?",
                       (reason,job_id,generation))

    def pending(self):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT message_id,job_id,payload FROM outbox ORDER BY created_at,message_id")]

    def acknowledge(self, message_id: str):
        with self._db() as db:
            db.execute("DELETE FROM outbox WHERE message_id=?", (message_id,))
            db.execute("UPDATE job_runs SET status='acknowledged',updated_at=CURRENT_TIMESTAMP WHERE message_id=? AND status='complete'", (message_id,))

    def quarantine(self, message_id: str, reason: str):
        with self._db() as db:
            row = db.execute("SELECT job_id,payload FROM outbox WHERE message_id=?", (message_id,)).fetchone()
            if row:
                db.execute("INSERT OR REPLACE INTO outbox_dead(message_id,job_id,payload,reason) VALUES (?,?,?,?)",
                           (message_id,row["job_id"],row["payload"],reason))
                db.execute("DELETE FROM outbox WHERE message_id=?", (message_id,))
                db.execute("UPDATE job_runs SET status='dead',reason=?,updated_at=CURRENT_TIMESTAMP WHERE message_id=?",(reason,message_id))
