from decimal import Decimal
import json
import pytest
from intelligence.analysts import AnalystConfig, builtin_configs
from intelligence.experiments import CandidateGenerator, ExperimentLLM, PairJudge, critic_round
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
    f=Fake(); PairJudge(f).judge(case={"evidence":[]},a={"summary":"a","model":"secret","trace":"secret"},b={"summary":"b","usage":"secret"},orientation="AB")
    assert "secret" not in str(f.seen)
    assert "AB" not in str(f.seen)
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
