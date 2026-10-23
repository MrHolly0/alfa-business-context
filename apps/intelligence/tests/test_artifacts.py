import pytest

from intelligence.artifacts import ArtifactStore, payload_hash


def test_candidate_is_durable_and_idempotent(tmp_path):
    path = tmp_path / "artifacts.sqlite"
    first = ArtifactStore(path)
    digest = payload_hash({"request_id": "r", "x": 1})
    assert first.save_candidate("t", "r", digest, {"status": "ready"}) == {"status": "ready"}
    assert ArtifactStore(path).get_candidate("t", "r", digest) == {"status": "ready"}
    with pytest.raises(ValueError): first.get_candidate("t", "r", payload_hash({"x": 2}))


def test_outbox_reuses_exact_message_and_rejects_changed_payload(tmp_path):
    store = ArtifactStore(tmp_path / "outbox.sqlite")
    store.enqueue("m", "j", {"x": 1})
    assert ArtifactStore(tmp_path / "outbox.sqlite").pending()[0]["payload"] == '{"x":1}'
    with pytest.raises(ValueError): store.enqueue("m", "j", {"x": 2})


def test_job_run_fence_persists_candidate_and_requires_explicit_reset(tmp_path):
    store=ArtifactStore(tmp_path/"runs.sqlite")
    digest=payload_hash({"snapshot":1})
    assert store.begin_job("j",2,digest)["status"]=="started"
    assert store.begin_job("j",2,digest)["status"]=="running"
    with pytest.raises(ValueError): store.begin_job("j",2,payload_hash({"snapshot":2}))
    payload={"message_id":"m","candidate":{"status":"ready"}}
    store.complete_job("j",2,digest,"m",payload)
    assert store.pending()[0]["payload"]=='{"candidate":{"status":"ready"},"message_id":"m"}'
    assert store.begin_job("j",2,digest)["status"]=="complete"
    store.acknowledge("m")
    assert store.begin_job("j",2,digest)["status"]=="acknowledged"


def test_interrupted_job_reset_requires_explicit_force(tmp_path):
    store=ArtifactStore(tmp_path/"runs.sqlite")
    store.begin_job("j",1,payload_hash({"input":1}))
    with pytest.raises(ValueError): store.reset_job_run("j",1)
    store.reset_job_run("j",1,force=True)
    assert store.begin_job("j",1,payload_hash({"input":1}))["status"]=="started"
