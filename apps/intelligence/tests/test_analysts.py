from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from intelligence.analysts import AnalystConfig, PromptRegistry
from intelligence.pipeline import AnalysisPipeline
from test_pipeline import FakeLLM, req, store


def eligible_report(registry,scope,ref,reviewer="alice"):
    baseline=registry.snapshot(scope)
    return {"schema_version":"tournament-report-v1","winner_ref":ref,"baseline_ref":baseline["ref"],
            "baseline_snapshot":baseline,"mode":"model","shadow":False,"promotion_eligible":True,
            "synthetic":False,"holdout":{"passed":True,"fresh_for_selection":True},
            "shadow_validation":{"validated":True,"passed":True,"fresh":True},
            "human_review":{"approved":True,"reviewer":reviewer}}


def test_config_is_closed_and_assignment_frozen():
    config = AnalystConfig(config_id="baseline")
    with pytest.raises(ValidationError): AnalystConfig(config_id="bad", system_prompt="do anything")
    with pytest.raises(ValidationError): config.answer_style = "custom"
    with pytest.raises(TypeError): config.focus.append("supply")
    assert config.ref == "baseline@1"


def test_registry_builtins_routing_and_immutable_revision(tmp_path):
    registry = PromptRegistry(tmp_path / "registry.sqlite")
    assert len(registry.list()) == 4
    selected = registry.select(role="Финансовый директор", industry="логистика")
    assert selected.ref == "baseline@1"  # built-ins are available but not promoted
    finance = AnalystConfig(config_id="finance-logistics", role_scope="finance", industry_scope=["логистика"])
    registry.register(finance)
    with pytest.raises(ValueError): registry.register(finance.model_copy(update={"answer_style": "concise"}))


def test_concurrent_register_same_ref_with_different_content_is_serialized(tmp_path):
    registry = PromptRegistry(tmp_path / "registry.sqlite")
    barrier = Barrier(2)
    configs = [AnalystConfig(config_id="race", answer_style="concise"),
               AnalystConfig(config_id="race", answer_style="detailed")]

    def register(config):
        barrier.wait()
        try: return registry.register(config)
        except ValueError: return "immutable-conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(register, configs))
    assert sum(result == "immutable-conflict" for result in results) == 1
    saved = registry.get("race@1")
    assert registry.register(saved)["content_hash"] == registry.register(saved)["content_hash"]


def test_promotion_gates_history_conflict_and_rollback(tmp_path):
    registry = PromptRegistry(tmp_path / "registry.sqlite")
    config = AnalystConfig(config_id="finance", revision=2, role_scope="finance")
    registry.register(config)
    scope = "impact:finance:*"
    report = {"schema_version":"tournament-report-v1", "winner_ref":config.ref, "mode":"model", "shadow":False, "promotion_eligible":True,
              "synthetic":False, "holdout":{"passed":True}, "shadow_validation":{"validated":True,"passed":True,"fresh":True}, "human_review":{"approved":True,"reviewer":"alice"}}
    report.update(baseline_ref="baseline@1",baseline_snapshot=registry.snapshot(scope))
    report["holdout"]["fresh_for_selection"]=True
    assert registry.promote(scope=scope, ref=config.ref, report=report, reviewer="alice")["revision"] == 1
    with pytest.raises(ValueError): registry.promote(scope=scope, ref=config.ref, report=report, reviewer="alice")
    assert registry.select(role="financial controller", industry="transport").ref == config.ref
    result = registry.rollback(scope=scope, reviewer="bob", expected_revision=1)
    assert result["revision"] == 2
    assert registry.select(role="financial controller", industry="transport").ref == "baseline@1"
    with pytest.raises(ValueError): registry.rollback(scope=scope, reviewer="bob", expected_revision=1)


def test_rejects_ineligible_reports_and_scope_mismatch(tmp_path):
    registry = PromptRegistry(tmp_path / "registry.sqlite")
    config = AnalystConfig(config_id="procure", role_scope="procurement")
    registry.register(config)
    report = {"schema_version":"tournament-report-v1", "winner_ref":config.ref, "promotion_eligible":True,
              "mode":"model", "shadow":False, "synthetic":True, "holdout":{"passed":True}, "human_review":{"approved":True,"reviewer":"alice"}}
    with pytest.raises(ValueError): registry.promote(scope="impact:procurement:*", ref=config.ref, report=report, reviewer="alice")
    report["synthetic"] = False
    report["mode"] = "baseline"
    with pytest.raises(ValueError): registry.promote(scope="impact:procurement:*", ref=config.ref, report=report, reviewer="alice")
    report["mode"] = "model"
    report.pop("shadow")
    with pytest.raises(ValueError): registry.promote(scope="impact:procurement:*", ref=config.ref, report=report, reviewer="alice")
    report["shadow"] = True
    with pytest.raises(ValueError): registry.promote(scope="impact:procurement:*", ref=config.ref, report=report, reviewer="alice")
    report["shadow"] = False
    with pytest.raises(ValueError): registry.promote(scope="impact:finance:*", ref=config.ref, report=report, reviewer="alice")


def test_any_scope_exact_and_wildcard_fallback_and_case_normalization(tmp_path):
    registry = PromptRegistry(tmp_path / "registry.sqlite")
    any_logistics = AnalystConfig(config_id="any-logistics", industry_scope=["Logistics"])
    any_general = AnalystConfig(config_id="any-general", revision=2)
    registry.register(any_logistics)
    registry.register(any_general)
    report = {"schema_version":"tournament-report-v1", "winner_ref":any_logistics.ref, "mode":"model", "shadow":False, "promotion_eligible":True,
              "synthetic":False, "holdout":{"passed":True}, "shadow_validation":{"validated":True,"passed":True,"fresh":True}, "human_review":{"approved":True,"reviewer":"alice"}}
    with pytest.raises(ValueError): registry.promote(scope="impact:any:*", ref=any_logistics.ref, report=report, reviewer="alice")
    report.update(baseline_ref="baseline@1",baseline_snapshot=registry.snapshot("impact:any:logistics"))
    report["holdout"]["fresh_for_selection"]=True
    registry.promote(scope="impact:ANY:LOGISTICS", ref=any_logistics.ref, report=report, reviewer="alice")
    general_report = eligible_report(registry,"impact:any:*",any_general.ref)
    registry.promote(scope="impact:any:*", ref=any_general.ref, report=general_report, reviewer="alice")
    assert registry.select(role="unknown role", industry="logistics").ref == any_logistics.ref
    assert registry.select(role="unknown role", industry="transport").ref == any_general.ref
    with pytest.raises(ValueError): registry.promote(scope="impact:any:logistics", ref=any_logistics.ref,
        report=report, reviewer=" ", expected_revision=1)
    with pytest.raises(ValueError): registry.promote(scope="impact:any:logistics", ref=any_logistics.ref,
        report=report, reviewer="alice", expected_revision=True)


def test_pipeline_explicit_config_changes_retrieval_and_records_version(tmp_path):
    corpus = store(tmp_path)
    llm = FakeLLM(['{"summary":"Недостаточно данных","claims":[],"influence_chain":[],"clarifications":[]}'])
    config = AnalystConfig(config_id="custom", retrieval_strategy="dependencies_first", focus=["supply"], answer_style="concise", horizon="short", evidence_limit=4)
    result = AnalysisPipeline(corpus, llm, analyst_config=config).run(req())
    assert result.versions.analyst_config == "custom@1"
    assert llm.calls and "ANALYST_PREFERENCES" in llm.calls[0][0][1]["content"]
    assert "dependencies_first" in llm.calls[0][0][1]["content"]
    assert "short means immediate to 30 days" in llm.calls[0][0][1]["content"]


def test_dependencies_first_evidence_limit_keeps_pinned_event(tmp_path):
    corpus = store(tmp_path)
    for i in range(12):
        corpus.ingest({"tenant_id":"tenant-a", "source_id":"bank", "source_role":"bank",
            "canonical_uri":f"https://example.invalid/research/{i}", "title":"Логистика порт закрыт",
            "content":"Перевозки через порт задерживаются; сроки доставки выросли.",
            "content_type":"text/plain", "published_at":"2026-10-24T09:00:00Z",
            "fetched_at":"2026-10-24T09:00:00Z", "synthetic":True})
    config = AnalystConfig(config_id="tight", retrieval_strategy="dependencies_first", evidence_limit=4)
    result = AnalysisPipeline(corpus, analyst_config=config).run(req())
    assert len(result.evidence) <= 4
    assert any(e.source_role == "external" and e.quote in req().event.text for e in result.evidence)


def test_pipeline_default_is_safe_analysis_v2_baseline(tmp_path):
    result = AnalysisPipeline(store(tmp_path)).run(req())
    assert result.versions.analyst_config == "baseline@1"
    assert result.versions.prompt == "analysis-v2"


def test_register_legacy_default_fields_preserves_original_revision_bytes(tmp_path):
    import hashlib
    import json
    registry=PromptRegistry(tmp_path/'legacy.sqlite3')
    config=AnalystConfig(config_id='baseline')
    legacy=config.model_dump(mode='json')
    for key in ['max_depth','retrieval_expansions','examples']:legacy.pop(key)
    raw=json.dumps(legacy,sort_keys=True,separators=(',',':'),ensure_ascii=False)
    digest=hashlib.sha256(raw.encode()).hexdigest()
    with registry._db() as db:
        db.execute('UPDATE configs SET config_json=?,content_hash=? WHERE ref=?',(raw,digest,config.ref))
    registered=registry.register(config)
    assert registered['content_hash']==digest
    with registry._db() as db:
        assert db.execute('SELECT config_json FROM configs WHERE ref=?',(config.ref,)).fetchone()[0]==raw
    with pytest.raises(ValueError,match='immutable'):
        registry.register(AnalystConfig(config_id='baseline',focus=['costs']))
