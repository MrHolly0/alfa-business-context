"""Narrow organizer-data approval checks for each external LLM role."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from intelligence.artifacts import payload_hash
from intelligence.case_permission import load_case_approval, role_may_receive_case_data
from intelligence.corpus import CorpusStore
from intelligence.multi_agent import MultiAgentPipeline
from intelligence.pipeline import AnalysisPipeline
from intelligence.schemas import AnalysisRequest, EvidenceRecord


NOW = "2026-10-26T09:00:00Z"
BANK_REF = "doc-bank-rev-1"
NEWS_REF = "doc-news-rev-1"
BANK_RAW = "a" * 64


def _request(**updates):
    data = {
        "request_id":"case-review", "tenant_id":"core-local", "company_id":"company-42",
        "as_of":NOW, "context_version":1, "source_policy_version":1,
        "source_ids":["news-source", "bank-source"], "document_revision_refs":[NEWS_REF, BANK_REF],
        "synthetic":False,
        "company":{"name":"Synthetic Case Company", "industry":"logistics", "description":"CSV-backed profile",
            "facts":[{"fact_id":"csv-1", "text":"Synthetic approved fact", "provenance":"df_Uid_data_hack.csv#row=3"}]},
        "event":{"event_id":"event-case", "title":"Port status", "text":"The port is temporarily closed.",
            "source_id":"news-source", "published_at":NOW, "document_revision_ref":NEWS_REF},
        "budget":{"max_model_calls":1, "max_input_tokens":24000, "max_output_tokens":512,
            "max_total_tokens":25000, "max_steps":12},
    }
    data.update(updates)
    return AnalysisRequest.model_validate(data)


def _documents():
    news = {"tenant_id":"core-local", "source_id":"news-source", "source_role":"external",
        "document_revision_ref":NEWS_REF, "canonical_uri":"https://news.example/port", "title":"Port status",
        "content":"The port is temporarily closed.", "published_at":NOW, "fetched_at":NOW,
        "data_classification":"public", "raw_sha256":"b" * 64, "synthetic":False}
    bank = {"tenant_id":"core-local", "source_id":"bank-source", "source_role":"bank",
        "document_revision_ref":BANK_REF, "canonical_uri":"https://drive.google.com/file/d/case-1",
        "title":"Approved synthetic bank excerpt", "content":"The case bank excerpt describes port exposure.",
        "published_at":NOW, "fetched_at":NOW, "data_classification":"confidential",
        "raw_sha256":BANK_RAW, "synthetic":False}
    return [news, bank]


def _approval_file(tmp_path, monkeypatch, request=None, documents=None):
    request = request or _request()
    documents = documents or _documents()
    value = {"schema_version":"organizer-model-approval-v1", "tenant_id":"core-local",
        "company_profiles":{"company-42":payload_hash(request.company)},
        "company_csv_sha256":"c" * 64,
        "documents":[{"source_id":"bank-source", "document_revision_ref":BANK_REF,
            "raw_sha256":BANK_RAW, "canonical_uri":"https://drive.google.com/file/d/case-1"}]}
    path = tmp_path / "case-approval.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setenv("INTELLIGENCE_CASE_APPROVAL_FILE", str(path))
    return load_case_approval(), documents


class _CaseCorpus:
    def __init__(self, docs):
        self.docs = docs
        self.evidence = []
        for doc in docs:
            quote = doc["content"]
            self.evidence.append({**doc, "evidence_id":"ev-" + doc["source_id"], "quote":quote,
                "content_hash":hashlib.sha256(quote.encode()).hexdigest(), "locator":"text:1"})

    def list_documents(self, *, tenant_id, source_ids, as_of, **kwargs):
        return [doc for doc in self.docs if doc["tenant_id"] == tenant_id and doc["source_id"] in source_ids]

    def search(self, *, role, **kwargs):
        return [item for item in self.evidence if item["source_role"] == role]


class _LLM:
    def __init__(self, profile, base_url):
        self.config = SimpleNamespace(profile=profile, base_url=base_url, model="test", max_output_tokens=512)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return {"content":"{}", "model":"test", "usage":{"prompt_tokens":10,"completion_tokens":10},
            "cost":None,"cost_currency":None,"cost_source":"unknown"}


class _RoleLLM(_LLM):
    def __init__(self, role, profile="hackathon", base_url="https://api.alfahack-gateway26.ru/v1"):
        super().__init__(profile, base_url)
        self.role = role
        self.config.budget_scope = "case-test"
        self.config.currency = "RUB"

    def complete(self, messages, **kwargs):
        self.calls.append(messages)
        content = {
            "researcher":'{"searches":[{"role":"bank","query":"port status"}],"missing_context":[]}',
            "analyst":"{}",
            "critic":'{"decision":"accept","issues":[],"clarifications":[],"searches":[]}',
        }[self.role]
        return {"content":content, "model":"test", "usage":{"prompt_tokens":10,"completion_tokens":10},
            "cost":None,"cost_currency":None,"cost_source":"unknown"}


def _case_run(tmp_path, monkeypatch, *, profile="hackathon", base_url="https://api.alfahack-gateway26.ru/v1",
              approval=True, company_change=None):
    request = _request()
    if company_change:
        request = request.model_copy(update={"company":request.company.model_copy(update=company_change)})
    token, docs = _approval_file(tmp_path, monkeypatch, request=request)
    if not approval:
        token = None
    llm = _LLM(profile, base_url)
    result = AnalysisPipeline(_CaseCorpus(docs), llm=llm).run(request, data_classification="confidential",
        case_approval=token)
    return result, llm, request, docs, token


def test_confidential_case_uses_only_exact_alfa_with_current_approval(tmp_path, monkeypatch):
    result, llm, *_ = _case_run(tmp_path, monkeypatch)
    assert len(llm.calls) == 1
    assert result.usage["model_calls"] == 1


@pytest.mark.parametrize("profile,base_url,approval", [
    ("hackathon", "https://api.alfahack-gateway26.ru/v1", False),
    ("openrouter", "https://openrouter.ai/api/v1", True),
    ("hackathon", "https://fake.invalid/v1", True),
])
def test_confidential_case_denied_without_exact_alfa_approval(tmp_path, monkeypatch, profile, base_url, approval):
    result, llm, *_ = _case_run(tmp_path, monkeypatch, profile=profile, base_url=base_url, approval=approval)
    assert llm.calls == []
    assert "external_model_data_classification_denied" in result.usage["errors"]


def test_case_approval_pins_company_snapshot_and_original_document(tmp_path, monkeypatch):
    request = _request()
    token, docs = _approval_file(tmp_path, monkeypatch, request=request)
    service = _LLM("hackathon", "https://changellenge.up.railway.app/v1")
    assert role_may_receive_case_data(request, docs, service, case_approval=token,
                                      data_classification="confidential") is True
    changed_profile = request.model_copy(update={"company":request.company.model_copy(update={
        "description":"edited after approval"})})
    assert role_may_receive_case_data(changed_profile, docs, service, case_approval=token,
                                      data_classification="confidential") is False
    changed_doc = [dict(docs[0]), {**docs[1], "raw_sha256":"c" * 64}]
    assert role_may_receive_case_data(request, changed_doc, service, case_approval=token,
                                      data_classification="confidential") is False


def test_case_approval_denies_openrouter_even_with_matching_token(tmp_path, monkeypatch):
    request = _request()
    token, docs = _approval_file(tmp_path, monkeypatch, request=request)
    service = _LLM("openrouter", "https://openrouter.ai/api/v1")
    assert role_may_receive_case_data(request, docs, service, case_approval=token,
                                      data_classification="confidential") is False


def test_multi_agent_confidential_roles_need_the_same_exact_case_approval(tmp_path, monkeypatch):
    request = _request()
    token, docs = _approval_file(tmp_path, monkeypatch, request=request)
    roles = {role:_RoleLLM(role) for role in ("researcher", "analyst", "critic")}
    request = request.model_copy(update={"budget":request.budget.model_copy(update={"max_model_calls":3})})
    result = MultiAgentPipeline(_CaseCorpus(docs), role_services=roles).run(
        request, data_classification="confidential", case_approval=token)
    assert all(len(service.calls) == 1 for service in roles.values())
    assert result.usage["model_calls"] == 3
    critic_payload = json.loads(roles["critic"].calls[0][-1]["content"])
    assert "company_context" in critic_payload
    assert "synthetic_company" not in critic_payload
    assert [item["fact_id"] for item in critic_payload["company_context"]["facts"]] == ["csv-1"]
    projection = next(item for item in result.trace if item["stage"] == "critic_prompt_projected")
    assert projection["company_context_included"] is True
    assert projection["company_fact_count"] == 1


def test_multi_agent_never_sends_confidential_case_material_to_openrouter(tmp_path, monkeypatch):
    request = _request()
    token, docs = _approval_file(tmp_path, monkeypatch, request=request)
    roles = {role:_RoleLLM(role) for role in ("researcher", "analyst", "critic")}
    roles["critic"] = _RoleLLM("critic", "openrouter", "https://openrouter.ai/api/v1")
    request = request.model_copy(update={"budget":request.budget.model_copy(update={"max_model_calls":3})})
    result = MultiAgentPipeline(_CaseCorpus(docs), role_services=roles).run(
        request, data_classification="confidential", case_approval=token)
    assert len(roles["researcher"].calls) == 1
    assert len(roles["analyst"].calls) == 1
    assert roles["critic"].calls == []
    assert result.usage["model_calls"] == 2


def test_tenantless_corpus_evidence_requires_exact_pinned_original_tuple(tmp_path, monkeypatch):
    corpus = CorpusStore(tmp_path / "corpus.sqlite3")
    published = NOW
    news = corpus.ingest({"tenant_id":"core-local", "source_id":"news-source", "source_role":"external",
        "canonical_uri":"https://news.example/port", "title":"Port status",
        "content":"The port is temporarily closed.", "content_type":"text/plain",
        "published_at":published, "fetched_at":published, "synthetic":False,
        "data_classification":"public"})
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
    pdf_path = tmp_path / "bank.pdf"
    pdf_path.write_bytes(pdf_bytes)
    bank = corpus.ingest_private_case_pdf({"tenant_id":"core-local", "source_id":"bank-source",
        "source_role":"bank", "canonical_uri":"https://drive.google.com/file/d/case-bank-1",
        "title":"Approved bank note", "content":"", "content_type":"application/pdf",
        "published_at":published, "fetched_at":published, "synthetic":False,
        "data_classification":"confidential"}, pdf_path, allowed_root=tmp_path,
        ocr_text="[page 1]\nThe bank note describes the port exposure.")
    request = _request()
    request_data = request.model_dump(mode="json")
    request_data["document_revision_refs"] = [news["document_revision_ref"], bank["document_revision_ref"]]
    request_data["event"]["document_revision_ref"] = news["document_revision_ref"]
    request = AnalysisRequest.model_validate(request_data)
    documents = corpus.list_documents(tenant_id="core-local", source_ids=request.source_ids, as_of=NOW)
    approval_data = {"schema_version":"organizer-model-approval-v1", "tenant_id":"core-local",
        "company_csv_sha256":"c" * 64,
        "company_profiles":{"company-42":payload_hash(request.company)},
        "documents":[{"source_id":"bank-source", "document_revision_ref":bank["document_revision_ref"],
            "raw_sha256":bank["raw_sha256"], "canonical_uri":"https://drive.google.com/file/d/case-bank-1"}]}
    approval_path = tmp_path / "case-approval.json"
    approval_path.write_text(json.dumps(approval_data), encoding="utf-8")
    monkeypatch.setenv("INTELLIGENCE_CASE_APPROVAL_FILE", str(approval_path))
    token = load_case_approval()
    raw = corpus.search(tenant_id="core-local", source_ids=["bank-source"], as_of=NOW,
        query="port exposure", role="bank", document_revision_refs=[bank["document_revision_ref"]])[0]
    evidence = EvidenceRecord.model_validate(raw)
    assert not hasattr(evidence, "tenant_id")
    service = _LLM("hackathon", "https://api.alfahack-gateway26.ru/v1")
    assert role_may_receive_case_data(request, documents, service, case_approval=token,
        evidence=[evidence], data_classification="confidential") is True
    changed_evidence = evidence.model_copy(update={"raw_sha256":"e" * 64})
    assert role_may_receive_case_data(request, documents, service, case_approval=token,
        evidence=[changed_evidence], data_classification="confidential") is False
