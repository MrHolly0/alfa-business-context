from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import pytest
from fastapi.testclient import TestClient
from intelligence.workflow import DataTask

from intelligence.core_client import CoreClient
from intelligence.core_stand import CoreStand, create_app
from intelligence.artifacts import ArtifactStore
from intelligence.worker import Worker


def local_client(app):
    return TestClient(app, client=("127.0.0.1", 12345))


class Clock:
    def __init__(self):
        self.value = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def envelope(schema="ai-job-v1", *, job_id="job-a", context=2, policy=3):
    task_key = "task" if schema == "ai-job-v1" else "analysis"
    task = {"operation": "collect", "tenant_id": "tenant-a", "company_id": "company-a",
            "context_version": context, "source_policy_version": policy, "source_ids": ["wire"],
            "document_revision_refs": []}
    return {"schema_version": schema, "message_id": "seed-" + job_id, "job_id": job_id,
            "tenant_scope": "tenant-a", "company_id": "company-a", "trace_id": "trace-a",
            "lease_generation": 1,
            "input_versions": {"context_version": context, "source_policy_version": policy,
                               "event_versions": [] if schema == "ai-job-v1" else [{"event_id": "event-a", "revision": 1}]},
            "deadline": "2030-01-01T01:00:00Z", "lease_until": "2030-01-01T00:01:00Z",
            "budget": {"max_model_calls": 0, "max_tokens": 1, "cost_reservation_rub": 0,
                       "cost_reservation_usd": 0},
            "scope": {"source_ids": ["wire"], "document_revision_refs": []}, task_key: task}


def result_for(job, *, message="result-a", candidate=None):
    kind = "analysis-result-v1" if job["schema_version"] == "analysis-job-v1" else "ai-result-v1"
    return {"schema_version": kind, "message_id": message, "job_id": job["job_id"],
            "tenant_scope": job["tenant_scope"], "company_id": job["company_id"],
            "trace_id": job["trace_id"], "lease_generation": job["lease_generation"],
            "input_versions": job["input_versions"],
            "candidate": candidate or {"versions": {"context_version": 2, "source_policy_version": 3},
                                       "publication_revision_required": True}}


def test_fixture_opt_in_loopback_auth_and_durable_state(tmp_path):
    with pytest.raises(ValueError, match="fixture_mode"):
        create_app(tmp_path / "state.db")
    app = create_app(tmp_path / "state.db", fixture_mode=True, operator_token="test-token")
    with local_client(app) as client:
        assert client.get("/health").json()["mode"] == "fixture"
        assert client.post("/fixture/seed", json={"job": envelope()}).status_code == 401
        seeded = client.post("/fixture/seed", headers={"X-Operator-Token": "test-token"},
                             json={"job": envelope()})
        assert seeded.status_code == 200
        assert client.post("/internal/v1/analysis-jobs/claim", headers={"Authorization": "Bearer test-token"}, json={}).json()["job"]["task"]["operation"] == "collect"
    app2 = create_app(tmp_path / "state.db", fixture_mode=True, operator_token="test-token")
    with local_client(app2) as client:
        state = client.get("/fixture/state", headers={"X-Operator-Token": "test-token"}).json()
        assert state["jobs"][0]["status"] == "leased"


def test_heartbeat_result_dedup_and_no_revision_for_no_change(tmp_path):
    stand = CoreStand(tmp_path / "db.sqlite", clock=Clock())
    job = envelope()
    stand.seed_job(job)
    leased = stand.claim()["job"]
    beat = stand.heartbeat(job["job_id"], 1)
    assert beat["accepted"] and beat["lease_generation"] == 1
    first = result_for(leased, candidate={"schema_version": "ai-data-result-v1", "status": "no_change",
                                          "versions": {"context": 2, "source_policy": 3},
                                          "revisions": [{"company_id": "company-a", "event_id": "event-a",
                                                         "status": "no_change", "publication_revision_required": False}]})
    ack, status = stand.submit_result(first)
    assert status == 200 and ack["accepted"] and ack["duplicate"] is False
    assert stand.inspect()["published"] == []
    assert stand.submit_result(first)[0]["duplicate"] is True
    changed = result_for(leased, message="different")
    assert stand.submit_result(changed)[1] == 409


def test_expired_lease_requeues_with_new_generation(tmp_path):
    clock = Clock()
    stand = CoreStand(tmp_path / "db.sqlite", clock=clock, lease_seconds=10)
    stand.seed_job(envelope())
    first = stand.claim()["job"]
    assert first["lease_generation"] == 1
    clock.value += timedelta(seconds=11)
    second = stand.claim()["job"]
    assert second["lease_generation"] == 2
    assert stand.submit_result(result_for(first))[1] == 409


def test_publication_signature_dedup_and_proof_for_later_tasks(tmp_path):
    stand = CoreStand(tmp_path / "db.sqlite", clock=Clock())

    def publish(job_id, signature):
        job = envelope(job_id=job_id)
        stand.seed_job(job)
        leased = stand.claim()["job"]
        candidate = {"schema_version": "ai-data-result-v1", "status": "completed",
                     "versions": {"context": 2, "source_policy": 3},
                     "revisions": [{"company_id": "company-a", "event_id": "event-a",
                                    "input_signature": signature, "assessment_revision": 1,
                                    "status": "changed", "publication_revision_required": True}]}
        return stand.submit_result(result_for(leased, message=f"result-{job_id}", candidate=candidate))

    assert publish("first", "same-inputs")[1] == 200
    assert publish("second", "same-inputs")[1] == 200
    assert stand.inspect()["published"][0]["revision"] == 1
    assert stand.published_assessments("tenant-a", "company-a") == [
        {"company_id": "company-a", "event_id": "event-a", "input_signature": "same-inputs", "assessment_revision": 1}
    ]
    assert publish("third", "changed-inputs")[1] == 200
    state = stand.inspect()
    assert state["published"][0]["revision"] == 2
    assert len(state["publication_history"]) == 2


def test_version_change_rejects_old_result_and_cancel_rejects_lease(tmp_path):
    stand = CoreStand(tmp_path / "db.sqlite", clock=Clock())
    job = envelope()
    stand.seed_job(job)
    leased = stand.claim()["job"]
    assert stand.update_versions("tenant-a", "company-a", 4, 3) == 1
    assert stand.submit_result(result_for(leased))[1] == 409
    assert stand.cancel(job["job_id"])["accepted"] is True

    other = envelope(job_id="job-b")
    stand.seed_job(other)
    assert stand.cancel("job-b")["accepted"] is True
    assert stand.claim() is None


def test_old_analysis_contract_supported(tmp_path):
    stand = CoreStand(tmp_path / "db.sqlite", clock=Clock())
    job = envelope("analysis-job-v1", job_id="legacy")
    stand.seed_job(job)
    leased = stand.claim()["job"]
    ack, status = stand.submit_result(result_for(leased))
    assert status == 200 and ack["accepted"]


class _ASGIClient:
    """Minimal synchronous httpx-compatible transport used by CoreClient."""
    def __init__(self, app, token):
        self.client = local_client(app)
        self.token = token

    def stream(self, method, url, **kwargs):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        response = self.client.request(method, parsed.path, headers=kwargs.get("headers"), json=kwargs.get("json"))
        return _Response(response)

    def close(self):
        self.client.close()


class _Response:
    def __init__(self, response): self.response = response
    def __enter__(self): return self
    def __exit__(self, *_): return None
    @property
    def is_redirect(self): return self.response.is_redirect
    @property
    def status_code(self): return self.response.status_code
    def raise_for_status(self): self.response.raise_for_status()
    def iter_bytes(self): yield self.response.content


def test_real_core_client_routes_over_asgi_transport(tmp_path):
    app = create_app(tmp_path / "db.sqlite", fixture_mode=True, operator_token="test-token")
    app.state.core_stand.seed_job(envelope())
    core = CoreClient("http://core.test", "test-token", client=_ASGIClient(app, "test-token"))
    leased = core.claim()["job"]
    assert core.heartbeat(leased["job_id"], leased["lease_generation"])["accepted"]
    assert core.submit_result(result_for(leased))["accepted"]


def test_worker_runs_against_stand_via_real_core_client(tmp_path):
    now = datetime.now(timezone.utc)
    request = {"schema_version": "analysis-request-v1", "tenant_id": "tenant-a", "company_id": "company-a",
               "context_version": 2, "source_policy_version": 3, "request_id": "request-a",
               "as_of": now.isoformat(), "source_ids": ["wire"],
               "company": {"name": "Demo", "industry": "", "description": "", "role": "", "dependencies": [], "facts": []},
               "event": {"event_id": "event-a", "title": "News", "text": "Text", "source_id": "wire", "published_at": now.isoformat()},
               "budget": {"max_model_calls": 0, "max_input_tokens": 24000, "max_output_tokens": 2048,
                          "max_total_tokens": 60000, "max_steps": 12}}
    job = {"schema_version": "analysis-job-v1", "message_id": "seed-worker", "job_id": "worker-job",
           "tenant_scope": "tenant-a", "company_id": "company-a", "trace_id": "trace-worker",
           "lease_generation": 1, "input_versions": {"context_version": 2, "source_policy_version": 3,
                                                       "event_versions": [{"event_id": "event-a", "revision": 1}]},
           "deadline": (now + timedelta(minutes=2)).isoformat(), "lease_until": (now + timedelta(seconds=30)).isoformat(),
           "budget": {"max_model_calls": 0, "max_tokens": 60000, "cost_reservation_rub": 0},
           "scope": {"source_ids": ["wire"], "document_revision_refs": []}, "analysis": request}
    app = create_app(tmp_path / "core.db", fixture_mode=True, operator_token="secret")
    app.state.core_stand.seed_job(job)
    core = CoreClient("http://core.test", "secret", client=_ASGIClient(app, "secret"))

    class Pipeline:
        def run(self, parsed, checkpoint=None):
            return {"tenant_id": parsed.tenant_id, "company_id": parsed.company_id, "request_id": parsed.request_id,
                    "versions": {"context": parsed.context_version, "source_policy": parsed.source_policy_version}}

    worker = Worker(core, Pipeline(), ArtifactStore(tmp_path / "worker.db"), heartbeat_interval=10,
                    tenant_ids={"tenant-a"})
    result = worker.run_once()
    assert result["status"] == "submitted"
    assert len(app.state.core_stand.inspect()["published"]) == 1


def test_fixture_schedule_is_durable_bounded_and_emits_data_task(tmp_path):
    clock = Clock()
    stand = CoreStand(tmp_path / "db.sqlite", clock=clock)
    task = {"schema_version": "ai-data-task-v1", "request_id": "r1", "tenant_id": "tenant-a",
            "company_id": "company-a", "context_version": 2, "source_policy_version": 3,
            "operation": "collect", "trigger": "scheduled", "as_of": clock().isoformat(),
            "source_policy": {"policy_id": "p1", "tenant_id": "tenant-a", "revision": 3,
                              "issued_at": clock().isoformat(), "sources": [],
                              "excluded_source_ids": [], "excluded_publishers": [],
                              "user_excluded_source_ids": [], "user_excluded_publishers": [],
                              "excluded_document_uris": [], "user_excluded_document_uris": []},
            "businesses": [], "event_ids": [],
            "budget": {"max_model_calls": 0, "max_input_tokens": 100, "max_output_tokens": 20,
                       "max_total_tokens": 100, "max_steps": 2},
            "data_classification": "synthetic", "synthetic": True, "max_events": 1}
    task["source_policy"]["sources"] = [
        {"source_id": "wire", "source_role": "external", "uri": "https://wire.example/rss",
         "content_type": "application/rss+xml", "publisher": "Wire"},
        {"source_id": "company-excluded", "source_role": "external", "uri": "https://excluded.example/rss",
         "content_type": "application/rss+xml", "publisher": "Excluded"},
        {"source_id": "personally-hidden", "source_role": "external", "uri": "https://hidden.example/rss",
         "content_type": "application/rss+xml", "publisher": "Hidden"},
    ]
    task["source_policy"]["excluded_source_ids"] = ["company-excluded"]
    task["source_policy"]["user_excluded_source_ids"] = ["personally-hidden"]
    assert DataTask.model_validate(task).operation == "collect"
    with pytest.raises(ValueError, match="at least 60"):
        stand.add_schedule("s1", task, 59)
    stand.add_schedule("s1", task, 60)
    ids = stand.enqueue_due_schedules()
    assert len(ids) == 1
    assert stand.enqueue_due_schedules() == []
    claimed = stand.claim()["job"]
    assert claimed["task"]["schema_version"] == "ai-data-task-v1"
    assert claimed["input_versions"]["event_versions"] == []
    assert claimed["task"]["trigger"] == "scheduled"
    assert claimed["task"]["knowledge_mode"] == "latest"
    assert datetime.fromisoformat(claimed["task"]["as_of"].replace("Z", "+00:00")) == clock()
    assert claimed["scope"]["source_ids"] == ["wire", "personally-hidden"]


def test_fixture_schedule_endpoint_and_lifespan_create_ordinary_job(tmp_path):
    clock = Clock()
    task = {"schema_version": "ai-data-task-v1", "request_id": "schedule-template",
            "tenant_id": "tenant-a", "company_id": "company-a", "context_version": 2,
            "source_policy_version": 3, "operation": "collect", "trigger": "manual",
            "as_of": clock().isoformat(),
            "source_policy": {"policy_id": "p1", "tenant_id": "tenant-a", "revision": 3,
                              "issued_at": clock().isoformat(), "sources": [],
                              "excluded_source_ids": [], "excluded_publishers": [],
                              "user_excluded_source_ids": [], "user_excluded_publishers": [],
                              "excluded_document_uris": [], "user_excluded_document_uris": []},
            "businesses": [], "event_ids": [], "published_assessments": [],
            "budget": {"max_model_calls": 0, "max_input_tokens": 100, "max_output_tokens": 20,
                       "max_total_tokens": 100, "max_steps": 2},
            "data_classification": "synthetic", "synthetic": True, "max_events": 1}
    app = create_app(tmp_path / "schedule.db", fixture_mode=True,
                     operator_token="test-token", clock=clock)
    headers = {"X-Operator-Token": "test-token"}
    with local_client(app) as client:
        denied = client.post("/fixture/schedules", json={"schedule_id": "s1", "task": task,
                                                         "interval_seconds": 60})
        assert denied.status_code == 401
        scheduled = client.post("/fixture/schedules", headers=headers,
                                json={"schedule_id": "s1", "task": task,
                                      "interval_seconds": 60})
        assert scheduled.status_code == 200
        state = None
        for _ in range(150):
            state = client.get("/fixture/state", headers=headers).json()
            if state["jobs"]:
                break
            time.sleep(0.01)
        assert state["jobs"]
        queued = state["jobs"][0]
        assert queued["status"] == "queued"
        job = app.state.core_stand.claim()["job"]
        assert job["schema_version"] == "ai-job-v1"
        assert job["task"]["request_id"] != "schedule-template"
        assert job["task"]["knowledge_mode"] == "latest"
        assert job["budget"]["max_model_calls"] == 0



def test_secondary_company_new_version_rejects_whole_batch_before_outbox(tmp_path):
    stand=CoreStand(tmp_path/'batch.db',clock=Clock())
    job=envelope()
    job['task']['businesses']=[{'company_id':'company-a','context_version':2},
                               {'company_id':'company-b','context_version':5}]
    stand.seed_job(job)
    leased=stand.claim()['job']
    stand.update_versions('tenant-a','company-b',6,3)
    candidate={'schema_version':'ai-data-result-v1','versions':{'context':2,'source_policy':3},
               'revisions':[{'company_id':'company-b','event_id':'event-b','publication_revision_required':True}]}
    ack,status=stand.submit_result(result_for(leased,candidate=candidate))
    assert status==409 and ack['reason']=='stale_business_context'
    assert stand.inspect()['outbox']==[] and stand.inspect()['published']==[]


def test_result_cannot_publish_company_outside_batch_scope(tmp_path):
    stand=CoreStand(tmp_path/'scope.db',clock=Clock())
    stand.seed_job(envelope())
    leased=stand.claim()['job']
    candidate={'schema_version':'ai-data-result-v1','versions':{'context':2,'source_policy':3},
               'revisions':[{'company_id':'ungranted-company','event_id':'event','publication_revision_required':True}]}
    ack,status=stand.submit_result(result_for(leased,candidate=candidate))
    assert status==403 and ack['code']=='authorization_denied'
    assert stand.inspect()['outbox']==[] and stand.inspect()['published']==[]
