from __future__ import annotations

import hmac
import os
from datetime import datetime
from pathlib import Path
import hashlib
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .artifacts import ArtifactStore, payload_hash
from .schemas import AnalysisRequest, CandidateAssessment
from .workflow import DataTask, DataResult, DataWorkflow, WorkflowNotStarted, WorkflowSnapshotConflict
from .collection import SourcePolicySnapshot, SourceProbeRequest, SourceProbeResult
from .client_data import CompanyContextPrepareRequest, CompanyContextPrepareResult, prepare_company_contexts
from .relevance import RankRequest, RankResult, rank_events
from .description_context import DescriptionPrepareRequest, DescriptionProposal, prepare_description


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentInput(_StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    source_role: Literal["external", "bank"]
    canonical_uri: str = Field(max_length=2048)
    title: str = Field(max_length=1000)
    content: str = Field(max_length=26_700_000)
    content_type: Literal["text/plain", "text/html", "application/pdf", "application/rss+xml"]
    published_at: datetime
    fetched_at: datetime
    synthetic: bool = False
    data_classification: Literal["public","synthetic","confidential"] = "confidential"
    publisher: str = Field(default="", max_length=500)
    language: str = Field(default="ru", max_length=24)
    author: str = Field(default="", max_length=500)
    event_time: datetime | None = None

    @field_validator("published_at", "fetched_at")
    @classmethod
    def dates_must_be_aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None: raise ValueError("timestamp must include timezone")
        return value

    @model_validator(mode="after")
    def bound_non_pdf_text(self):
        if self.content_type != "application/pdf" and len(self.content) > 2_000_000:
            raise ValueError("text document exceeds 2 MB")
        return self


class SearchInput(_StrictBody):
    source_policy: SourcePolicySnapshot | None = None
    tenant_id: str = Field(min_length=1, max_length=128)
    source_ids: list[str] = Field(min_length=1, max_length=100)
    as_of: datetime
    query: str = Field(min_length=1, max_length=1000)
    document_revision_refs: list[str] | None = Field(default=None, max_length=500)
    role: Literal["external", "bank"] | None = None
    limit: int = Field(default=8, ge=1, le=20)

    @field_validator("as_of")
    @classmethod
    def date_must_be_aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None: raise ValueError("timestamp must include timezone")
        return value


class OriginalInput(_StrictBody):
    tenant_id: str = Field(min_length=1,max_length=128)
    source_policy: SourcePolicySnapshot
    document_revision_ref: str = Field(min_length=1,max_length=512)
    as_of: datetime


class _BodyLimit:
    """Bound even chunked request bodies before FastAPI parses JSON."""
    def __init__(self, app, limit: int): self.app, self.limit = app, limit
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http": return await self.app(scope, receive, send)
        length = next((v.decode() for k, v in scope.get("headers", []) if k.lower() == b"content-length"), None)
        if length:
            try:
                if int(length) > self.limit:
                    from starlette.responses import JSONResponse
                    return await JSONResponse({"detail":"request_too_large"}, status_code=413)(scope, receive, send)
            except ValueError:
                from starlette.responses import JSONResponse
                return await JSONResponse({"detail":"request_too_large"}, status_code=413)(scope, receive, send)
        messages, total = [], 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.limit:
                    from starlette.responses import JSONResponse
                    return await JSONResponse({"detail":"request_too_large"}, status_code=413)(scope, receive, send)
                if not message.get("more_body", False): break
            elif message["type"] == "http.disconnect": return
        async def replay():
            if messages: return messages.pop(0)
            return await receive()
        await self.app(scope, replay, send)


def create_app(*, store, pipeline, artifact_store: ArtifactStore | None = None,
               data_workflow=None, service_token: str | None = None, tenant_ids: set[str] | list[str] | None = None) -> FastAPI:
    """Build private service API. Missing identity configuration denies all private calls."""
    token = service_token if service_token is not None else os.getenv("INTELLIGENCE_SERVICE_TOKEN", "")
    tenants = set(tenant_ids if tenant_ids is not None else (v.strip() for v in os.getenv("INTELLIGENCE_TENANTS", "").split(",") if v.strip()))
    artifacts = artifact_store or ArtifactStore(os.getenv("INTELLIGENCE_ARTIFACT_DB", "/data/intelligence-artifacts.sqlite3"))
    app = FastAPI(title="Intelligence internal API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.pipeline, app.state.artifact_store = store, pipeline, artifacts
    from fastapi.exceptions import RequestValidationError
    from starlette.responses import JSONResponse
    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        return JSONResponse(status_code=422, content={"detail":"invalid_request", "errors":[
            {"type":error["type"],"loc":list(error["loc"])} for error in exc.errors()]})

    app.add_middleware(_BodyLimit, limit=30_000_000)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    def auth(authorization: str | None = Header(default=None)):
        if not token or not tenants or not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "unauthorized")
        supplied = authorization[7:]
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "unauthorized")
        return True

    def tenant(body: dict):
        tenant_id = body.get("tenant_id")
        if not isinstance(tenant_id, str) or tenant_id not in tenants:
            raise HTTPException(403, "tenant_forbidden")
        return tenant_id

    @app.post("/internal/v1/documents")
    def ingest(body: DocumentInput, _: bool = Depends(auth)):
        tenant_id = tenant(body.model_dump(mode="json"))
        try: return store.ingest(body.model_dump(mode="json"))
        except (ValueError, ValidationError) as e: raise HTTPException(422, "invalid_document")

    @app.post("/internal/v1/search")
    def search(body: SearchInput, _: bool = Depends(auth)):
        values = body.model_dump(mode="json")
        tenant_id = tenant(values)
        try:
            refs = body.document_revision_refs
            if body.source_policy is not None:
                if body.source_policy.tenant_id != tenant_id or not set(body.source_ids).issubset(body.source_policy.effective_source_ids()):
                    raise HTTPException(403,"policy_scope_mismatch")
                documents = store.list_documents(tenant_id=tenant_id,source_ids=body.source_ids,as_of=body.as_of.isoformat(),policy=body.source_policy)
                permitted = {item["document_revision_ref"] for item in documents}
                refs = [ref for ref in (refs if refs is not None else permitted) if ref in permitted]
                if not refs: return []
            return store.search(tenant_id=tenant_id, source_ids=body.source_ids, as_of=body.as_of.isoformat(),
                                query=body.query, document_revision_refs=refs,
                                role=body.role, limit=body.limit)
        except (KeyError, TypeError, ValueError) as e: raise HTTPException(422, "invalid_search_request") from e

    @app.post("/internal/v1/rank", response_model=RankResult)
    def rank(request: RankRequest, _: bool = Depends(auth)):
        values=request.model_dump(mode="json")
        tenant(values)
        try: return rank_events(**values)
        except ValueError as exc: raise HTTPException(422,"invalid_ranking_request") from exc

    @app.post("/internal/v1/analyze", response_model=CandidateAssessment | DataResult)
    def analyze(request: AnalysisRequest | DataTask, _: bool = Depends(auth)):
        body = request.model_dump(mode="json")
        tenant_id = tenant(body)
        req_id = str(request.request_id)
        digest = payload_hash(body)
        try:
            prior = artifacts.get_candidate(tenant_id, req_id, digest)
        except ValueError as e: raise HTTPException(409, "idempotency_conflict") from e
        if isinstance(request, DataTask):
            try:
                validate_snapshot=getattr(get_workflow(),'assert_current',None)
                if validate_snapshot: validate_snapshot(request)
            except WorkflowSnapshotConflict as exc: raise HTTPException(409, str(exc)) from exc
        if prior is not None: return prior
        try:
            owner_token = artifacts.claim_candidate(tenant_id, req_id, digest)
        except ValueError as e: raise HTTPException(409, "idempotency_conflict") from e
        if owner_token is None:
            # An identical request is already executing, or is awaiting operator recovery after a crash.
            raise HTTPException(409, "request_in_progress")
        try:
            if isinstance(request, DataTask):
                try:
                    raw_result = get_workflow().run(request)
                except WorkflowNotStarted as e:
                    # This is the sole safe retry signal. It is deliberately
                    # caught only around DataWorkflow.run, before validation or
                    # persistence can fail after work has started.
                    artifacts.release_candidate(tenant_id, req_id, digest, owner_token)
                    code = str(e)
                    if code in {"capacity_exhausted", "request_in_progress"}:
                        raise HTTPException(409, code) from e
                    raise HTTPException(503, "workflow_not_started") from e
                result = DataResult.model_validate(raw_result)
            else:
                result = CandidateAssessment.model_validate(pipeline.run(request))
            return artifacts.save_candidate(tenant_id, req_id, digest, result, owner_token=owner_token)
        except WorkflowSnapshotConflict as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            if str(e) == "idempotency_conflict": raise HTTPException(409, "idempotency_conflict") from e
            code = str(e)
            if code in {"capacity_exhausted","request_in_progress"}: raise HTTPException(409,code) from e
            raise HTTPException(422,"analysis_input_or_configuration_error") from e

    def get_workflow():
        nonlocal data_workflow
        if data_workflow is None:
            data_workflow = DataWorkflow(Path(artifacts.path).parent,llm=getattr(pipeline,"llm",None),role_services=getattr(pipeline,"role_services",None),analyst_config=getattr(pipeline,"analyst_config",None))
        return data_workflow

    @app.post("/internal/v1/company-context/prepare",response_model=CompanyContextPrepareResult)
    def prepare_context(request: CompanyContextPrepareRequest, _: bool = Depends(auth)):
        tenant(request.model_dump(mode="json"))
        return prepare_company_contexts(**request.model_dump())

    @app.post("/internal/v1/company-context/from-description",response_model=DescriptionProposal)
    def description_context(request: DescriptionPrepareRequest, _: bool = Depends(auth)):
        tenant(request.model_dump(mode="json"))
        # The short deterministic route never calls a provider. Model extraction
        # uses the ordinary prepare_description Core job and its shared budget.
        return prepare_description(request)

    @app.post("/internal/v1/sources/probe", response_model=SourceProbeResult)
    def probe(request: SourceProbeRequest, _: bool = Depends(auth)):
        tenant(request.model_dump(mode="json"))
        return get_workflow().collector.probe(request)

    @app.post("/internal/v1/originals")
    def original(request: OriginalInput, _: bool = Depends(auth)):
        tenant(request.model_dump(mode="json"))
        if request.source_policy.tenant_id != request.tenant_id: raise HTTPException(403,"policy_scope_mismatch")
        try:
            raw = get_workflow().store.read_original(tenant_id=request.tenant_id,source_ids=request.source_policy.effective_source_ids(),
                document_revision_ref=request.document_revision_ref,as_of=request.as_of.isoformat(),policy=request.source_policy)
        except ValueError as exc: raise HTTPException(404,"original_not_permitted") from exc
        from starlette.responses import Response
        return Response(raw,media_type="application/octet-stream",headers={"X-Content-SHA256":hashlib.sha256(raw).hexdigest(),"Content-Disposition":"attachment"})

    @app.get("/internal/v1/executions/{request_id}")
    def progress(request_id: str, tenant_id: str, _: bool = Depends(auth)):
        tenant({"tenant_id":tenant_id})
        workflow=get_workflow()
        with workflow.state.db() as db:
            rows=db.execute("SELECT stage,payload FROM workflow_stages WHERE tenant=? AND request=? ORDER BY rowid",(tenant_id,request_id)).fetchall()
        import json
        return {"request_id":request_id,"stages":[{"stage":row[0],"details":json.loads(row[1])} for row in rows]}

    return app
