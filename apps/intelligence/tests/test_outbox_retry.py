import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from intelligence.artifacts import ArtifactStore, canonical_json
from intelligence.worker import Worker
from test_worker_runtime import Core, job


def test_unavailable_old_message_does_not_block_later_result_or_claim(tmp_path):
    store = ArtifactStore(tmp_path / "outbox.db")
    store.enqueue("blocked", "old-job", {"message_id":"blocked", "tenant_scope":"tenant-a"})
    store.enqueue("ok", "other-job", {"message_id":"ok", "tenant_scope":"tenant-a"})

    class Mixed(Core):
        def __init__(self):
            super().__init__(job()); self.claims = 0
        def claim(self): self.claims += 1; return super().claim()
        def submit_result(self, payload):
            self.results.append(payload)
            if payload["message_id"] == "blocked": raise TimeoutError()
            return {"accepted":True, "message_id":payload["message_id"]}

    class Pipeline:
        calls = 0
        def run(self, request, checkpoint=None):
            self.calls += 1
            return {"status":"ready", "tenant_id":request.tenant_id, "company_id":request.company_id, "request_id":request.request_id}

    core, pipeline = Mixed(), Pipeline()
    result = Worker(core, pipeline, store, tenant_ids={"tenant-a"}, jitter=lambda low, high: 1).run_once()
    assert core.claims == 1
    assert [item["message_id"] for item in core.results[:2]] == ["blocked", "ok"]
    assert result["status"] == "submitted"
    assert pipeline.calls == 1
    assert len(core.results) == 3  # the newly claimed job result was also sent
    assert [entry["message_id"] for entry in store.pending()] == ["blocked"]


def test_backoff_suppresses_immediate_retry_and_retry_reuses_exact_payload(tmp_path):
    store = ArtifactStore(tmp_path / "outbox.db")
    exact = {"message_id":"m1", "tenant_scope":"tenant-a", "candidate":{"v":1}}
    store.enqueue("m1", "j1", exact)
    now = [1000.0]

    class NoClaim(Core):
        def __init__(self): super().__init__(None); self.claims = 0
        def claim(self): self.claims += 1; return None
        def submit_result(self, payload): self.results.append(payload); raise TimeoutError()

    core = NoClaim()
    worker = Worker(core, None, store, tenant_ids={"tenant-a"}, clock=lambda:now[0],
                    retry_base_seconds=5, jitter=lambda low, high:1)
    assert worker.run_once()["status"] == "pending"
    row = store.pending()[0]
    assert row["attempts"] == 1 and row["next_attempt_at"] == 1005
    assert worker.run_once()["status"] == "pending"
    assert len(core.results) == 1
    now[0] += 5
    worker.run_once()
    assert len(core.results) == 2
    assert core.results[0] == exact == core.results[1]


def test_exhausted_delivery_is_quarantined(tmp_path):
    store = ArtifactStore(tmp_path / "outbox.db")
    store.enqueue("m1", "j1", {"message_id":"m1", "tenant_scope":"tenant-a"})
    now = [100.0]
    class Failing(Core):
        def __init__(self): super().__init__(None)
        def claim(self): return None
        def submit_result(self, payload): raise TimeoutError()
    worker = Worker(Failing(), None, store, tenant_ids={"tenant-a"}, clock=lambda:now[0],
                    max_delivery_attempts=2, retry_base_seconds=1, jitter=lambda low, high:1)
    worker.run_once()
    now[0] += 1
    worker.run_once()
    assert store.pending() == []
    assert store.dead_letters()[0]["reason"] == "retry_limit_exceeded"


def test_old_sqlite_outbox_migrates_without_changing_payload(tmp_path):
    path = tmp_path / "old.db"
    payload = {"message_id":"m", "tenant_scope":"tenant-a"}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE outbox(message_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO outbox(message_id,job_id,payload) VALUES (?,?,?)", ("m", "j", canonical_json(payload)))
    store = ArtifactStore(path)
    row = store.pending()[0]
    assert row["attempts"] == 0 and row["retry_deadline"] > 0
    assert row["payload"] == canonical_json(payload)
    assert store.due_pending(now=row["retry_deadline"]-100, limit=1)[0]["payload"] == canonical_json(payload)


def test_parallel_workers_do_not_send_same_reserved_entry(tmp_path):
    store = ArtifactStore(tmp_path / "outbox.db")
    store.enqueue("m1", "j1", {"message_id":"m1", "tenant_scope":"tenant-a"})
    entered, release = threading.Event(), threading.Event()
    class Blocking(Core):
        def __init__(self): super().__init__(None); self.lock=threading.Lock(); self.active=set(); self.overlap=False
        def claim(self): return None
        def submit_result(self, payload):
            self.results.append(payload)
            with self.lock:
                if payload["message_id"] in self.active: self.overlap=True
                self.active.add(payload["message_id"])
            entered.set(); release.wait(2)
            with self.lock: self.active.remove(payload["message_id"])
            raise TimeoutError()
    core=Blocking()
    workers=[Worker(core,None,store,tenant_ids={"tenant-a"},jitter=lambda low,high:1) for _ in range(2)]
    first=threading.Thread(target=workers[0].run_once); first.start(); assert entered.wait(1)
    second=threading.Thread(target=workers[1].run_once); second.start()
    second.join(1); release.set(); first.join(2)
    assert not core.overlap
    assert len(core.results)==1
    assert len(core.active)==0


def test_retry_deadline_moves_row_to_visible_quarantine(tmp_path):
    path = tmp_path / "outbox.db"
    store = ArtifactStore(path)
    store.enqueue("m1", "j1", {"message_id":"m1", "tenant_scope":"tenant-a"})
    with sqlite3.connect(path) as db:
        db.execute("UPDATE outbox SET retry_deadline=99 WHERE message_id='m1'")

    class NoCall(Core):
        def __init__(self): super().__init__(None)
        def claim(self): return None
        def submit_result(self, payload): raise AssertionError("expired entry must not be sent")

    result = Worker(NoCall(), None, store, tenant_ids={"tenant-a"}, clock=lambda:100).run_once()
    assert result["status"] == "quarantined"
    assert store.pending() == []
    assert store.dead_letters()[0]["reason"] == "retry_deadline_exceeded"


def test_batch_claim_lease_starts_immediately_before_each_send(tmp_path):
    store = ArtifactStore(tmp_path / "outbox.db")
    store.enqueue("m1", "j1", {"message_id":"m1", "tenant_scope":"tenant-a"})
    store.enqueue("m2", "j2", {"message_id":"m2", "tenant_scope":"tenant-a"})
    now = [100.0]
    class SlowFirst(Core):
        def __init__(self): super().__init__(None); self.claim_times=[]
        def claim(self): return None
        def submit_result(self, payload):
            self.results.append(payload)
            row = next(item for item in store.pending() if item["message_id"] == payload["message_id"])
            self.claim_times.append(row["claim_until"])
            if payload["message_id"] == "m1":
                now[0] = 200.0
                raise TimeoutError()
            return {"accepted":True, "message_id":payload["message_id"]}
    core = SlowFirst()
    Worker(core, None, store, tenant_ids={"tenant-a"}, clock=lambda:now[0], jitter=lambda low,high:1).run_once()
    assert core.claim_times == [160, 260]


def test_expired_workers_cannot_overwrite_newer_claim(tmp_path):
    store = ArtifactStore(tmp_path / "outbox.db")
    store.enqueue("m1", "j1", {"message_id":"m1", "tenant_scope":"tenant-a"})
    now = [100.0]
    first_entered, release_first = threading.Event(), threading.Event()
    second_entered, release_second = threading.Event(), threading.Event()

    class First(Core):
        def __init__(self): super().__init__(None)
        def claim(self): return None
        def submit_result(self, payload):
            self.results.append(payload); first_entered.set(); release_first.wait(2); raise TimeoutError()

    class Second(Core):
        def __init__(self): super().__init__(None)
        def claim(self): return None
        def submit_result(self, payload):
            self.results.append(payload); second_entered.set(); release_second.wait(2)
            return {"accepted":True, "message_id":payload["message_id"]}

    opts = {"tenant_ids":{"tenant-a"}, "clock":lambda:now[0], "jitter":lambda low,high:1}
    first = First()
    first_thread = threading.Thread(target=lambda:Worker(first,None,store,**opts).run_once())
    first_thread.start(); assert first_entered.wait(1)
    with sqlite3.connect(store.path) as db:
        stale_token = db.execute("SELECT claim_token FROM outbox WHERE message_id='m1'").fetchone()[0]
    now[0] = 161.0
    second = Second()
    second_thread = threading.Thread(target=lambda:Worker(second,None,store,**opts).run_once())
    second_thread.start(); assert second_entered.wait(1)
    active = store.pending()[0]
    with sqlite3.connect(store.path) as db:
        active_token, active_until = db.execute("SELECT claim_token,claim_until FROM outbox WHERE message_id='m1'").fetchone()
    assert active_token != stale_token and active["attempts"] == 2
    release_first.set(); first_thread.join(1)
    with sqlite3.connect(store.path) as db:
        after_token, after_until = db.execute("SELECT claim_token,claim_until FROM outbox WHERE message_id='m1'").fetchone()
    assert after_token == active_token
    assert after_until == active_until
    release_second.set(); second_thread.join(1)
    assert store.pending() == []


def test_two_initializers_migrate_old_outbox_schema_safely(tmp_path):
    path = tmp_path / "old-concurrent.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE outbox(message_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO outbox(message_id,job_id,payload) VALUES ('m','j','{}')")

    stores = []
    errors = []
    barrier = threading.Barrier(2)
    def initialize():
        try:
            barrier.wait(timeout=1)
            stores.append(ArtifactStore(path))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=initialize) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(2)
    assert not errors
    assert len(stores) == 2
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(outbox)")}
    assert {"attempts", "next_attempt_at", "retry_deadline", "claim_until", "claim_token", "last_error"} <= columns
    assert len(stores[0].pending()) == 1


def test_stale_ack_and_quarantine_cannot_mutate_newer_claim(tmp_path):
    store = ArtifactStore(tmp_path / "outbox.db")
    store.enqueue("m1", "j1", {"message_id":"m1", "tenant_scope":"tenant-a"})
    first = store.due_pending(now=100, limit=1)[0]
    second = store.due_pending(now=161, limit=1)[0]
    assert first["claim_token"] != second["claim_token"]
    assert store.acknowledge("m1", first["claim_token"]) is False
    assert store.quarantine("m1", "stale_lease", first["claim_token"]) is False
    assert store.retry_delivery("m1", next_attempt_at=200, error="late", claim_token=first["claim_token"], now=161) is False
    assert [row["message_id"] for row in store.pending()] == ["m1"]
    assert store.dead_letters() == []
    assert store.acknowledge("m1", second["claim_token"]) is True
    assert store.pending() == []
