from decimal import Decimal

import pytest

from intelligence.llm.config import OPENROUTER_BASE, ProviderConfig, _from_mapping
from intelligence.llm.routing import ModelRouter


def config(**changes):
    values={"profile":"openrouter","base_url":OPENROUTER_BASE,"api_key_env":"OPENROUTER_API_KEY",
        "model":"google/gemma-4-26b-a4b-it","budget_scope":"shared","currency":"USD",
        "daily_limit":Decimal("0.20"),"input_per_million":Decimal("0.08"),
        "output_per_million":Decimal("0.40"),"timeout_seconds":20,"max_output_tokens":4096,
        "allow_paid":True,"max_request_cost":Decimal("0.01")}
    values.update(changes)
    return ProviderConfig(**values)


def test_reasoning_effort_is_optional_and_positional_compatibility_is_preserved():
    original=ProviderConfig("openrouter",OPENROUTER_BASE,"OPENROUTER_API_KEY",
        "google/gemma-4-26b-a4b-it","shared","USD",Decimal("0.20"),Decimal("0.08"),
        Decimal("0.40"),20,4096,True,Decimal("0.01"),None)
    assert original.reasoning_effort is None
    assert config(reasoning_effort="low").reasoning_effort=="low"


@pytest.mark.parametrize("value",["minimal","xhigh","automatic","",False,1])
def test_reasoning_effort_rejects_unapproved_values(value):
    with pytest.raises(ValueError,match="reasoning_effort"):
        config(reasoning_effort=value)


def test_hackathon_profile_rejects_reasoning_parameter():
    with pytest.raises(ValueError,match="OpenRouter profile"):
        _from_mapping("hackathon",{"base_url":"https://changellenge.up.railway.app/v1",
            "api_key_env":"HACKATHON_KEY","model":"google/gemma-4-26b-a4b-it",
            "budget_scope":"shared","currency":"RUB","daily_limit":"500",
            "input_per_million":"0","output_per_million":"0","timeout_seconds":20,
            "max_output_tokens":4096,"reasoning_effort":"none"})


def test_route_reasoning_override_preserves_shared_budget_identity(tmp_path):
    router=ModelRouter(config(),tmp_path/"ledger.sqlite3",routes={
        "analyst":{"model":"qwen/qwen3.6-35b-a3b","input_per_million":"0.15",
            "output_per_million":"1.20","max_output_tokens":2048,"reasoning_effort":"none"},
        "critic":{"reasoning_effort":"low"}})
    services=router.services()
    analyst=services["analyst"].config
    critic=services["critic"].config
    assert analyst.reasoning_effort=="none"
    assert critic.reasoning_effort=="low"
    assert analyst.model=="qwen/qwen3.6-35b-a3b"
    assert analyst.daily_limit==Decimal("0.20")
    assert analyst.max_request_cost==Decimal("0.01")
    assert analyst.lifetime_limit is None
    assert all(service.ledger.path==str(tmp_path/"ledger.sqlite3") for service in services.values())


def test_router_rejects_unknown_effort_and_hackathon_route_effort(tmp_path):
    with pytest.raises(ValueError,match="reasoning_effort"):
        ModelRouter(config(),tmp_path/"ledger.sqlite3",routes={"analyst":{"reasoning_effort":"max"}})
    hackathon=ProviderConfig("hackathon","https://changellenge.up.railway.app/v1","HACKATHON_KEY",
        "google/gemma-4-26b-a4b-it","shared","RUB",Decimal("500"),Decimal("0"),Decimal("0"),20,4096)
    with pytest.raises(ValueError,match="OpenRouter profile"):
        ModelRouter(hackathon,tmp_path/"ledger.sqlite3",routes={"analyst":{"reasoning_effort":"low"}}).services()
