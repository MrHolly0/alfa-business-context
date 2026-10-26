"""Narrow, operator-configured disclosure approval for organizer case material.

The approval file authorizes only a fixed company-context snapshot and exact
private document originals. It is never inferred from a request flag.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .artifacts import payload_hash
from .llm.config import HACKATHON_BASES


class ApprovedCaseDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=256)
    document_revision_ref: str = Field(min_length=1, max_length=512)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_uri: str = Field(min_length=1, max_length=4096)


class CaseApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    tenant_id: str
    company_profiles: dict[str, str]
    company_csv_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    documents: tuple[ApprovedCaseDocument, ...]


def load_case_approval() -> CaseApproval | None:
    """Load the small operator-owned allowlist; malformed or absent files deny."""
    filename = os.getenv("INTELLIGENCE_CASE_APPROVAL_FILE")
    if not filename:
        return None
    try:
        path = Path(filename)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 256_000:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or not {"schema_version", "tenant_id", "company_profiles", "documents"}.issubset(value)
                or set(value) - {"schema_version", "tenant_id", "company_profiles", "company_csv_sha256", "documents"}):
            return None
        if value.get("schema_version") != "organizer-model-approval-v1" or value.get("tenant_id") != "core-local":
            return None
        profiles = value.get("company_profiles")
        if (not isinstance(profiles, dict) or not profiles or len(profiles) > 20
                or any(not isinstance(key, str) or not key or not isinstance(digest, str)
                       or re.fullmatch(r"[0-9a-f]{64}", digest) is None for key, digest in profiles.items())):
            return None
        raw_documents = value.get("documents")
        if not isinstance(raw_documents, list) or len(raw_documents) > 100:
            return None
        documents = tuple(ApprovedCaseDocument.model_validate(row) for row in raw_documents)
        keys = [(item.source_id, item.document_revision_ref) for item in documents]
        if len(keys) != len(set(keys)) or any(not item.canonical_uri.startswith("https://") for item in documents):
            return None
        return CaseApproval(schema_version=value["schema_version"], tenant_id=value["tenant_id"],
                            company_profiles=profiles, company_csv_sha256=value.get("company_csv_sha256"),
                            documents=documents)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return None


def _service_config(service):
    return getattr(service, "config", None)


def _as_mapping(value):
    if isinstance(value, Mapping):
        return value
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        try:
            return dumper(mode="json")
        except (TypeError, ValueError):
            return None
    return None


def _document_fields(document: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    values = (document.get("source_id"), document.get("document_revision_ref"),
              document.get("raw_sha256"), document.get("canonical_uri"))
    if not all(isinstance(value, str) and value for value in values):
        return None
    return values


def approved_case_data(request, documents, service, *, case_approval: CaseApproval | None = None,
                       evidence=(), package: Mapping[str, Any] | None = None) -> CaseApproval | None:
    """Return the current operator approval iff this exact payload may reach Alfa.

    Call this before each role invocation. The token must equal the freshly loaded
    operator file; tenant/company, every selected document, and the actual service
    endpoint are checked again at use time.
    """
    current = load_case_approval()
    if current is None or (case_approval is not None and case_approval != current):
        return None
    if getattr(request, "tenant_id", None) != current.tenant_id:
        return None
    company_id = getattr(request, "company_id", None)
    company = getattr(request, "company", None)
    if (not isinstance(company_id, str) or company_id not in current.company_profiles or company is None
            or current.company_csv_sha256 is None):
        return None
    try:
        if payload_hash(company) != current.company_profiles[company_id]:
            return None
        profile = company.model_dump(mode="json") if hasattr(company, "model_dump") else company
        facts = profile.get("facts", []) if isinstance(profile, Mapping) else []
        if not any("df_Uid_data_hack.csv" in str(fact.get("provenance", ""))
                   for fact in facts if isinstance(fact, Mapping)):
            return None
    except (TypeError, ValueError):
        return None
    config = _service_config(service)
    if (getattr(config, "profile", None) != "hackathon"
            or getattr(config, "base_url", None) not in HACKATHON_BASES):
        return None

    approved = {(item.source_id, item.document_revision_ref, item.raw_sha256, item.canonical_uri)
                for item in current.documents}
    requested_refs = set(getattr(request, "document_revision_refs", ()) or ())
    observed_refs: set[str] = set()
    trusted_documents = []
    trusted_by_fields = {}
    for original_document in documents or ():
        document = _as_mapping(original_document)
        if document is None:
            return None
        tenant = document.get("tenant_id")
        source_id = document.get("source_id")
        if (tenant != current.tenant_id or not isinstance(source_id, str)
                or source_id not in set(getattr(request, "source_ids", ()) or ())):
            return None
        ref = document.get("document_revision_ref")
        if isinstance(ref, str):
            observed_refs.add(ref)
        fields = _document_fields(document)
        if fields is not None:
            trusted_documents.append(document)
            trusted_by_fields[fields] = document
        classification = document.get("data_classification", "confidential")
        if classification not in {"public", "synthetic"}:
            if document.get("source_role") != "bank":
                return None
            if fields is None or fields not in approved:
                return None

    for original_evidence in evidence or ():
        item = _as_mapping(original_evidence)
        if item is None:
            return None
        fields = _document_fields(item)
        if fields is None or fields[0] not in set(getattr(request, "source_ids", ()) or ()):
            return None
        evidence_tenant = item.get("tenant_id")
        trusted_document = trusted_by_fields.get(fields)
        if evidence_tenant is None:
            # EvidenceRecord intentionally omits tenant_id. Accept it only when
            # its complete source/revision/original tuple matches a pinned row
            # read from the request tenant's CorpusStore.
            if trusted_document is None or trusted_document.get("tenant_id") != current.tenant_id:
                return None
        elif evidence_tenant != current.tenant_id:
            return None
        if trusted_document is None:
            return None
        observed_refs.add(fields[1])
        classification = item.get("data_classification", "confidential")
        if classification not in {"public", "synthetic"}:
            if (item.get("source_role") != "bank" or fields not in approved
                    or trusted_document.get("data_classification", "confidential") not in {"confidential"}):
                return None
    if requested_refs and not requested_refs.issubset(observed_refs):
        return None

    if package is not None:
        if not isinstance(package, Mapping) or package.get("tenant_id") != current.tenant_id:
            return None
        doc_by_ref = {document.get("document_revision_ref"): document for document in trusted_documents}
        for source in package.get("sources", ()):
            if not isinstance(source, Mapping):
                return None
            if source.get("data_classification", "confidential") not in {"public", "synthetic"}:
                ref = source.get("document_revision_ref")
                document = doc_by_ref.get(ref)
                fields = _document_fields(document) if isinstance(document, Mapping) else None
                if (fields is None or fields not in approved or fields[0] != source.get("source_id")
                        or fields[1] != ref or fields[3] != source.get("canonical_uri")):
                    return None
    return current


def request_case_documents(store, request):
    """Read only request-pinned corpus revisions for repeat authorization checks."""
    refs = set(getattr(request, "document_revision_refs", ()) or ())
    if not refs:
        return []
    documents = store.list_documents(tenant_id=request.tenant_id, source_ids=list(request.source_ids),
                                     as_of=request.as_of.isoformat())
    return [document for document in documents if document.get("document_revision_ref") in refs]


def role_may_receive_case_data(request, documents, service, *, case_approval=None,
                               evidence=(), package=None, data_classification=None) -> bool:
    """Use ordinary public/synthetic policy or revalidate the narrow case approval."""
    safe_task_classification = (data_classification in {"public", "synthetic"}
                                or (data_classification is None and request.synthetic))
    private = (not safe_task_classification
               or any((_as_mapping(item) or {}).get("data_classification", "confidential") not in {"public", "synthetic"}
                      for item in documents or ())
               or any((_as_mapping(item) or {}).get("data_classification", "confidential") not in {"public", "synthetic"}
                      for item in evidence or ())
               or bool(package and (package.get("data_classification", "confidential") not in {"public", "synthetic"}
                   or any(source.get("data_classification", "confidential") not in {"public", "synthetic"}
                          for source in package.get("sources", ()))))
               )
    if not private:
        return True
    return approved_case_data(request, documents, service, case_approval=case_approval,
                              evidence=evidence, package=package) is not None
