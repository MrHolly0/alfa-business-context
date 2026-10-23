import threading

from fastapi.testclient import TestClient

from intelligence.api import create_app
from intelligence.artifacts import ArtifactStore
from intelligence.pipeline import AnalysisPipeline
from intelligence.corpus import CorpusStore


class Store:
    def ingest(self, body): return {"document_id":"d", "tenant_id":body["tenant_id"]}
    def search(self, **kwargs): return [{"tenant_id":kwargs["tenant_id"]}]


def test_private_routes_fail_closed_and_check_tenant(tmp_path):
    class Pipeline: pass
    client=TestClient(create_app(store=Store(),pipeline=Pipeline(),artifact_store=ArtifactStore(tmp_path/"a.db")))
    assert client.get("/health").status_code==200
    assert client.post("/internal/v1/search",json={"tenant_id":"t"}).status_code==401
    client=TestClient(create_app(store=Store(),pipeline=Pipeline(),artifact_store=ArtifactStore(tmp_path/"b.db"),service_token="secret",tenant_ids={"allowed"}))
    headers={"Authorization":"Bearer secret"}
    assert client.post("/internal/v1/search",headers=headers,json={"tenant_id":"other","source_ids":["s"],"as_of":"2026-10-24T00:00:00Z","query":"x"}).status_code==403
    assert client.post("/internal/v1/search",headers=headers,json={"tenant_id":"allowed","source_ids":["s"],"as_of":"2026-10-24T00:00:00Z","query":"x"}).status_code==200


def test_analysis_request_idempotency_and_payload_conflict(tmp_path):
    class Pipeline:
        def __init__(self): self.calls=0
        def run(self, request):
            self.calls+=1
            return AnalysisPipeline(CorpusStore(tmp_path/"corpus.db")).run(request)
    pipe=Pipeline()
    client=TestClient(create_app(store=Store(),pipeline=pipe,artifact_store=ArtifactStore(tmp_path/"c.db"),service_token="secret",tenant_ids={"t"}))
    headers={"Authorization":"Bearer secret"}
    body={"schema_version":"analysis-request-v1","request_id":"r","tenant_id":"t","company_id":"co","as_of":"2026-10-24T00:00:00Z","context_version":1,"source_policy_version":1,"source_ids":["s"],"company":{"name":"Example"},"event":{"event_id":"e","title":"Title","text":"Text","source_id":"s","published_at":"2026-10-23T00:00:00Z"}}
    assert client.post("/internal/v1/analyze",headers=headers,json=body).status_code==200
    assert client.post("/internal/v1/analyze",headers=headers,json=body).status_code==200
    changed={**body,"event":{"event_id":"e","title":"Other","text":"Text","source_id":"s","published_at":"2026-10-23T00:00:00Z"}}
    assert client.post("/internal/v1/analyze",headers=headers,json=changed).status_code==409
    assert pipe.calls==1


def test_concurrent_same_request_reserves_before_model_call(tmp_path):
    entered, release=threading.Event(),threading.Event()
    class Pipeline:
        def __init__(self): self.calls=0
        def run(self, request): self.calls+=1; entered.set(); release.wait(2); return AnalysisPipeline(CorpusStore(tmp_path/"corpus.db")).run(request)
    pipe=Pipeline(); client=TestClient(create_app(store=Store(),pipeline=pipe,artifact_store=ArtifactStore(tmp_path/"d.db"),service_token="secret",tenant_ids={"t"}))
    headers={"Authorization":"Bearer secret"}
    body={"schema_version":"analysis-request-v1","request_id":"r","tenant_id":"t","company_id":"co","as_of":"2026-10-24T00:00:00Z","context_version":1,"source_policy_version":1,"source_ids":["s"],"company":{"name":"Example"},"event":{"event_id":"e","title":"Title","text":"Text","source_id":"s","published_at":"2026-10-23T00:00:00Z"}}
    first=[]
    thread=threading.Thread(target=lambda:first.append(client.post("/internal/v1/analyze",headers=headers,json=body)))
    thread.start(); assert entered.wait(1)
    concurrent=client.post("/internal/v1/analyze",headers=headers,json=body)
    release.set(); thread.join(2)
    assert concurrent.status_code==409 and first[0].status_code==200 and pipe.calls==1


def test_document_rejects_naive_dates_and_caps_text(tmp_path):
    class Pipeline: pass
    client=TestClient(create_app(store=Store(),pipeline=Pipeline(),artifact_store=ArtifactStore(tmp_path/"e.db"),service_token="secret",tenant_ids={"t"}))
    headers={"Authorization":"Bearer secret"}
    body={"tenant_id":"t","source_id":"s","source_role":"external","canonical_uri":"urn:x","title":"x","content":"abc","content_type":"text/plain","published_at":"2026-10-24T00:00:00","fetched_at":"2026-10-24T00:00:00Z"}
    assert client.post("/internal/v1/documents",headers=headers,json=body).status_code==422
    body["published_at"]="2026-10-24T00:00:00Z"; body["content"]="x"*2_000_001
    assert client.post("/internal/v1/documents",headers=headers,json=body).status_code==422


def test_openapi_exposes_closed_runtime_models(tmp_path):
    class Pipeline: pass
    app=create_app(store=Store(),pipeline=Pipeline(),artifact_store=ArtifactStore(tmp_path/"f.db"),service_token="secret",tenant_ids={"t"})
    spec=app.openapi()
    schemas=spec["components"]["schemas"]
    assert "DocumentInput" in schemas and schemas["DocumentInput"]["additionalProperties"] is False
    assert "SearchInput" in schemas and "AnalysisRequest" in schemas
