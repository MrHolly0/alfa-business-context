"""Reservation fencing and safe retry after workflow capacity rejection."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from intelligence.api import create_app
from intelligence.artifacts import ArtifactStore
from intelligence.pipeline import AnalysisPipeline
from intelligence.workflow import DataResult
from test_workflow import task, workflow


AUTH = {"Authorization": "Bearer capacity-test"}


def test_busy_capacity_can_be_retried_and_executes_once(tmp_path):
    data, fetcher = workflow(tmp_path)
    completed = []

    def finish(task_value, checkpoint):
        completed.append(task_value.request_id)
        return DataResult(request_id=task_value.request_id, tenant_id=task_value.tenant_id,
            company_id=task_value.company_id, status="completed", versions={},
            created_at=datetime.now(timezone.utc))

    data._run = finish
    artifacts = ArtifactStore(tmp_path / "artifacts.sqlite3")
    client = TestClient(create_app(store=data.store, pipeline=AnalysisPipeline(data.store),
        data_workflow=data, artifact_store=artifacts, service_token="capacity-test", tenant_ids={"tenant-a"}))
    locks = [data._acquire_slot_lock() for _ in range(data.max_concurrency)]
    assert all(locks)
    body = task(tmp_path, request_id="same-request").model_dump(mode="json")
    try:
        busy = client.post("/internal/v1/analyze", headers=AUTH, json=body)
        assert busy.status_code == 409 and busy.json()["detail"] == "capacity_exhausted"
        assert artifacts.list_reservations() == []
    finally:
        for lock in locks:
            data._release_file_lock(lock)

    result = client.post("/internal/v1/analyze", headers=AUTH, json=body)
    assert result.status_code == 200, result.text
    replay = client.post("/internal/v1/analyze", headers=AUTH, json=body)
    assert replay.status_code == 200 and replay.json() == result.json()
    assert len(completed) == 1


def test_unknown_failure_after_start_keeps_reservation(tmp_path):
    data, _ = workflow(tmp_path)

    class FailsAfterStart:
        def run(self, request):
            raise RuntimeError("provider outcome may be uncertain")

    artifacts = ArtifactStore(tmp_path / "uncertain.sqlite3")
    client = TestClient(create_app(store=data.store, pipeline=AnalysisPipeline(data.store),
        data_workflow=FailsAfterStart(), artifact_store=artifacts,
        service_token="capacity-test", tenant_ids={"tenant-a"}), raise_server_exceptions=False)
    body = task(tmp_path, request_id="uncertain-request").model_dump(mode="json")
    assert client.post("/internal/v1/analyze", headers=AUTH, json=body).status_code == 500
    retry = client.post("/internal/v1/analyze", headers=AUTH, json=body)
    assert retry.status_code == 409 and retry.json()["detail"] == "request_in_progress"
    assert len(artifacts.list_reservations()) == 1


def test_reservation_owner_token_fences_completion_and_release(tmp_path):
    store = ArtifactStore(tmp_path / "fence.sqlite3")
    owner = store.claim_candidate("tenant", "request", "digest")
    assert owner and store.claim_candidate("tenant", "request", "digest") is None
    assert store.release_candidate("tenant", "request", "digest", owner)
    next_owner = store.claim_candidate("tenant", "request", "digest")
    assert next_owner and next_owner != owner
    assert not store.release_candidate("tenant", "request", "digest", owner)
    with pytest.raises(ValueError, match="reservation_owner_mismatch"):
        store.save_candidate("tenant", "request", "digest", {"ok": True}, owner_token=owner)
    assert store.save_candidate("tenant", "request", "digest", {"ok": True}, owner_token=next_owner) == {"ok": True}


def test_concurrent_claim_has_one_owner(tmp_path):
    store = ArtifactStore(tmp_path / "race.sqlite3")
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return store.claim_candidate("tenant", "request", "digest")

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda _: claim(), range(2)))
    assert sum(token is not None for token in tokens) == 1
