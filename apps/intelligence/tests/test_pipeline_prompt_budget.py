"""The model prompt keeps full company facts while bounding evidence excerpts."""
import json

from intelligence.analysts import AnalystConfig
from intelligence.client_data import PRODUCT_MARKER_FACT_CAVEAT, PRODUCT_MARKER_LABELS, prepare_company_contexts
from intelligence.company_context import project_company_context
from intelligence.llm.service import estimate_text_tokens
from intelligence.pipeline import AnalysisPipeline, MAX_PROMPT_EVIDENCE, MAX_PROMPT_EVIDENCE_QUOTE_CHARS
from intelligence.schemas import ModelAnswer
from test_pipeline import req, store


def test_model_prompt_preserves_all_context_and_primary_bank_within_input_budget():
    facts = [{"fact_id":f"fact-{index}", "text":f"Approved profile fact {index}: " + ("profile detail " * 18),
        "provenance":f"df_Uid_data_hack.csv#row={index + 100},column=PROFILE",
        "status":"confirmed"} for index in range(21)]
    request = req(synthetic=False, company={"name":"Case company", "industry":"logistics",
        "description":"Case profile description. " + ("company context detail " * 35),
        "role":"finance lead", "facts":facts},
        event={"event_id":"event-1", "title":"Port status", "text":"Port closed after severe storm. " + ("Operational disruption detail " * 100),
            "source_id":"news", "published_at":"2026-10-24T10:00:00Z"},
        document_revision_refs=["news-revision", "bank-revision"], budget={"max_input_tokens":24_000})
    evidence = []
    for index in range(16):
        is_bank = index == 7
        quote = (f"Source excerpt {index}. " + ("supporting detail " * 90))[:1000]
        evidence.append({"evidence_id":f"ev-{index}", "document_revision_ref":
            "bank-revision" if is_bank else f"news-revision-{index}",
            "source_id":"bank" if is_bank else "news", "source_role":"bank" if is_bank else "external",
            "title":f"Source title {index}", "publisher":"Publisher", "author":"Editor",
            "canonical_uri":f"https://source.example/{index}", "published_at":"2026-10-24T00:00:00Z",
            "locator":"page:1", "data_classification":"confidential" if is_bank else "public",
            "quote":quote, "evidence_id":f"ev-{index}"})
    primary = {"ev-0"}
    projection = project_company_context(request.company, as_of=request.as_of)

    messages, coverage = AnalysisPipeline._build_model_messages(request, evidence,
        AnalystConfig(config_id="bounded-prompt-test"), projection, package=None, include_package=False,
        primary_ids=primary, system="System policy. Keep sources grounded.", user_base="Analyze the evidence.")

    payload = json.loads(messages[-1]["content"].rsplit("\n", 1)[-1])
    prompt_evidence = payload["RETRIEVED_EVIDENCE"]
    sent_ids = {item["evidence_id"] for item in prompt_evidence}
    prompt_facts = payload["REQUEST_CONTEXT"]["company"]["facts"]
    assert len(prompt_facts) == len(facts)
    assert {item["fact_id"] for item in prompt_facts} == {item["fact_id"] for item in facts}
    assert {item["provenance"] for item in prompt_facts} == {item["provenance"] for item in facts}
    assert len(prompt_evidence) == MAX_PROMPT_EVIDENCE
    assert "ev-0" in sent_ids
    assert any(item["source_role"] == "bank" for item in prompt_evidence)
    assert all(len(item["quote"]) <= MAX_PROMPT_EVIDENCE_QUOTE_CHARS for item in prompt_evidence)
    assert coverage["evidence_available"] == len(evidence)
    assert coverage["evidence_omitted"] == len(evidence) - MAX_PROMPT_EVIDENCE
    assert coverage["evidence_quote_excerpts_truncated"] > 0
    assert coverage["company_facts_preserved"] == len(facts)
    assert estimate_text_tokens(messages) == coverage["estimated_input_tokens"]
    assert estimate_text_tokens(messages) <= request.budget.max_input_tokens
    assert len(evidence) == 16  # Local validation retains the complete corpus evidence.


def test_adaptive_fit_keeps_all_facts_and_primary_bank_evidence():
    facts = [{"fact_id":f"fact-{index}", "text":f"Case profile fact {index}: " + ("operational background " * 10),
        "provenance":f"case.csv#row={index + 1}", "status":"confirmed"} for index in range(22)]
    request = req(synthetic=False, company={"name":"Case company", "industry":"logistics",
        "description":"Operating profile", "role":"finance lead", "facts":facts},
        event={"event_id":"event-fit", "title":"Port status",
            "text":"Primary event text. " + ("Context excerpt. " * 500),
            "source_id":"news", "published_at":"2026-10-24T10:00:00Z"},
        budget={"max_input_tokens":24_000})
    evidence = []
    for index in range(16):
        bank = index == 7
        evidence.append({"evidence_id":f"ev-{index}", "document_revision_ref":f"revision-{index}",
            "source_id":"bank" if bank else "news", "source_role":"bank" if bank else "external",
            "title":f"Evidence {index}", "publisher":"Publisher", "published_at":"2026-10-24T00:00:00Z",
            "data_classification":"confidential" if bank else "public",
            "quote":f"Evidence quote {index}. " + ("supporting detail " * 60)})
    package = {"package_id":"package-1", "data_classification":"public",
        "claims":[{"claim_id":f"claim-{i}", "kind":"reported_fact", "statement":"Shared summary " * 20,
                   "quote":"Source quote " * 20, "source_id":"news"} for i in range(8)],
        "sources":[], "mechanisms":[], "resources":[], "unknowns":[]}
    projection = project_company_context(request.company, as_of=request.as_of)
    from importlib.resources import files
    system = files("intelligence.prompts").joinpath("analysis-v2.system.ru.txt").read_text("utf-8")
    user_base = files("intelligence.prompts").joinpath("analysis-v2.user.ru.txt").read_text("utf-8")
    messages, metrics = AnalysisPipeline._fit_model_prompt(request, evidence,
        AnalystConfig(config_id="adaptive-fit-test"), projection, package=package,
        package_allowed=True, primary_ids={"ev-0"}, system=system, user_base=user_base,
        input_budget=24_000)
    payload = json.loads(messages[-1]["content"].rsplit("\n", 1)[-1])
    assert metrics["budget_fit"] is True
    assert metrics["estimated_input_tokens"] <= 24_000
    assert metrics["company_facts_preserved"] == len(facts)
    assert metrics["all_company_facts_retained"] is True
    assert payload["REQUEST_CONTEXT"]["company"]["facts"]
    assert metrics["primary_event_evidence_sent"] >= 1
    assert metrics["bank_evidence_sent"] >= 1
    assert metrics["fit_stage"] != "full"


def test_four_active_product_markers_and_22_facts_fit_with_prompt_only_compaction(tmp_path):
    row = {"SYNTHETIC_U":"U-TEST", "ORG_TYPE":"ООО", "OKFS_NAME":"Частная",
        "COMPANY_AGE_YEARS_GRADATION":"10-15 лет", "ECO_MARK_LABEL":"Эко-маркер",
        "MAINOKVED_CODE":"62.01", "MAINOKVED_NAME":"Разработка программного обеспечения",
        "NEGATIVEMARK":"unknown-marker", "AMOUNT_WORKERS_FNS_GRADATION":"100-500",
        "AMOUNT_WORKERS_SPARK_GRADATION":"100-500", "CL_TYPE":"Тип A", "FTS_FLAG":"flag-x",
        "FTS_COUNTRY_NAME":"Россия", "GROUP_ID":"GROUP-X", "CITY":"Москва", "REGION":"Москва",
        "CL_PRODUCT_RKO_MARK":"1.0", "CL_PRODUCT_GAR_MARK":"0.0", "CL_PRODUCT_LOA_MARK":"1",
        "CL_PRODUCT_LEA_MARK":"1.0", "CL_PRODUCT_DEP_MARK":"0", "CL_PRODUCT_ZPP_MARK":"1",
        "FACTORING_MARK":"unrecognized"}
    proposal = prepare_company_contexts([row], tenant_id="core-local", filename="df_Uid_data_hack.csv",
        origin="synthetic-test").proposals[0]
    assert len(proposal.context.facts) == 22
    assert len(proposal.context.assertions) == 7
    assert sum(item.status == "confirmed" for item in proposal.context.assertions) == 4
    request = req(synthetic=False, company=proposal.context.model_dump(mode="json"),
        event={"event_id":"event-fit", "title":"Port update",
            "text":"Primary event. " + ("Detailed source context. " * 200),
            "source_id":"news", "published_at":"2026-10-24T10:00:00Z"},
        document_revision_refs=["news-revision-0", "bank-revision"]
    )
    evidence = []
    for index in range(16):
        is_bank = index == 7
        evidence.append({"evidence_id":f"ev-{index}", "document_revision_ref":
            "bank-revision" if is_bank else f"news-revision-{index}",
            "source_id":"bank" if is_bank else "news", "source_role":"bank" if is_bank else "external",
            "title":f"Evidence {index}", "publisher":"Publisher", "published_at":"2026-10-24T00:00:00Z",
            "data_classification":"confidential" if is_bank else "public",
            "quote":f"Source quote {index}. " + ("supporting detail " * 50)})
    package = {"package_id":"package-fit", "data_classification":"public",
        "claims":[{"claim_id":f"shared-{index}", "kind":"reported_fact", "statement":"Shared claim " * 12,
                   "quote":"Quoted source " * 12, "source_id":"news"} for index in range(8)],
        "sources":[], "mechanisms":[], "resources":[], "unknowns":[]}
    company_projection = project_company_context(request.company, as_of=request.as_of)
    from importlib.resources import files
    system = files("intelligence.prompts").joinpath("analysis-v2.system.ru.txt").read_text("utf-8")
    user_base = files("intelligence.prompts").joinpath("analysis-v2.user.ru.txt").read_text("utf-8")
    messages, metrics = AnalysisPipeline._fit_model_prompt(request, evidence,
        AnalystConfig(config_id="product-marker-fit"), company_projection, package=package,
        package_allowed=True, primary_ids={"ev-0"}, system=system, user_base=user_base,
        input_budget=24_000)
    payload = json.loads(messages[-1]["content"].rsplit("\n", 1)[-1])
    model_company = payload["REQUEST_CONTEXT"]["company"]
    assert metrics["budget_fit"] is True
    assert metrics["estimated_input_tokens"] <= 24_000
    assert metrics["company_facts_preserved"] == 22
    assert metrics["company_assertions_preserved"] == 4  # Four confirmed active marks; 0/unknown stay out of confirmed projection.
    assert len(model_company["facts"]) == 22
    assert len(model_company["assertions"]) == 4
    assert set(item["fact_id"] for item in model_company["facts"]) == {item.fact_id for item in proposal.context.facts}
    assert {item["fact_id"]:item["provenance"] for item in model_company["facts"]} == {
        item.fact_id:item.provenance for item in proposal.context.facts}
    for column in PRODUCT_MARKER_LABELS:
        fact = next(item for item in model_company["facts"] if item["fact_id"].endswith(column.lower()))
        assert fact["text"].endswith(row[column] + ".")
        assert PRODUCT_MARKER_FACT_CAVEAT not in fact["text"]  # Shared once in the controlled policy.
    policy = model_company["PRODUCT_MARKER_POLICY"]
    assert policy["source_mark_1"] and policy["source_mark_0"] and policy["unknown_or_missing"]
    assert "confirmed only here" in model_company["context_projection_policy"]
    assert metrics["profile_prompt_compaction"]["product_marker_policy_included"] is True
    assert metrics["profile_prompt_compaction"]["product_fact_caveats_compacted"] == 7
    assert metrics["profile_prompt_compaction"]["product_assertion_labels_compacted"] == 4
    assert payload["OUTPUT_JSON_SCHEMA"] == ModelAnswer.model_json_schema()
    # The proposal itself keeps its full local labels and repeated caveats.
    assert all(PRODUCT_MARKER_FACT_CAVEAT in item.text for item in proposal.context.facts
               if any(item.fact_id.endswith(column.lower()) for column in PRODUCT_MARKER_LABELS))

    # Full local constraints remain available even though Coverage presents a
    # grouped, bounded summary. Exercise both baseline and model-error fallback.
    full_request = request.model_copy(update={"document_revision_refs":[]})
    baseline = AnalysisPipeline(store(tmp_path), llm=None).run(full_request)
    assert baseline.status == "partial"
    assert len(baseline.usage["context_limitations"]) == 32
    assert baseline.usage["context_limitation_counts"]["freshness_unknown:facts"] == 22
    assert len(baseline.coverage.limitations) <= 30
    assert len(baseline.clarifications) <= 30
    # Workflow may call the helper again with the already grouped Coverage
    # list plus package-level flags. Re-expansion must retain original details.
    merged_usage = dict(baseline.usage)
    merged = AnalysisPipeline._bounded_context_limitations(
        [*baseline.coverage.limitations, "shared_event_package_used"], merged_usage)
    assert len(merged_usage["context_limitations"]) == 33
    assert merged_usage["context_limitation_counts"]["freshness_unknown:facts"] == 22
    assert merged_usage["context_limitation_counts"]["unresolved:assertions"] == 3
    assert "shared_event_package_used" in merged

    class FailedModel:
        def complete(self, *_args, **_kwargs):
            raise RuntimeError("synthetic provider failure")
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    fallback = AnalysisPipeline(store(fallback_dir), llm=FailedModel()).run(full_request)
    assert fallback.status == "partial"
    assert fallback.usage["errors"]
    assert len(fallback.usage["context_limitations"]) == 33
    assert len(fallback.coverage.limitations) <= 30
    assert len(fallback.clarifications) <= 30

    # The no-candidate constructor follows the same bounded presentation rule.
    unknown_usage = {}
    unknown = AnalysisPipeline(store(tmp_path / "unknown"), llm=None)._unknown(
        full_request, [], unknown_usage, "unknown")
    assert len(unknown.coverage.limitations) <= 30
    assert len(unknown_usage["context_limitations"]) == 31
    assert unknown_usage["context_limitation_counts"]["freshness_unknown:facts"] == 22

    # If global categories themselves exceed the schema, preserve them all in
    # usage and make the bounded Coverage summary's truncation explicit.
    many_usage = {}
    many = AnalysisPipeline._bounded_context_limitations(
        [f"global_limitation_{i}" for i in range(35)], many_usage)
    assert len(many) == 30
    assert many[-1] == "additional_limitations_in_usage"
    assert len(many_usage["context_limitations"]) == 35
