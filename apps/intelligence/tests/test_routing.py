from decimal import Decimal

import pytest

from intelligence.llm.config import OPENROUTER_BASE, ProviderConfig
from intelligence.llm.routing import ModelRouter, ROLES


def base_config():
    return ProviderConfig(
        profile="openrouter", base_url=OPENROUTER_BASE, api_key_env="OPENROUTER_API_KEY",
        model="google/gemma-4-26b-a4b-it", budget_scope="alfa-openrouter-paid-tests",
        currency="USD", daily_limit=Decimal("0.20"), input_per_million=Decimal("0.08"),
        output_per_million=Decimal("0.40"), timeout_seconds=20, max_output_tokens=4096,
        allow_paid=True, max_request_cost=Decimal("0.01"), lifetime_limit=None)


def test_role_services_share_immutable_account_and_ledger(tmp_path):
    router = ModelRouter(base_config(), tmp_path / "shared-ledger.sqlite", routes={
        "analyst": {"model":"qwen/qwen3.6-35b-a3b", "input_per_million":"0.15",
                    "output_per_million":"1.20", "max_output_tokens":2048},
        "critic": {"model":"z-ai/glm-5.3-flash", "input_per_million":"0.20",
                   "output_per_million":"0.60"},
        "analyst:complex": {"model":"qwen/qwen3.6-35b-a3b", "max_output_tokens":4096},
    })
    services = router.services()
    assert set(services) == ROLES
    assert services["analyst"].config.model == "qwen/qwen3.6-35b-a3b"
    assert services["critic"].config.model == "z-ai/glm-5.3-flash"
    assert services["researcher"].config.model == "google/gemma-4-26b-a4b-it"
    for service in services.values():
        assert service.ledger.path == str(tmp_path / "shared-ledger.sqlite")
        assert service.config.budget_scope == "alfa-openrouter-paid-tests"
        assert service.config.currency == "USD"
        assert service.config.daily_limit == Decimal("0.20")
        assert service.config.lifetime_limit is None
        assert service.config.max_request_cost == Decimal("0.01")
        assert service.config.api_key_env == "OPENROUTER_API_KEY"
        assert service.config.base_url == OPENROUTER_BASE
    assert router.route_for("analyst", complexity="complex").config.max_output_tokens == 4096
    assert base_config().model == "google/gemma-4-26b-a4b-it"
    assert router.route_for("analyst") is services["analyst"]


def test_complex_route_is_explicit_and_no_implicit_model_fallback(tmp_path):
    router = ModelRouter(base_config(), tmp_path / "ledger.sqlite", routes={
        "researcher": {"model":"qwen/qwen3.6-35b-a3b"}
    })
    assert router.route_for("researcher").config.model == "qwen/qwen3.6-35b-a3b"
    with pytest.raises(ValueError, match="no explicit complex route"):
        router.route_for("researcher", complexity="complex")
    with pytest.raises(ValueError, match="unknown model route role"):
        router.route_for("unknown")
    with pytest.raises(ValueError, match="complexity"):
        router.route_for("researcher", complexity="automatic")


def test_route_overrides_are_closed_and_models_remain_allowlisted(tmp_path):
    with pytest.raises(ValueError, match="only model"):
        ModelRouter(base_config(), tmp_path / "ledger.sqlite", routes={"analyst":{"base_url":"https://other"}})
    with pytest.raises(ValueError):
        ModelRouter(base_config(), tmp_path / "ledger.sqlite", routes={"analyst":{"model":"unknown/provider-model"}})
    with pytest.raises(ValueError):
        ModelRouter(base_config(), tmp_path / "ledger.sqlite", routes={"analyst":{"model":"qwen/qwen3.6-35b-a3b:free"}})
    with pytest.raises(ValueError, match="route key"):
        ModelRouter(base_config(), tmp_path / "ledger.sqlite", routes={"analyst:automatic":{"model":"google/gemma-4-26b-a4b-it"}})
