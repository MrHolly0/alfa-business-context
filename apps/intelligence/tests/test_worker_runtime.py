import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from intelligence.artifacts import ArtifactStore
from intelligence.worker import Worker
from intelligence.core_client import CoreClient


def job(**overrides):
    now=datetime.now(timezone.utc)
    req = {"schema_version":"analysis-request-v1", "tenant_id": "tenant-a", "company_id": "co", "context_version": 3,
           "source_policy_version": 4, "request_id": "r", "as_of":now.isoformat(), "source_ids":["s"],
           "company":{"name":"Example","industry":"","description":"","role":"","dependencies":[],"facts":[]},
           "event":{"event_id":"e","title":"Title","text":"Text","source_id":"s","published_at":now.isoformat()},
           "budget":{"max_model_calls":2,"max_input_tokens":24000,"max_output_tokens":2048,"max_total_tokens":60000,"max_steps":12}}
    value = {"schema_version": "analysis-job-v1", "message_id":"claim-message-1",
             "scope":{"source_ids":["s"],"document_revision_refs":[]}, "job_id": "j", "tenant_scope": "tenant-a",
             "company_id": "co", "trace_id": "trace", "input_versions": {"context_version": 3, "source_policy_version": 4, "event_versions":[{"event_id":"e","revision":1}]},
             "lease_generation": 2, "deadline": (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat(),
             "lease_until": (datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat(), "analysis": req,
             "budget":{"max_model_calls":2,"max_tokens":60000,"cost_reservation_rub":1.0}}
    value.update(overrides)
    return value


class Core:
    def __init__(self, value): self.value=value; self.beats=0; self.results=[]; self.failures=[]
    def claim(self): return {"job": self.value}
    def heartbeat(self, jid, gen): self.beats+=1; return {"accepted": True, "lease_generation": gen, "lease_until":(datetime.now(timezone.utc)+timedelta(seconds=15)).isoformat()}
    def submit_result(self, payload): self.results.append(payload); return {"accepted": True, "message_id": payload["message_id"]}
    def submit_failure(self, *args): self.failures.append(args)


def test_heartbeat_runs_during_model_call(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Pipeline:
        def run(self, request, checkpoint=None): entered.set(); release.wait(2); return {"status":"ready", "tenant_id":request.tenant_id, "company_id":request.company_id, "request_id":request.request_id}
    core=Core(job()); worker=Worker(core, Pipeline(), ArtifactStore(tmp_path/"a.db"), heartbeat_interval=.05, tenant_ids={"tenant-a"})
    thread=threading.Thread(target=worker.run_once); thread.start(); assert entered.wait(1); time.sleep(.12); release.set(); thread.join(2)
    assert core.beats >= 1 and len(core.results)==1


def test_rejects_expired_scope_and_version_mismatch(tmp_path):
    class Pipeline:
        def run(self, request, checkpoint=None): raise AssertionError("must not run")
    base=job(); base["tenant_scope"]="other"
    core=Core(base); result=Worker(core,Pipeline(),ArtifactStore(tmp_path/"a.db"),tenant_ids={"tenant-a"}).run_once()
    assert result["status"]=="rejected" and core.failures
    bad=job(); bad["input_versions"]={"context_version": 99,"source_policy_version":4}
    assert Worker(Core(bad),Pipeline(),ArtifactStore(tmp_path/"b.db"),tenant_ids={"tenant-a"}).run_once()["status"]=="rejected"
    naive=job(deadline="2026-10-24T00:00:00")
    assert Worker(Core(naive),Pipeline(),ArtifactStore(tmp_path/"c.db"),tenant_ids={"tenant-a"}).run_once()["status"]=="rejected"
    stale_event=job(); stale_event["input_versions"]["event_versions"][0]["revision"]=2
    assert Worker(Core(stale_event),Pipeline(),ArtifactStore(tmp_path/"d.db"),tenant_ids={"tenant-a"}).run_once()["status"]=="rejected"


def test_uncertain_ack_retries_same_durable_payload_after_restart(tmp_path):
    store=ArtifactStore(tmp_path/"a.db")
    class Pipeline:
        def __init__(self): self.calls=0
        def run(self, request, checkpoint=None): self.calls+=1; return {"status":"ready", "tenant_id":request.tenant_id, "company_id":request.company_id, "request_id":request.request_id}
    class Uncertain(Core):
        def submit_result(self, payload): self.results.append(payload); raise TimeoutError()
    p=Pipeline(); c=Uncertain(job())
    assert Worker(c,p,store,tenant_ids={"tenant-a"}).run_once()["status"]=="pending"
    original=c.results[0]
    c2=Core(None)
    assert Worker(c2,p,ArtifactStore(tmp_path/"a.db"),tenant_ids={"tenant-a"}).run_once()["status"]=="retransmitted"
    assert c2.results[0]==original and p.calls==1


def test_stale_heartbeat_stops_result_publication(tmp_path):
    entered, release=threading.Event(),threading.Event()
    class Pipeline:
        def run(self, request, checkpoint=None): entered.set(); release.wait(2); return {"status":"ready", "tenant_id":request.tenant_id, "company_id":request.company_id, "request_id":request.request_id}
    class Stale(Core):
        def heartbeat(self, jid, gen): return {"accepted":False,"lease_generation":gen,"lease_until":"2026-10-24T00:00:00Z"}
    c=Stale(job()); w=Worker(c,Pipeline(),ArtifactStore(tmp_path/"a.db"),heartbeat_interval=.05,tenant_ids={"tenant-a"})
    th=threading.Thread(target=w.run_once); th.start(); assert entered.wait(1); time.sleep(.1); release.set(); th.join(2)
    assert not c.results


def test_pipeline_checkpoint_is_live_guard_and_expired_lease_blocks_result(tmp_path):
    value=job(lease_until=(datetime.now(timezone.utc)+timedelta(seconds=.12)).isoformat())
    class Slow:
        def run(self, request, checkpoint=None):
            checkpoint("before_model")
            time.sleep(.15)
            return {"status":"ready","tenant_id":request.tenant_id,"company_id":request.company_id,"request_id":request.request_id}
    core=Core(value)
    result=Worker(core,Slow(),ArtifactStore(tmp_path/"a.db"),tenant_ids={"tenant-a"},heartbeat_interval=10).run_once()
    assert result["status"]=="lease_lost" and not core.results


def test_job_generation_reservation_prevents_double_model_call(tmp_path):
    barrier=threading.Barrier(2); started=threading.Event(); release=threading.Event()
    class CoreRace(Core):
        def claim(self): barrier.wait(timeout=1); return {"job":self.value}
    class PaidPipeline:
        def __init__(self): self.calls=0; self.lock=threading.Lock()
        def run(self, request, checkpoint=None):
            with self.lock: self.calls+=1
            started.set(); release.wait(2)
            return {"status":"ready","tenant_id":request.tenant_id,"company_id":request.company_id,"request_id":request.request_id}
    store=ArtifactStore(tmp_path/"a.db"); p=PaidPipeline(); results=[]; shared_job=job(); renewed=deepcopy(shared_job)
    renewed["lease_until"]=(datetime.now(timezone.utc)+timedelta(seconds=25)).isoformat()
    def run(value): results.append(Worker(CoreRace(value),p,store,tenant_ids={"tenant-a"},heartbeat_interval=10).run_once())
    a=threading.Thread(target=run,args=(shared_job,)); b=threading.Thread(target=run,args=(renewed,)); a.start(); b.start()
    assert started.wait(1); time.sleep(.05); release.set(); a.join(2); b.join(2)
    assert p.calls==1 and any(result["status"] in ("in_progress","already_acknowledged","retransmitted") for result in results), results


def test_budget_reservation_uses_rub_rates_and_rejects_paid_openrouter(tmp_path):
    class Pipeline:
        llm=SimpleNamespace(config=SimpleNamespace(profile="hackathon",model="google/gemma-4-26b-a4b-it",currency="RUB",input_per_million=100,output_per_million=100,max_output_tokens=2048))
        def run(self, request, checkpoint=None): raise AssertionError("must not run with insufficient reservation")
    low=job(budget={"max_model_calls":2,"max_tokens":60000,"cost_reservation_rub":.001})
    result=Worker(Core(low),Pipeline(),ArtifactStore(tmp_path/"a.db"),tenant_ids={"tenant-a"}).run_once()
    assert result["status"]=="rejected" and "reservation" in result["reason"]
    Pipeline.llm.config=SimpleNamespace(profile="openrouter",model="google/gemma-4-26b-a4b-it",currency="USD",input_per_million=0,output_per_million=0,max_output_tokens=2048)
    free=job(budget={"max_model_calls":2,"max_tokens":60000,"cost_reservation_rub":0})
    assert Worker(Core(free),Pipeline(),ArtifactStore(tmp_path/"b.db"),tenant_ids={"tenant-a"}).run_once()["status"]=="rejected"


def test_core_client_is_origin_only_no_redirects_and_bounded(tmp_path):
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json={"job":None})
    raw=httpx.Client(transport=httpx.MockTransport(handler))
    client=CoreClient("http://core:8080", "token", client=raw)
    assert client.claim()=={"job":None}
    assert str(seen[0].url)=="http://core:8080/internal/v1/analysis-jobs/claim"
    assert seen[0].headers["authorization"]=="Bearer token"
    client.close()
    for url in ("http://user:pass@core:8080", "http://core:8080/evil", "http://core:8080?redirect=x", "http://core:8080/#frag"):
        try: CoreClient(url,"token")
        except ValueError: pass
        else: raise AssertionError("unsafe Core origin accepted")
    with pytest.raises(ValueError): CoreClient("http://core:8080","token",timeout=float("inf"))

    redirecting=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(302,headers={"Location":"http://evil.invalid"})))
    with pytest.raises(RuntimeError,match="redirects"): CoreClient("http://core:8080","token",client=redirecting).claim()
    redirecting.close()
    oversized=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b"x"*4_000_001)))
    with pytest.raises(RuntimeError,match="exceeds limit"): CoreClient("http://core:8080","token",client=oversized).claim()
    oversized.close()
