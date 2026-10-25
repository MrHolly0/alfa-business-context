"""Durable candidate and Core result outbox storage in PostgreSQL."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .storage import Database


def canonical_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class ArtifactStore:
    """PostgreSQL request idempotency and result outbox; never stores credentials."""

    def __init__(self, path: str | Path, *, namespace: str | None = None):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("ArtifactStore requires durable PostgreSQL storage")
        self.database = Database("artifacts", path, namespace=namespace)

    @contextmanager
    def _db(self):
        with self.database.connection() as db:
            yield db

    def get_candidate(self, tenant_id: str, request_id: str, body_hash: str):
        with self._db() as db:
            row = db.execute(
                "SELECT payload_hash,response FROM ai_artifacts.candidates WHERE tenant_id=%s AND request_id=%s",
                (tenant_id, request_id),
            ).fetchone()
        if not row:
            return None
        if row["payload_hash"] != body_hash:
            raise ValueError("idempotency_conflict")
        return json.loads(row["response"])

    def save_candidate(self, tenant_id: str, request_id: str, body_hash: str, response: Any,
                       owner_token: str | None = None):
        encoded = canonical_json(response)
        with self._db() as db:
            db.lock(key="write")
            if owner_token is not None:
                reservation = db.execute(
                    "SELECT payload_hash,owner_token FROM ai_artifacts.reservations WHERE tenant_id=%s AND request_id=%s FOR UPDATE",
                    (tenant_id, request_id),
                ).fetchone()
                if (not reservation or reservation["payload_hash"] != body_hash
                        or reservation["owner_token"] != owner_token):
                    raise ValueError("reservation_owner_mismatch")
            inserted = db.execute(
                """INSERT INTO ai_artifacts.candidates(tenant_id,request_id,payload_hash,response)
                   VALUES(%s,%s,%s,%s) ON CONFLICT(store_key,tenant_id,request_id) DO NOTHING
                   RETURNING payload_hash,response""",
                (tenant_id, request_id, body_hash, encoded),
            ).fetchone()
            if inserted:
                db.execute("DELETE FROM ai_artifacts.reservations WHERE tenant_id=%s AND request_id=%s", (tenant_id, request_id))
                return json.loads(inserted["response"])
            row = db.execute(
                "SELECT payload_hash,response FROM ai_artifacts.candidates WHERE tenant_id=%s AND request_id=%s FOR UPDATE",
                (tenant_id, request_id),
            ).fetchone()
            if row["payload_hash"] != body_hash:
                raise ValueError("idempotency_conflict")
            return json.loads(row["response"])

    def reserve_candidate(self, tenant_id: str, request_id: str, body_hash: str) -> bool:
        """Atomically reserve one execution; retries do not run the model twice."""
        return self.claim_candidate(tenant_id, request_id, body_hash) is not None

    def claim_candidate(self, tenant_id: str, request_id: str, body_hash: str) -> str | None:
        """Create an execution reservation and return its fencing token.

        A matching retry receives no token while the first owner is active or
        after an interrupted/uncertain execution. Only that token may complete
        or release the reservation.
        """
        owner_token = uuid.uuid4().hex
        with self._db() as db:
            db.lock(key="write")
            if db.execute(
                "SELECT 1 FROM ai_artifacts.candidates WHERE tenant_id=%s AND request_id=%s",
                (tenant_id, request_id),
            ).fetchone():
                return None
            inserted = db.execute(
                """INSERT INTO ai_artifacts.reservations(tenant_id,request_id,payload_hash,owner_token)
                   VALUES(%s,%s,%s,%s) ON CONFLICT(store_key,tenant_id,request_id) DO NOTHING
                   RETURNING owner_token""",
                (tenant_id, request_id, body_hash, owner_token),
            ).fetchone()
            if inserted:
                return owner_token
            row = db.execute(
                "SELECT payload_hash FROM ai_artifacts.reservations WHERE tenant_id=%s AND request_id=%s",
                (tenant_id, request_id),
            ).fetchone()
            if row["payload_hash"] != body_hash:
                raise ValueError("idempotency_conflict")
            return None

    def release_candidate(self, tenant_id: str, request_id: str, body_hash: str, owner_token: str) -> bool:
        """Release a reservation only for its owner and matching request hash."""
        if not owner_token:
            return False
        with self._db() as db:
            db.lock(key="write")
            return db.execute(
                """DELETE FROM ai_artifacts.reservations
                   WHERE tenant_id=%s AND request_id=%s AND payload_hash=%s AND owner_token=%s""",
                (tenant_id, request_id, body_hash, owner_token),
            ).rowcount == 1

    def list_reservations(self):
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT tenant_id,request_id,payload_hash,owner_token FROM ai_artifacts.reservations ORDER BY tenant_id,request_id"
            )]

    def reset_candidate(self, tenant_id: str, request_id: str, *, force: bool = False):
        """Operator recovery after stopping the caller and reviewing uncertain spend."""
        if force is not True:
            raise ValueError("explicit force confirmation is required")
        with self._db() as db:
            db.lock(key="write")
            deleted = db.execute(
                "DELETE FROM ai_artifacts.reservations WHERE tenant_id=%s AND request_id=%s", (tenant_id, request_id)
            ).rowcount
            if not deleted:
                raise KeyError("request reservation not found")

    def enqueue(self, message_id: str, job_id: str, payload: Any):
        encoded = canonical_json(payload)
        with self._db() as db:
            db.lock(key="write")
            db.execute(
                """INSERT INTO ai_artifacts.outbox(message_id,job_id,payload,retry_deadline)
                   VALUES(%s,%s,%s,%s) ON CONFLICT(store_key,message_id) DO NOTHING""",
                (message_id, job_id, encoded, time.time() + 86400),
            )
            row = db.execute("SELECT payload FROM ai_artifacts.outbox WHERE message_id=%s", (message_id,)).fetchone()
            if row["payload"] != encoded:
                raise ValueError("outbox message id payload conflict")
        return json.loads(encoded)

    def begin_job(self, job_id: str, generation: int, input_hash: str) -> dict:
        """Fence duplicate executions, including restarts after uncertain spend."""
        with self._db() as db:
            db.lock(key="job:" + job_id + ":" + str(generation))
            inserted = db.execute(
                """INSERT INTO ai_artifacts.job_runs(job_id,lease_generation,input_hash,status)
                   VALUES(%s,%s,%s,'running') ON CONFLICT(store_key,job_id,lease_generation) DO NOTHING
                   RETURNING job_id""",
                (job_id, generation, input_hash),
            ).fetchone() is not None
            row = db.execute(
                "SELECT * FROM ai_artifacts.job_runs WHERE job_id=%s AND lease_generation=%s FOR UPDATE",
                (job_id, generation),
            ).fetchone()
            if row["input_hash"] != input_hash:
                raise ValueError("job generation input conflict")
            if inserted:
                return {"status": "started"}
            if row["status"] == "reset":
                db.execute(
                    """UPDATE ai_artifacts.job_runs SET status='running',reason=NULL,updated_at=CURRENT_TIMESTAMP
                       WHERE job_id=%s AND lease_generation=%s""", (job_id, generation)
                )
                return {"status": "started"}
            return {"status": row["status"], "message_id": row["message_id"],
                    "payload": json.loads(row["payload"]) if row["payload"] else None, "reason": row["reason"]}

    def complete_job(self, job_id: str, generation: int, input_hash: str, message_id: str, payload: Any):
        """Persist candidate and outbox message atomically before network delivery."""
        encoded = canonical_json(payload)
        with self._db() as db:
            db.lock(key="job:" + job_id + ":" + str(generation))
            row = db.execute(
                "SELECT * FROM ai_artifacts.job_runs WHERE job_id=%s AND lease_generation=%s FOR UPDATE",
                (job_id, generation),
            ).fetchone()
            if not row or row["input_hash"] != input_hash:
                raise ValueError("job run reservation missing or changed")
            if row["status"] == "complete":
                if row["message_id"] != message_id or row["payload"] != encoded:
                    raise ValueError("completed job payload conflict")
                return json.loads(row["payload"])
            if row["status"] != "running":
                raise ValueError("job run is not executable")
            db.execute(
                """INSERT INTO ai_artifacts.outbox(message_id,job_id,payload,retry_deadline)
                   VALUES(%s,%s,%s,%s) ON CONFLICT(store_key,message_id) DO NOTHING""",
                (message_id, job_id, encoded, time.time() + 86400),
            )
            pending = db.execute("SELECT payload FROM ai_artifacts.outbox WHERE message_id=%s", (message_id,)).fetchone()
            if not pending or pending["payload"] != encoded:
                raise ValueError("outbox message id payload conflict")
            db.execute(
                """UPDATE ai_artifacts.job_runs SET status='complete',message_id=%s,payload=%s,updated_at=CURRENT_TIMESTAMP
                   WHERE job_id=%s AND lease_generation=%s""", (message_id, encoded, job_id, generation)
            )
        return json.loads(encoded)

    def list_job_runs(self):
        with self._db() as db:
            return [dict(row) for row in db.execute(
                """SELECT job_id,lease_generation,input_hash,status,message_id,reason,updated_at
                   FROM ai_artifacts.job_runs ORDER BY updated_at"""
            )]

    def reset_job_run(self, job_id: str, generation: int, *, force: bool = False):
        """Explicitly authorize rerun after operator review of uncertain provider spend."""
        if force is not True:
            raise ValueError("explicit force confirmation is required")
        with self._db() as db:
            db.lock(key="job:" + job_id + ":" + str(generation))
            row = db.execute(
                "SELECT status FROM ai_artifacts.job_runs WHERE job_id=%s AND lease_generation=%s FOR UPDATE",
                (job_id, generation),
            ).fetchone()
            if not row:
                raise KeyError("job run not found")
            if row["status"] not in ("running", "dead"):
                raise ValueError("only interrupted/dead runs may be reset")
            db.execute(
                """UPDATE ai_artifacts.job_runs SET status='reset',reason='operator_reset',updated_at=CURRENT_TIMESTAMP
                   WHERE job_id=%s AND lease_generation=%s""", (job_id, generation)
            )

    def quarantine_job_run(self, job_id: str, generation: int, reason: str):
        with self._db() as db:
            db.lock(key="job:" + job_id + ":" + str(generation))
            db.execute(
                """UPDATE ai_artifacts.job_runs SET status='dead',reason=%s,updated_at=CURRENT_TIMESTAMP
                   WHERE job_id=%s AND lease_generation=%s""", (reason, job_id, generation)
            )

    def pending(self):
        """All live outbox rows for backwards-compatible operator visibility."""
        with self._db() as db:
            return [dict(row) for row in db.execute(
                """SELECT message_id,job_id,payload,attempts,next_attempt_at,retry_deadline,claim_until,last_error
                   FROM ai_artifacts.outbox ORDER BY created_at,message_id"""
            )]

    def has_pending(self):
        with self._db() as db:
            return db.execute("SELECT 1 FROM ai_artifacts.outbox LIMIT 1").fetchone() is not None

    def pending_ids(self, limit: int = 10):
        with self._db() as db:
            return [row[0] for row in db.execute(
                "SELECT message_id FROM ai_artifacts.outbox ORDER BY created_at,message_id LIMIT %s",
                (max(1, min(10, int(limit))),),
            )]

    def due_pending(self, now: float | None = None, limit: int = 10, max_attempts: int = 8,
                    exclude_ids=(), claim_seconds: float = 60):
        """Atomically reserve due entries; reserve one immediately before each send."""
        now = time.time() if now is None else float(now)
        limit = max(1, min(10, int(limit)))
        max_attempts = max(1, min(8, int(max_attempts)))
        claim_seconds = max(1.0, float(claim_seconds))
        with self._db() as db:
            db.lock(key="outbox-claim")
            expired = db.execute(
                "SELECT message_id FROM ai_artifacts.outbox WHERE retry_deadline<=%s AND claim_until<=%s",
                (now, now),
            ).fetchall()
            for row in expired:
                self._quarantine_in_transaction(db, row["message_id"], "retry_deadline_exceeded")
            exhausted = db.execute(
                "SELECT message_id FROM ai_artifacts.outbox WHERE attempts>=%s AND claim_until<=%s", (max_attempts, now)
            ).fetchall()
            for row in exhausted:
                self._quarantine_in_transaction(db, row["message_id"], "retry_limit_exceeded")
            excluded = tuple(dict.fromkeys(str(item) for item in exclude_ids))
            exclusion_sql = " AND message_id <> ALL(%s)" if excluded else ""
            params = (now, now, max_attempts, now, list(excluded), limit) if excluded else (now, now, max_attempts, now, limit)
            rows = db.execute(
                """SELECT message_id,job_id,payload,attempts,retry_deadline
                   FROM ai_artifacts.outbox WHERE next_attempt_at<=%s AND retry_deadline>%s
                   AND attempts<%s AND claim_until<=%s""" + exclusion_sql +
                " ORDER BY created_at,message_id LIMIT %s",
                params,
            ).fetchall()
            reserved = []
            for row in rows:
                token = uuid.uuid4().hex
                changed = db.execute(
                    """UPDATE ai_artifacts.outbox SET attempts=attempts+1,claim_until=%s,claim_token=%s,last_error=NULL
                       WHERE message_id=%s AND claim_until<=%s RETURNING message_id""",
                    (now + claim_seconds, token, row["message_id"], now),
                ).fetchone()
                if changed:
                    item = dict(row)
                    item["attempts"] += 1
                    item["claim_token"] = token
                    item["claim_until"] = now + claim_seconds
                    reserved.append(item)
            return reserved

    def retry_delivery(self, message_id: str, *, next_attempt_at: float, error: str, max_attempts: int = 8,
                       claim_token: str | None = None, now: float | None = None):
        now = time.time() if now is None else float(now)
        with self._db() as db:
            db.lock(key="outbox:" + message_id)
            row = db.execute(
                """SELECT job_id,payload,attempts,retry_deadline,claim_token FROM ai_artifacts.outbox
                   WHERE message_id=%s FOR UPDATE""", (message_id,)
            ).fetchone()
            if not row or (claim_token is not None and row["claim_token"] != claim_token):
                return False
            if row["attempts"] >= min(8, max(1, int(max_attempts))):
                return self._quarantine_in_transaction(db, message_id, "retry_limit_exceeded", claim_token)
            if row["retry_deadline"] <= now:
                return self._quarantine_in_transaction(db, message_id, "retry_deadline_exceeded", claim_token)
            where_token = " AND claim_token=%s" if claim_token is not None else ""
            params = ((float(next_attempt_at), str(error)[:200], message_id, claim_token)
                      if claim_token is not None else (float(next_attempt_at), str(error)[:200], message_id))
            return db.execute(
                "UPDATE ai_artifacts.outbox SET next_attempt_at=%s,claim_until=0,claim_token=NULL,last_error=%s WHERE message_id=%s" + where_token,
                params,
            ).rowcount == 1

    def dead_letters(self):
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT message_id,job_id,payload,reason,failed_at FROM ai_artifacts.outbox_dead ORDER BY failed_at,message_id"
            )]

    @staticmethod
    def _quarantine_in_transaction(db, message_id: str, reason: str, claim_token: str | None = None):
        where_token = " AND claim_token=%s" if claim_token is not None else ""
        params = (message_id, claim_token) if claim_token is not None else (message_id,)
        row = db.execute(
            "SELECT job_id,payload FROM ai_artifacts.outbox WHERE message_id=%s" + where_token + " FOR UPDATE", params
        ).fetchone()
        if not row:
            return False
        db.execute(
            """INSERT INTO ai_artifacts.outbox_dead(message_id,job_id,payload,reason)
               VALUES(%s,%s,%s,%s) ON CONFLICT(store_key,message_id) DO UPDATE
               SET job_id=EXCLUDED.job_id,payload=EXCLUDED.payload,reason=EXCLUDED.reason,failed_at=CURRENT_TIMESTAMP""",
            (message_id, row["job_id"], row["payload"], reason),
        )
        db.execute("DELETE FROM ai_artifacts.outbox WHERE message_id=%s" + where_token, params)
        db.execute(
            "UPDATE ai_artifacts.job_runs SET status='dead',reason=%s,updated_at=CURRENT_TIMESTAMP WHERE message_id=%s",
            (reason, message_id),
        )
        return True

    def acknowledge(self, message_id: str, claim_token: str | None = None):
        with self._db() as db:
            db.lock(key="outbox:" + message_id)
            where_token = " AND claim_token=%s" if claim_token is not None else ""
            params = (message_id, claim_token) if claim_token is not None else (message_id,)
            deleted = db.execute(
                "DELETE FROM ai_artifacts.outbox WHERE message_id=%s" + where_token + " RETURNING message_id", params
            ).fetchone() is not None
            if deleted:
                db.execute(
                    """UPDATE ai_artifacts.job_runs SET status='acknowledged',updated_at=CURRENT_TIMESTAMP
                       WHERE message_id=%s AND status='complete'""", (message_id,)
                )
            return deleted

    def quarantine(self, message_id: str, reason: str, claim_token: str | None = None):
        with self._db() as db:
            db.lock(key="outbox:" + message_id)
            return self._quarantine_in_transaction(db, message_id, reason, claim_token)
