"""Guardrails for the explicitly enabled organizer case gateway.

All inputs are synthetic. The workflow's normal PostgreSQL test boundary is used;
no provider calls are made here.
"""
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from intelligence.artifacts import payload_hash
from intelligence.collection import FetchResult, NewsCollector, SourceGrant, SourcePolicySnapshot
from intelligence.llm.config import HACKATHON_BASES
from intelligence.workflow import BusinessSnapshot, DataTask, DataWorkflow


NOW = datetime(2026, 10, 26, 9, tzinfo=timezone.utc)
CSV = "df_Uid_data_hack.csv#row=3"
FEED = b'''<?xml version="1.0"?><rss version="2.0"><channel><title>Synthetic</title>
<item><title>Public synthetic update</title><description>A synthetic update.</description>
<pubDate>Mon, 26 Oct 2026 08:00:00 +0000</pubDate><link>https://news.example/item</link></item>
</channel></rss>'''


def _service(base, profile="hackathon"):
    return SimpleNamespace(config=SimpleNamespace(profile=profile, base_url=base,
        model="google/gemma-4-26b-a4b-it", pricing_mode="fixed_reserve"))


def _inputs(tmp_path, *, tenant="core-local", provenance=CSV, source_policy=None):
    grants = source_policy or SourcePolicySnapshot(policy_id="case-policy", revision=1,
        tenant_id=tenant, issued_at=NOW, sources=(
            SourceGrant(source_id="external", source_role="external", data_classification="synthetic",
                uri="https://feed.example/rss", content_type="application/rss+xml", publisher="Synthetic",
                article_hosts=("news.example",)),
            SourceGrant(source_id="bank", source_role="bank", data_classification="confidential",
                uri="https://bank.example/rss", content_type="application/rss+xml", publisher="Synthetic bank"),
        ))
    business = BusinessSnapshot.model_validate({"company_id":"case-company", "context_version":1,
        "company":{"name":"Synthetic company", "industry":"logistics", "facts":[
            {"fact_id":"csv-fact", "text":"Synthetic fact from the case CSV", "provenance":provenance}
        ]}})
    task = DataTask(request_id="case-collect", tenant_id=tenant, company_id="case-company",
        context_version=1, source_policy_version=grants.revision, operation="collect", as_of=NOW,
        source_policy=grants, businesses=[business], data_classification="confidential",
        budget={"max_model_calls":0})
    return grants, task


def _workflow(tmp_path, services, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_ALLOW_HACKATHON_CASE_DATA", "1")
    monkeypatch.delenv("INTELLIGENCE_CASE_APPROVAL_FILE", raising=False)
    workflow = DataWorkflow(tmp_path / "state", role_services=services, clock=lambda: NOW)
    return workflow


def _install_approval(tmp_path, monkeypatch, task, documents=()):
    value = {"schema_version":"organizer-model-approval-v1", "tenant_id":"core-local",
        "company_csv_sha256":"d" * 64,
        "company_profiles":{item.company_id:payload_hash(item.company) for item in task.businesses},
        "documents":list(documents)}
    path = tmp_path / "organizer-model-approval.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setenv("INTELLIGENCE_CASE_APPROVAL_FILE", str(path))


def test_case_gateway_denied_without_operator_flag(tmp_path, monkeypatch):
    _, task = _inputs(tmp_path)
    workflow = _workflow(tmp_path, {"analyst":_service(next(iter(HACKATHON_BASES)))} , monkeypatch)
    monkeypatch.delenv("INTELLIGENCE_ALLOW_HACKATHON_CASE_DATA")
    assert workflow._case_gateway_allowed(task) is False


@pytest.mark.parametrize("tenant", ["tenant-a", "another-tenant"])
def test_case_gateway_denied_for_other_tenant(tmp_path, monkeypatch, tenant):
    _, task = _inputs(tmp_path, tenant=tenant)
    workflow = _workflow(tmp_path, {"analyst":_service(next(iter(HACKATHON_BASES)))} , monkeypatch)
    assert workflow._case_gateway_allowed(task) is False


@pytest.mark.parametrize("services", [
    {"analyst":_service("https://fake.example/v1")},
    {"analyst":_service("https://openrouter.ai/api/v1", profile="openrouter")},
    {"analyst":_service(next(iter(HACKATHON_BASES)), "hackathon"),
     "critic":_service("https://openrouter.ai/api/v1", "openrouter")},
])
def test_case_gateway_denied_for_unapproved_or_mixed_routes(tmp_path, monkeypatch, services):
    _, task = _inputs(tmp_path)
    workflow = _workflow(tmp_path, services, monkeypatch)
    _install_approval(tmp_path, monkeypatch, task)
    assert workflow._case_gateway_allowed(task) is False


def test_case_gateway_denied_without_csv_provenance(tmp_path, monkeypatch):
    _, task = _inputs(tmp_path, provenance="manual note")
    workflow = _workflow(tmp_path, {"analyst":_service(next(iter(HACKATHON_BASES)))}, monkeypatch)
    assert workflow._case_gateway_allowed(task) is False


def test_case_gateway_allows_exact_alfa_core_and_csv_case(tmp_path, monkeypatch):
    _, task = _inputs(tmp_path)
    for base in HACKATHON_BASES:
        workflow = _workflow(tmp_path, {"analyst":_service(base)}, monkeypatch)
        _install_approval(tmp_path, monkeypatch, task)
        assert workflow._case_gateway_allowed(task) is True


def test_case_collection_fetches_external_only_but_retains_authorized_bank_docs(tmp_path, monkeypatch):
    _, task = _inputs(tmp_path)
    workflow = _workflow(tmp_path, {"analyst":_service("https://api.alfahack-gateway26.ru/v1")}, monkeypatch)

    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
    pdf_path = tmp_path / "authorized-bank-note.pdf"
    pdf_path.write_bytes(pdf_bytes)
    bank_doc = workflow.store.ingest_private_case_pdf({"tenant_id":"core-local", "source_id":"bank",
        "source_role":"bank", "canonical_uri":"https://bank.example/case-note",
        "title":"Synthetic bank case note", "content":"", "content_type":"application/pdf",
        "published_at":NOW.isoformat(), "fetched_at":NOW.isoformat(), "synthetic":False,
        "data_classification":"confidential"}, pdf_path, allowed_root=tmp_path,
        ocr_text="[page 1]\nSynthetic confidential note retained locally.")
    _install_approval(tmp_path, monkeypatch, task, documents=[{
        "source_id":"bank", "document_revision_ref":bank_doc["document_revision_ref"],
        "raw_sha256":hashlib.sha256(pdf_bytes).hexdigest(), "canonical_uri":"https://bank.example/case-note"}])

    class Fetcher:
        calls = []
        def __call__(self, url, headers, **kwargs):
            self.calls.append(url)
            return FetchResult(status=200, headers={"content-type":"application/rss+xml"}, body=FEED)

    fetcher = Fetcher()
    workflow.collector = NewsCollector(workflow.store, state_path=tmp_path / "poll.sqlite3",
        fetcher=fetcher, raw_dir=workflow.state_dir / "raw")
    result = workflow.run(task)

    assert fetcher.calls == ["https://feed.example/rss"]
    documents = result.collection["documents"]
    assert {item["source_role"] for item in documents} == {"external", "bank"}, (
        result.collection.get("fetched"), result.collection.get("errors"),
        result.collection.get("skipped"), result.collection.get("documents"))
    assert any(item["document_revision_ref"] == bank_doc["document_revision_ref"] for item in documents)
    assert result.coverage["bank_documents_preloaded"] == 1
    assert result.provider["profile"] == "hackathon"


def test_changed_profile_uses_local_bank_corpus_without_refetch_or_model(tmp_path, monkeypatch):
    from intelligence.schemas import CompanyFact
    _, original = _inputs(tmp_path)
    calls = []
    service = _service("https://api.alfahack-gateway26.ru/v1")
    def forbidden_model(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("private_profile_must_not_leave")
    service.complete = forbidden_model
    workflow = _workflow(tmp_path, {"analyst": service}, monkeypatch)
    data = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
    pdf_path = tmp_path / "pinned.pdf"
    pdf_path.write_bytes(data)
    document = workflow.store.ingest_private_case_pdf({"tenant_id": "core-local", "source_id": "bank",
        "source_role": "bank", "canonical_uri": "https://bank.example/rss", "title": "Private case note",
        "content": "", "content_type": "application/pdf", "published_at": NOW.isoformat(),
        "fetched_at": NOW.isoformat(), "synthetic": False, "data_classification": "confidential"},
        pdf_path, allowed_root=tmp_path, ocr_text="[page 1]\nPrivate bank note for local analysis.")
    _install_approval(tmp_path, monkeypatch, original, documents=[{
        "source_id": "bank", "document_revision_ref": document["document_revision_ref"],
        "raw_sha256": hashlib.sha256(data).hexdigest(), "canonical_uri": "https://bank.example/rss"}])
    changed_company = original.businesses[0].company.model_copy(update={"facts": [
        *original.businesses[0].company.facts,
        CompanyFact(fact_id="manual-edit", text="Additional private observation", provenance="Core user context"),
    ]})
    changed = original.model_copy(update={"context_version": 2,
        "businesses": [original.businesses[0].model_copy(update={"context_version": 2, "company": changed_company})],
        "budget": original.budget.model_copy(update={"max_model_calls": 3})})
    urls = []
    def fetch(url, headers, **kwargs):
        urls.append(url)
        return FetchResult(status=200, headers={"content-type": "application/rss+xml"}, body=FEED)
    workflow.collector = NewsCollector(workflow.store, state_path=tmp_path / "collection.sqlite3", fetcher=fetch)
    result = workflow.run(changed)
    assert urls == ["https://feed.example/rss"]
    assert calls == []
    assert result.coverage["model_limitation"] == "organizer_profile_requires_approval"
    assert result.coverage["bank_documents_preloaded"] == 1
    assert any(item["source_role"] == "bank" for item in result.collection["documents"])
