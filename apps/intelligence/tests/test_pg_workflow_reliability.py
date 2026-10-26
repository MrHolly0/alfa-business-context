"""PostgreSQL workflow lease and transaction-boundary checks; no live model/network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import multiprocessing
import os
import time
from threading import Event

import psycopg
import pytest

from intelligence.collection import FetchResult, NewsCollector
from intelligence.workflow import (
    BusinessSnapshot, DataTask, DataWorkflow, WorkflowNotStarted,
    WorkflowSnapshotConflict, WorkflowState,
)
from test_workflow import NOW, company, grant, policy


def _task(request_id):
    business = company()
    source_policy = policy(grant(), tenant="tenant-pg-reliability")
    return DataTask(
        request_id=request_id, tenant_id="tenant-pg-reliability",
        company_id=business["company_id"], context_version=business["context_version"],
        source_policy_version=source_policy.revision, operation="collect", trigger="scheduled",
        as_of=NOW, source_policy=source_policy,
        businesses=[BusinessSnapshot.model_validate(business)],
        data_classification="public",
    )


def _slot_claim_worker(path, task_data, owner, start, release, results):
    task = DataTask.model_validate(task_data)
    state = WorkflowState(path)
    start.wait(timeout=20)
    try:
        state.claim_execution(task, owner, capacity=1, ttl=60)
        results.put(("claimed", owner))
        release.wait(timeout=20)
        state.release_execution(task, owner, success=False)
    except WorkflowNotStarted as exc:
        results.put((exc.code, owner))


def test_independent_processes_compete_for_single_workflow_slot(tmp_path):
    path = str(tmp_path / "workflow.pg")
    task_a, task_b = _task("check-slot-a"), _task("check-slot-b")
    context = multiprocessing.get_context("spawn")
    start, release = context.Barrier(3), context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_slot_claim_worker, args=(path, task.model_dump(mode="json"), owner, start, release, results))
        for task, owner in ((task_a, "owner-a"), (task_b, "owner-b"))
    ]
    for process in processes:
        process.start()
    start.wait(timeout=20)
    outcomes = [results.get(timeout=20) for _ in processes]
    release.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results.close()
    assert sorted(result[0] for result in outcomes) == ["capacity_exhausted", "claimed"]


def test_stale_analysis_result_cannot_replace_newer_result_via_workflow_state(tmp_path):
    state = WorkflowState(tmp_path / "workflow.pg")
    newer = {"candidate": {"versions": {"as_of": "2026-10-24T12:00:00+00:00", "marker": "new"}}}
    older = {"candidate": {"versions": {"as_of": "2026-10-23T12:00:00+00:00", "marker": "old"}}}
    state.save_analysis("tenant-pg-reliability", "company-a", "event-a", "sig-new", newer)
    with pytest.raises(WorkflowSnapshotConflict, match="stale_analysis_as_of"):
        state.save_analysis("tenant-pg-reliability", "company-a", "event-a", "sig-old", older)
    persisted = state.prior("tenant-pg-reliability", "company-a", "event-a")
    assert persisted["candidate"]["versions"]["marker"] == "new"
    assert persisted["candidate"]["versions"]["as_of"] == "2026-10-24T12:00:00+00:00"


def test_uncertain_expired_execution_requires_explicit_recovery_before_retry(tmp_path):
    state = WorkflowState(tmp_path / "workflow.pg")
    task = _task("check-explicit-recovery")
    state.claim_execution(task, "first-owner", capacity=1, ttl=0.05)
    state.release_execution(task, "first-owner", success=False)
    with pytest.raises(WorkflowNotStarted) as blocked:
        state.claim_execution(task, "retry-owner", capacity=1, ttl=60)
    assert blocked.value.code == "execution_recovery_required"

    time.sleep(0.08)
    state.recover_execution(task.tenant_id, task.request_id)
    state.claim_execution(task, "retry-owner", capacity=1, ttl=60)
    state.release_execution(task, "retry-owner", success=True)
    with state.db() as db:
        row = db.execute("SELECT state,owner FROM workflow_executions WHERE tenant=%s AND request=%s",
            (task.tenant_id, task.request_id)).fetchone()
        slots = db.execute("SELECT count(*) FROM workflow_slots WHERE owner=%s", ("retry-owner",)).fetchone()[0]
    assert row["state"] == "complete" and row["owner"] == "retry-owner"
    assert slots == 0


class _BlockingFetcher:
    def __init__(self, entered, release):
        self.entered, self.release = entered, release

    def __call__(self, url, headers, **options):
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test_fetch_release_timeout")
        body = (b'<rss version="2.0"><channel><title>fixture</title></channel></rss>')
        return FetchResult(status=200, headers={"content-type": "application/rss+xml"}, body=body)


def test_blocking_http_does_not_hold_open_postgres_transaction(tmp_path):
    dsn = os.environ.get("INTELLIGENCE_DATABASE_URL")
    if not dsn:
        pytest.skip("dedicated PostgreSQL test DSN required")
    workflow = DataWorkflow(tmp_path / "state")
    entered, release = Event(), Event()
    workflow.collector = NewsCollector(workflow.store,
        state_path=workflow.state_dir / "poll.sqlite3",
        fetcher=_BlockingFetcher(entered, release), raw_dir=workflow.state_dir / "raw")
    task = _task("check-no-transaction-during-http")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(workflow.run, task)
        try:
            assert entered.wait(timeout=5), "workflow never reached fake HTTP fetch"
            # The worker's database pool should be idle while user-controlled I/O blocks.
            with psycopg.connect(dsn, autocommit=True) as connection:
                active = connection.execute("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname=current_database()
                      AND application_name=%s
                      AND pid<>pg_backend_pid()
                      AND state='idle in transaction'
                      AND xact_start IS NOT NULL
                """, (f"business-context-intelligence:{os.getpid()}",)).fetchone()[0]
            assert active == 0
        finally:
            release.set()
        result = future.result(timeout=15)
    assert result.collection["fetched"] == 1
