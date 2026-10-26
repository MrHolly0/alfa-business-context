"""The model prompt keeps full company facts while bounding evidence excerpts."""
import json

from intelligence.analysts import AnalystConfig
from intelligence.company_context import project_company_context
from intelligence.llm.service import estimate_text_tokens
from intelligence.pipeline import AnalysisPipeline, MAX_PROMPT_EVIDENCE, MAX_PROMPT_EVIDENCE_QUOTE_CHARS
from test_pipeline import req


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
