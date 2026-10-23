from __future__ import annotations

import hmac
import os
from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .artifacts import ArtifactStore, payload_hash
from .schemas import AnalysisRequest, CandidateAssessment
from .relevance import RankRequest, RankResult, rank_events


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
    publisher: str = Field(default="", max_length=500)
    language: str = Field(default="ru", max_length=24)

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
               service_token: str | None = None, tenant_ids: set[str] | list[str] | None = None) -> FastAPI:
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
            return store.search(tenant_id=tenant_id, source_ids=body.source_ids, as_of=body.as_of.isoformat(),
                                query=body.query, document_revision_refs=body.document_revision_refs,
                                role=body.role, limit=body.limit)
        except (KeyError, TypeError, ValueError) as e: raise HTTPException(422, "invalid_search_request") from e

    @app.post("/internal/v1/rank", response_model=RankResult)
    def rank(request: RankRequest, _: bool = Depends(auth)):
        values=request.model_dump(mode="json")
        tenant(values)
        try: return rank_events(**values)
        except ValueError as exc: raise HTTPException(422,"invalid_ranking_request") from exc

    @app.post("/internal/v1/analyze", response_model=CandidateAssessment)
    def analyze(request: AnalysisRequest, _: bool = Depends(auth)):
        body = request.model_dump(mode="json")
        tenant_id = tenant(body)
        req_id = str(request.request_id)
        digest = payload_hash(body)
        try:
            prior = artifacts.get_candidate(tenant_id, req_id, digest)
        except ValueError as e: raise HTTPException(409, "idempotency_conflict") from e
        if prior is not None: return prior
        try:
            reserved = artifacts.reserve_candidate(tenant_id, req_id, digest)
        except ValueError as e: raise HTTPException(409, "idempotency_conflict") from e
        if not reserved:
            # An identical request is already executing, or is awaiting operator recovery after a crash.
            raise HTTPException(409, "request_in_progress")
        try:
            result = CandidateAssessment.model_validate(pipeline.run(request))
            return artifacts.save_candidate(tenant_id, req_id, digest, result)
        except ValueError as e:
            if str(e) == "idempotency_conflict": raise HTTPException(409, "idempotency_conflict") from e
            raise

    return app
