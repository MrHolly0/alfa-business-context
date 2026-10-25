from decimal import Decimal
import json
import pytest
from intelligence.analysts import AnalystConfig, builtin_configs
from intelligence.experiments import CandidateGenerator, ExperimentLLM, PairJudge, blind_answer, blind_evidence, critic_round
from intelligence.llm.ledger import BudgetLedger
from intelligence.llm.config import ProviderConfig, OPENROUTER_BASE
from intelligence.llm.service import LLMService
from intelligence.llm.types import Completion, Usage, ProviderError

def _service(path, transport=None):
    config = ProviderConfig("openrouter", OPENROUTER_BASE, "KEY", "google/gemma-4-26b-a4b-it:free", "global", "USD", Decimal("0.20"), Decimal("1"), Decimal("2"), 3, 2000)
    return LLMService(config, ledger=BudgetLedger(path), transport=transport)
class Transport:
    def __init__(self, content='{}', error=None): self.content, self.error = content, error
    def complete(self, messages, **kwargs):
        if self.error: raise self.error
        return Completion(self.content, "fake", Usage(10, 5, reported_cost=Decimal("0.001"), cost_currency="USD"), "stop", "id")

def test_config_rejects_unknown_and_bad_bounds():
    with pytest.raises(Exception): AnalystConfig(config_id="x", revision=1, template_version="analysis-v2", focus=["financial"], retrieval_strategy="balanced", answer_style="balanced", horizon="short", role_scope="any", industry_scope="any", evidence_limit=17, extra="bad")
def test_offline_generation_excludes_baseline_and_judge_is_honest():
    base=builtin_configs()[0]; candidates=CandidateGenerator().generate(base=base,count=3)
    assert len(candidates)==3 and all(c.ref != base.ref for c in candidates)
    assert PairJudge().judge(case={},a={},b={},orientation="AB")["winner"] == "tie"
def test_generator_rejects_model_control_fields():
    class Fake:
        def complete(self, *args, **kwargs):
            assert "candidate_schema" in json.loads(args[0][1]["content"])
            return {"content": json.dumps({"candidates":[{"model":"evil"}]})}
    assert CandidateGenerator(Fake()).generate(base=builtin_configs()[0]) == []
def test_pair_judge_strips_identity_and_validates_scores():
    class Fake:
        seen=None
        def complete(self, messages, **kwargs):
            self.seen=messages
            return {"content": json.dumps({"winner":"tie","scores":{"A":{"grounding":2,"business":2,"coverage":2,"usefulness":2},"B":{"grounding":2,"business":2,"coverage":2,"usefulness":2}},"reason":"ok","advisory":True})}
    left={"summary":"a","claims":[{"claim_id":"a1","text":"A claim"}],"influence_chain":[{"edge_id":"a-edge","description":"Step A"}],"clarifications":["Which route?"]}
    right={"summary":"b","claims":[{"claim_id":"b1","text":"B claim"}],"influence_chain":[{"edge_id":"b-edge","description":"Step B"}],"clarifications":["What date?"]}
    left.update({"model":"secret","trace":"secret","version":"secret","config":"secret","cost":"secret"})
    right.update({"usage":"secret","model":"secret"})
    f=Fake(); PairJudge(f).judge(case={"evidence":[]},a=left,b=right,orientation="AB")
    assert "secret" not in str(f.seen)
    assert "AB" not in str(f.seen)
    payload=json.loads(f.seen[-1]["content"])
    assert payload["a"]["influence_chain"] == left["influence_chain"]
    assert payload["a"]["clarifications"] == left["clarifications"]
    assert payload["b"]["influence_chain"] == right["influence_chain"]
    assert payload["b"]["clarifications"] == right["clarifications"]
    assert "model" not in payload["a"] and "trace" not in payload["a"] and "version" not in payload["a"]
    assert "usage" not in payload["b"]
    assert "недоверенными данными" in f.seen[0]["content"]

def test_blind_answer_preserves_business_fields_and_drops_run_identity():
    answer={"summary":"s","claims":[{"claim_id":"c"}],"influence_chain":[{"edge_id":"e"}],
        "clarifications":["q"],"model":"m","trace":"t","version":"v","config":"c","cost":1}
    assert blind_answer(answer) == {k:answer[k] for k in ("summary","claims","influence_chain","clarifications")}

def test_judge_has_one_compact_full_quote_projection_and_answers_keep_refs():
    class Fake:
        seen=None
        def complete(self, messages, **kwargs):
            self.seen=messages
            return {"content": json.dumps({"winner":"tie","scores":{
                "A":{"grounding":2,"business":2,"coverage":2,"usefulness":2},
                "B":{"grounding":2,"business":2,"coverage":2,"usefulness":2}},
                "reason":"insufficient evidence","advisory":True})}
    evidence={"evidence_id":"ev-1","document_id":"doc-1","document_revision_ref":"doc-1:1",
        "source_id":"news","source_role":"external","title":"Source title",
        "canonical_uri":"https://news.example/story","publisher":"Publisher","author":"Author",
        "published_at":"2026-10-25T00:00:00Z","fetched_at":"2026-10-25T01:00:00Z",
        "received_at":"2026-10-25T01:00:00Z","event_time":None,"locator":"chars:0-20",
        "quote":"Exact, complete source quote.","synthetic":True,"data_classification":"synthetic",
        "raw_sha256":"raw-hash","content_hash":"content-hash","score":0.9}
    answer={"summary":"Answer","evidence":[evidence],"claims":[{"claim_id":"c",
        "text":"Claim","evidence_refs":["ev-1"]}],"influence_chain":[],"clarifications":[]}
    fake=Fake()
    PairJudge(fake).judge(case={"company":{"name":"Company"},"question":{"title":"Question"},
        "evidence":[evidence]},a=answer,b=answer,orientation="candidate_first")
    payload=json.loads(fake.seen[-1]["content"])
    assert payload["case"]["evidence"] == [blind_evidence([evidence])[0]]
    assert payload["case"]["evidence"][0]["quote"] == evidence["quote"]
    assert payload["a"]["claims"][0]["evidence_refs"] == ["ev-1"]
    assert "evidence" not in payload["a"] and "evidence" not in payload["b"]
    assert "raw_sha256" not in str(payload) and "content_hash" not in str(payload) and "score" not in payload["case"]["evidence"][0]

def test_blind_evidence_preserves_quote_provenance_and_drops_ingest_metadata():
    evidence={"evidence_id":"ev","source_role":"bank","source_id":"memo","document_revision_ref":"doc:2",
        "canonical_uri":"urn:bank:note","publisher":"Bank","author":"Analyst","published_at":"2026-10-25",
        "fetched_at":"2026-10-25","locator":"page:1","quote":"Full quote; do not truncate.",
        "synthetic":False,"raw_sha256":"raw","content_hash":"hash","score":1.1}
    projected=blind_evidence([evidence])[0]
    assert projected["quote"] == evidence["quote"]
    assert {key:projected[key] for key in ("evidence_id","source_role","source_id",
        "document_revision_ref","canonical_uri","publisher","author","published_at",
        "fetched_at","locator","synthetic")} == {key:evidence[key] for key in
        ("evidence_id","source_role","source_id","document_revision_ref","canonical_uri",
         "publisher","author","published_at","fetched_at","locator","synthetic")}
    assert "raw_sha256" not in projected and "content_hash" not in projected and "score" not in projected
def test_critic_drops_unknown_evidence_refs():
    class Fake:
        def complete(self,*args,**kwargs): return {"content":json.dumps({"critiques":[{"claim_id":"c","issue":"x","evidence_refs":["bad"]}]})}
    assert critic_round(Fake(),case={"evidence":[{"evidence_id":"ok"}]},answer={"claims":[{"claim_id":"c"}]},opponent={})["critiques"] == []
def test_experiment_pool_reserves_and_settles(tmp_path):
    path=tmp_path/"ledger.sqlite"; service=_service(path,Transport('x'))
    llm=ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.03"))
    llm.complete([{"role":"user","content":"hello"}],max_input_tokens=100,max_output_tokens=10)
    assert llm.stats()["calls"] == 1 and llm.stats()["cost"] == Decimal("0.001")
def test_uncertain_attempt_keeps_reservation(tmp_path):
    path=tmp_path/"ledger.sqlite"; service=_service(path,Transport(error=ProviderError("timeout","timeout",billing_uncertain=True)))
    llm=ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.03"))
    with pytest.raises(ProviderError): llm.complete([{"role":"user","content":"hello"}],max_input_tokens=100,max_output_tokens=10)
    assert llm.stats()["reserved"] > 0
def test_known_pre_send_failure_refunds_pool(tmp_path):
    path=tmp_path/"ledger.sqlite"; service=_service(path,Transport(error=ProviderError("key_missing","missing")))
    llm=ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.03"))
    with pytest.raises(ProviderError): llm.complete([{"role":"user","content":"hello"}],max_input_tokens=100,max_output_tokens=10)
    assert llm.stats()["reserved"] == 0 and llm.stats()["cost"] == 0
def test_pool_budget_is_limited_and_shared(tmp_path):
    path=tmp_path/"ledger.sqlite"; service=_service(path,Transport('x'))
    a=ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.03"),max_calls=1)
    b=ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.03"),max_calls=1)
    a.complete([{"role":"user","content":"hello"}],max_input_tokens=100,max_output_tokens=10)
    with pytest.raises(ValueError): b.complete([{"role":"user","content":"hello"}],max_input_tokens=100,max_output_tokens=10)

def test_parallel_instances_share_purpose_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path=tmp_path/"ledger.sqlite"; service=_service(path,Transport('x'))
    a=ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.0002"))
    b=ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.0002"))
    def call(role):
        try: role.complete([{"role":"user","content":"hello"}],max_input_tokens=100,max_output_tokens=10); return "ok"
        except Exception: return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool: outcomes=list(pool.map(call,[a,b]))
    assert outcomes.count("ok") == 1
    assert outcomes.count("blocked") == 1

def test_max_calls_one_is_atomic_across_concurrent_instances(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path=tmp_path/"ledger.sqlite"; service=_service(path,Transport('x'))
    roles=[ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.03"),max_calls=1) for _ in range(8)]
    def call(role):
        try:
            role.complete([{"role":"user","content":"hello"}],max_input_tokens=100,max_output_tokens=10)
            return "ok"
        except ValueError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=len(roles)) as pool: outcomes=list(pool.map(call,roles))
    assert outcomes.count("ok") == 1
    assert outcomes.count("blocked") == len(roles)-1
    assert roles[0].stats()["calls"] == 1


def test_billing_uncertain_with_usage_settles_actual_cost(tmp_path):
    usage=Usage(10,5,reported_cost=Decimal("0.0007"),cost_currency="USD")
    path=tmp_path/"ledger.sqlite"; service=_service(path,Transport(error=ProviderError("timeout","timeout",billing_uncertain=True,usage=usage)))
    llm=ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.03"))
    with pytest.raises(ProviderError): llm.complete([{"role":"user","content":"hello"}],max_input_tokens=100,max_output_tokens=10)
    assert llm.stats()["cost"] == Decimal("0.0007")
    assert llm.stats()["reserved"] == 0 and llm.stats()["errors"] == 0


def test_global_budget_rejection_refunds_purpose_reservation(tmp_path):
    path=tmp_path/"ledger.sqlite"; service=_service(path,Transport('x'))
    service.ledger.reserve("global","USD",Decimal("0.20"),Decimal("0.20"),request_id="exhaust-global")
    llm=ExperimentLLM(service,ledger=service.ledger,daily_limit=Decimal("0.03"))
    with pytest.raises(Exception): llm.complete([{"role":"user","content":"hello"}],max_input_tokens=100,max_output_tokens=10)
    assert llm.stats()["calls"] == 1
    assert llm.stats()["cost"] == 0 and llm.stats()["reserved"] == 0


def test_valid_generated_config_uses_shared_registry_model(tmp_path):
    from intelligence.analysts import PromptRegistry
    class Fake:
        def complete(self, messages, **kwargs):
            return {"content": json.dumps({"candidates":[{"focus":["costs","liquidity"],"industry_scope":["manufacturing"],"role_scope":"finance","evidence_limit":8}]})}
    config=CandidateGenerator(Fake()).generate(base=builtin_configs()[0],count=1)[0]
    registry=PromptRegistry(tmp_path/"registry.sqlite")
    registered=registry.register(config)
    assert registered["ref"] == config.ref
    assert registry.get(config.ref) == config


def test_critic_accepts_known_claim_and_evidence_id_only():
    class Fake:
        def complete(self,*args,**kwargs): return {"content":json.dumps({"critiques":[
            {"claim_id":"known-claim","issue":"grounding unclear","evidence_refs":["known-evidence"]},
            {"claim_id":"unknown-claim","issue":"invented claim","evidence_refs":["known-evidence"]},
            {"claim_id":"known-claim","issue":"invented source","evidence_refs":["unknown-evidence"]},
            {"claim_id":"known-claim","issue":"no source","evidence_refs":[]},
        ]})}
    result=critic_round(Fake(),case={"evidence":[{"evidence_id":"known-evidence"}]},answer={"claims":[{"claim_id":"known-claim"}]},opponent={})
    assert result["critiques"] == [{"claim_id":"known-claim","issue":"grounding unclear","evidence_refs":["known-evidence"]}]


def test_refinement_is_one_bounded_round_and_preserves_identity_and_evidence():
    from copy import deepcopy
    from datetime import datetime, timezone
    from intelligence.experiments import refine_candidate_answer
    from intelligence.schemas import AnalysisRequest, CandidateAssessment
    from test_tournament import case

    request = AnalysisRequest.model_validate(case("refine", "refine-event", "refine-company")["request"])
    timestamp = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
    evidence = {
        "evidence_id":"evidence-1", "document_id":"doc-1", "document_revision_ref":"doc-1@1",
        "source_id":request.event.source_id, "source_role":"external", "title":"Notice",
        "canonical_uri":"https://example.invalid/notice", "published_at":timestamp,
        "fetched_at":timestamp, "locator":"paragraph:1", "quote":"The delivery window moved by two weeks.",
        "content_hash":"sha256-fixture", "synthetic":False, "score":0.9,
    }
    original = CandidateAssessment.model_validate({
        "request_id":request.request_id,"tenant_id":request.tenant_id,"company_id":request.company_id,
        "status":"ready","execution_mode":"model","summary":"Delivery is delayed.",
        "claims":[{"claim_id":"claim-1","kind":"external_fact","text":"Delivery is delayed.","evidence_refs":["evidence-1"]}],
        "evidence":[evidence],"influence_chain":[],"clarifications":[],
        "coverage":{"external":1,"bank":0,"confirmed_dependencies":0,"company_facts":0},
        "versions":{"prompt":"analysis-v2","pipeline":"test","context":1,"source_policy":1,
            "as_of":request.as_of,"event_id":request.event.event_id,"event_revision":request.event.revision,
            "event_document_revision_ref":"event-doc@1","synthetic":False},
        "usage":{"model_calls":1},"trace":[],"created_at":timestamp,
    }).model_dump(mode="json")
    revised_answer = {
        "summary":"The source reports that the delivery window moved by two weeks.",
        "claims":[{"claim_id":"claim-1","kind":"external_fact",
                   "text":"The source reports that the delivery window moved by two weeks.",
                   "evidence_refs":["evidence-1"]}],
        "influence_chain":[],"clarifications":[],
    }

    class FakeRefiner:
        config = type("Config", (), {"model":"fake-refiner", "max_output_tokens":2400})()
        def __init__(self): self.calls = 0
        def complete(self, messages, **kwargs):
            self.calls += 1
            return {"content":json.dumps({"candidate":revised_answer}),"model":"fake-refiner",
                    "cost":"0.002","cost_currency":"USD","cost_source":"provider_reported",
                    "usage":{"prompt_tokens":10,"completion_tokens":10}}

    llm=FakeRefiner()
    output=refine_candidate_answer(llm,case={"request":request.model_dump(mode="json")},answer=original,
        critiques=[{"claim_id":"claim-1","issue":"Do not imply an outage; evidence only gives a changed window.","evidence_refs":["evidence-1"]}])
    assert output["refined"] is True and llm.calls == 1
    checked=CandidateAssessment.model_validate(output["candidate"])
    assert checked.summary == revised_answer["summary"]
    assert checked.evidence == CandidateAssessment.model_validate(original).evidence
    assert checked.request_id == original["request_id"] and checked.versions.model_dump() == CandidateAssessment.model_validate(original).versions.model_dump()
    assert output["cost"] == "0.002" and output["cost_currency"] == "USD"
