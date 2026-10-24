"""Bounded collection controlled by immutable source policy supplied by Core.

This module executes source grants. It does not create sources, decide user rights,
or persist business policy. Raw responses are stored content-addressed when raw_dir
is configured; CorpusStore remains the owner of normalized text revisions.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import random
import re
import socket
import sqlite3
import ssl
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .corpus import CorpusStore
from .parsers import parse_content

MAX_BODY_BYTES = 2_000_000
MAX_RESPONSE_TEXT = 2_000_000
MAX_FEED_ITEMS = 100
MAX_REDIRECTS = 2
MAX_TIMEOUT_SECONDS = 20
MAX_JSON_ITEMS = 100
MAX_ARTICLES = 10
MAX_PDF_PAGES = 100
USER_AGENT = "AlfaBusinessContext/1.0"


def _aware(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        try:
            result = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None


def _host(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.hostname or "").lower().rstrip(".")


def _safe_report_url(url: str) -> str:
    """Avoid reflecting credentials or sensitive query values from untrusted feed entries."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return ""


def _safe_error_code(value: str) -> str:
    """Allow only bounded machine codes into operator-facing reports."""
    if re.fullmatch(r"http_[1-5][0-9]{2}|[a-z][a-z0-9_]{0,63}", value):
        return value
    return "collection_failed"


def _https_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid HTTPS URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password or port not in (None, 443):
        raise ValueError("URL must be HTTPS on port 443 without credentials")
    sensitive_key = re.compile(r"(?:token|api[_-]?key|auth|signature|(?:^|[_-])sig(?:$|[_-])|password|secret|cookie|session)", re.I)
    if any(sensitive_key.search(key) for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("URL query credentials are not supported")
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class JSONAdapterMapping(FrozenContract):
    """Declarative dot-path mapping only; never a URL, expression, or executable adapter."""

    items: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=256)
    published_at: str = Field(min_length=1, max_length=256)
    publisher: str | None = Field(default=None, max_length=256)
    author: str | None = Field(default=None, max_length=256)
    event_time: str | None = Field(default=None, max_length=256)
    language: str | None = Field(default=None, max_length=256)

    @field_validator("items", "title", "content", "url", "published_at", "publisher", "author", "event_time", "language")
    @classmethod
    def safe_path(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", value):
            raise ValueError("JSON adapter mapping must use dot-separated object/list keys")
        return value


class SourceGrant(FrozenContract):
    source_id: str = Field(min_length=1, max_length=256)
    source_role: Literal["external", "bank"]
    # Set by Core as part of the authorized immutable grant. Never infer from
    # source HTML, feed content, user request, or a workflow's task label.
    data_classification: Literal["public", "synthetic", "confidential"] = "confidential"
    uri: str = Field(min_length=1, max_length=4096)
    content_type: Literal["application/rss+xml", "application/atom+xml", "text/html", "application/pdf", "application/json"]
    title: str = Field(default="", max_length=1000)
    publisher: str = Field(default="", max_length=1000)
    language: str = Field(default="", max_length=32)
    enabled: bool = True
    poll_interval_seconds: int = Field(default=3600, ge=60, le=31_536_000)
    timeout_seconds: int = Field(default=15, ge=1, le=MAX_TIMEOUT_SECONDS)
    max_bytes: int = Field(default=MAX_BODY_BYTES, ge=1, le=MAX_BODY_BYTES)
    allowed_redirect_hosts: tuple[str, ...] = Field(default=(), max_length=5)
    fetch_articles: bool = False
    article_hosts: tuple[str, ...] = Field(default=(), max_length=5)
    published_at: datetime | None = None
    event_time: datetime | None = None
    json_mapping: JSONAdapterMapping | None = None

    @field_validator("uri")
    @classmethod
    def valid_uri(cls, value: str) -> str:
        return _https_url(value)

    @field_validator("published_at", "event_time")
    @classmethod
    def aware_dates(cls, value: datetime | None) -> datetime | None:
        if value is not None and _aware(value) is None:
            raise ValueError("source dates must include timezone")
        return value

    @field_validator("allowed_redirect_hosts", "article_hosts")
    @classmethod
    def host_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.lower().rstrip(".") for value in values)
        if any(not host or any(char in host for char in "/:@?#") or ".." in host for host in normalized):
            raise ValueError("host allowlists must contain hostnames only")
        return normalized

    @model_validator(mode="after")
    def mapping_requirements(self) -> "SourceGrant":
        if self.fetch_articles and not self.article_hosts:
            raise ValueError("fetch_articles requires explicit article_hosts")
        if self.content_type == "application/json":
            if self.json_mapping is None:
                raise ValueError("JSON source requires items/title/content/url/published_at mapping paths")
        return self


class SourcePolicySnapshot(FrozenContract):
    """Effective tenant policy version issued by Core for a single job."""

    policy_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    tenant_id: str = Field(min_length=1, max_length=256)
    issued_at: datetime
    sources: tuple[SourceGrant, ...] = Field(default=(), max_length=1000)
    excluded_source_ids: tuple[str, ...] = ()
    excluded_publishers: tuple[str, ...] = ()
    user_excluded_source_ids: tuple[str, ...] = ()
    user_excluded_publishers: tuple[str, ...] = ()
    excluded_document_uris: tuple[str, ...] = ()
    user_excluded_document_uris: tuple[str, ...] = ()

    @field_validator("issued_at")
    @classmethod
    def issued_aware(cls, value: datetime) -> datetime:
        if _aware(value) is None:
            raise ValueError("issued_at must include timezone")
        return value

    @model_validator(mode="after")
    def unique_sources(self) -> "SourcePolicySnapshot":
        ids = [source.source_id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source_id must be unique in policy snapshot")
        return self

    def effective_sources(self) -> tuple[SourceGrant, ...]:
        # Personal hiding is a presentation preference. It must not silently
        # change shared collection or company analysis inputs.
        excluded_ids = set(self.excluded_source_ids)
        excluded_publishers = {value.casefold() for value in self.excluded_publishers}
        return tuple(source for source in self.sources if source.enabled
                     and source.source_id not in excluded_ids
                     and source.publisher.casefold() not in excluded_publishers)

    def effective_source_ids(self) -> list[str]:
        return [source.source_id for source in self.effective_sources()]

    def permits_document(self, document: dict[str, Any]) -> bool:
        if document.get("tenant_id") != self.tenant_id:
            return False
        source = next((item for item in self.sources if item.source_id == document.get("source_id") and item.enabled), None)
        if source is None:
            return False
        if document.get("source_role") is not None and document.get("source_role") != source.source_role:
            return False
        excluded_ids = set(self.excluded_source_ids) | set(self.user_excluded_source_ids)
        excluded_publishers = {value.casefold() for value in self.excluded_publishers}
        excluded_uris = set(self.excluded_document_uris)
        publisher = str(document.get("publisher") or source.publisher).casefold()
        canonical_uri = str(document.get("canonical_uri") or "")
        return source.source_id not in excluded_ids and publisher not in excluded_publishers and canonical_uri not in excluded_uris

    def is_personally_hidden(self, document: dict[str, Any]) -> bool:
        """Return personal-feed visibility without changing analytical eligibility."""
        if document.get("tenant_id") != self.tenant_id:
            return False
        source_id = str(document.get("source_id") or "")
        source = next((item for item in self.sources if item.source_id == source_id), None)
        publisher = str(document.get("publisher") or (source.publisher if source else "")).casefold()
        uri = str(document.get("canonical_uri") or "")
        return (source_id in self.user_excluded_source_ids
                or publisher in {value.casefold() for value in self.user_excluded_publishers}
                or uri in set(self.user_excluded_document_uris))


class SourceProbeRequest(FrozenContract):
    """One-shot Core grant to inspect a candidate URL; it never creates a source."""

    grant_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=256)
    candidate_url: str = Field(min_length=1, max_length=4096)
    expires_at: datetime
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=10)
    allowed_types: tuple[Literal["application/rss+xml", "application/atom+xml", "text/html", "application/pdf", "application/json"], ...] = Field(min_length=1)
    json_mapping: JSONAdapterMapping | None = None
    max_bytes: int = Field(default=MAX_BODY_BYTES, ge=1, le=MAX_BODY_BYTES)
    timeout_seconds: int = Field(default=15, ge=1, le=MAX_TIMEOUT_SECONDS)

    @field_validator("candidate_url")
    @classmethod
    def candidate_https(cls, value: str) -> str:
        return _https_url(value)

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        hosts = tuple(value.lower().rstrip(".") for value in values)
        if any(not host or any(char in host for char in "/:@?#") or ".." in host for host in hosts):
            raise ValueError("allowed_hosts must contain hostnames only")
        return hosts

    @field_validator("expires_at")
    @classmethod
    def expiration_aware(cls, value: datetime) -> datetime:
        if _aware(value) is None:
            raise ValueError("expires_at must include timezone")
        return value


class SourceProbeResult(FrozenContract):
    grant_id: str
    status: Literal["ready", "needs_metadata", "unsupported", "error"]
    requested_url: str
    final_url: str | None = None
    detected_type: str | None = None
    title: str | None = None
    publisher: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    event_time: datetime | None = None
    language: str | None = None
    missing_fields: tuple[str, ...] = ()
    preview_text: str = Field(default="", max_length=4000)
    raw_sha256: str | None = None
    error_code: str | None = None


class FetchResult(BaseModel):
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""


class CollectionDocument(FrozenContract):
    tenant_id: str
    source_id: str
    source_role: Literal["external", "bank"]
    data_classification: Literal["public", "synthetic", "confidential"] = "confidential"
    canonical_uri: str
    title: str
    content: str
    content_type: str
    published_at: datetime
    fetched_at: datetime
    synthetic: bool = False
    publisher: str = ""
    author: str = ""
    event_time: datetime | None = None
    language: str = "ru"
    document_revision_ref: str
    content_hash: str
    raw_sha256: str
    raw_ref: str


def _public_addresses(host: str) -> list[str]:
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("source DNS lookup failed") from exc
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses:
        raise ValueError("source DNS returned no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("source resolves to a non-public IP address")
    return addresses


class _PinnedHTTPSConnection:
    """TLS-verifying HTTPS GET with a validated, pinned DNS answer and no implicit redirects."""

    @staticmethod
    def get(url: str, headers: dict[str, str], *, timeout: int = 15, max_bytes: int = MAX_BODY_BYTES) -> FetchResult:
        from http.client import HTTPSConnection, HTTPException

        url = _https_url(url)
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        addresses = _public_addresses(host)
        context = ssl.create_default_context()
        last_error: Exception | None = None
        host_header = f"[{host}]" if ":" in host else host
        safe_headers: dict[str, str] = {}
        for key, value in headers.items():
            normalized = key.casefold()
            if normalized not in {"if-none-match", "if-modified-since"}:
                continue
            text = str(value)
            if len(text) <= 512 and not any(ch in text for ch in "\r\n\x00"):
                safe_headers["If-None-Match" if normalized == "if-none-match" else "If-Modified-Since"] = text
        for address in addresses:
            class PinnedConnection(HTTPSConnection):
                def connect(self) -> None:
                    raw = socket.create_connection((address, 443), timeout=timeout)
                    self.sock = context.wrap_socket(raw, server_hostname=host)
            connection = PinnedConnection(host, 443, timeout=timeout, context=context)
            try:
                path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                connection.request("GET", path, headers={"Host": host_header, "User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/json, text/html, application/pdf;q=0.9", "Accept-Encoding": "identity", **safe_headers})
                response = connection.getresponse()
                response_headers = {key.lower(): value for key, value in response.getheaders()}
                if response.status == 304:
                    body = b""
                else:
                    length = response_headers.get("content-length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise ValueError("response_too_large")
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise ValueError("response_too_large")
                return FetchResult(status=response.status, headers=response_headers, body=body)
            except (OSError, HTTPException, ssl.SSLError) as exc:
                last_error = exc
            finally:
                connection.close()
        raise ValueError("https_request_failed") from last_error


class _HTMLMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.metadata: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = values.get("property") or values.get("name") or values.get("itemprop")
            if key and values.get("content"):
                self.metadata[key.lower()] = values["content"].strip()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data.strip())


def _metadata_datetime(value: str | None) -> datetime | None:
    return _aware(value)


def _nested(value: Any, path: str) -> Any:
    """Read a dot path from JSON; this mapping cannot execute expressions or URLs."""
    current = value
    for part in path.split("."):
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


class NewsCollector:
    def __init__(self, store: CorpusStore, *, state_path: str | Path, fetcher: Any = None, raw_dir: str | Path | None = None):
        self.store = store
        self.state_path = str(state_path)
        self.raw_dir = Path(raw_dir) if raw_dir is not None else Path(self.state_path).parent / "raw"
        Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
        if self.raw_dir is not None:
            self.raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                self.raw_dir.chmod(0o700)
        self.fetcher = fetcher or _PinnedHTTPSConnection.get
        # Serialize additive schema migration across workers starting together.
        with sqlite3.connect(self.state_path, timeout=10, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""CREATE TABLE IF NOT EXISTS poll_state (
              tenant_id TEXT NOT NULL, source_id TEXT NOT NULL, next_poll TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, etag TEXT, last_modified TEXT,
              last_error TEXT, last_status INTEGER, lease_until TEXT, grant_fingerprint TEXT,
              PRIMARY KEY(tenant_id,source_id))""")
                columns = {row[1] for row in db.execute("PRAGMA table_info(poll_state)")}
                if "lease_until" not in columns:
                    db.execute("ALTER TABLE poll_state ADD COLUMN lease_until TEXT")
                if "grant_fingerprint" not in columns:
                    db.execute("ALTER TABLE poll_state ADD COLUMN grant_fingerprint TEXT")
                db.execute("""CREATE TABLE IF NOT EXISTS raw_receipts (
              tenant_id TEXT NOT NULL, source_id TEXT NOT NULL, canonical_uri TEXT NOT NULL,
              raw_sha256 TEXT NOT NULL, raw_ref TEXT NOT NULL, content_type TEXT NOT NULL,
              title TEXT, publisher TEXT, author TEXT, published_at TEXT, event_time TEXT,
              language TEXT, document_revision_ref TEXT, fetched_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              PRIMARY KEY(tenant_id,source_id,canonical_uri,raw_sha256))""")
                db.commit()
            except Exception:
                db.rollback()
                raise

    def collect(self, snapshot: SourcePolicySnapshot | dict[str, Any], *, now: datetime | None = None, source_ids: list[str] | tuple[str, ...] | None = None, force: bool = False) -> dict[str, Any]:
        snapshot = snapshot if isinstance(snapshot, SourcePolicySnapshot) else SourcePolicySnapshot.model_validate(snapshot)
        now = _aware(now or datetime.now(timezone.utc))
        if now is None:
            raise ValueError("now must be timezone-aware")
        selected = snapshot.effective_sources()
        if source_ids is not None:
            requested = set(source_ids)
            allowed = {source.source_id for source in selected}
            if requested - allowed:
                raise ValueError("source_ids contain sources outside effective policy")
            selected = tuple(source for source in selected if source.source_id in requested)
        report: dict[str, Any] = {"policy_id": snapshot.policy_id, "policy_revision": snapshot.revision, "tenant_id": snapshot.tenant_id, "checked_at": _iso(now), "fetched": 0, "unchanged": 0, "skipped": [], "pending_metadata": [], "errors": [], "documents": []}
        for source in selected:
            outcome = self._collect_source(snapshot, source, now, force)
            report["fetched"] += int(outcome.pop("fetched", 0))
            report["unchanged"] += int(outcome.pop("unchanged", 0))
            for key in ("skipped", "pending_metadata", "errors", "documents"):
                report[key].extend(outcome.get(key, []))
        report["sources_considered"] = len(selected)
        return report

    def probe(self, request: SourceProbeRequest | dict[str, Any], *, now: datetime | None = None) -> SourceProbeResult:
        request = request if isinstance(request, SourceProbeRequest) else SourceProbeRequest.model_validate(request)
        now = _aware(now or datetime.now(timezone.utc))
        if now is None or request.expires_at.astimezone(timezone.utc) <= now:
            return SourceProbeResult(grant_id=request.grant_id, status="error", requested_url=_safe_report_url(request.candidate_url), error_code="probe_grant_expired")
        url = _https_url(request.candidate_url)
        if _host(url) not in request.allowed_hosts:
            return SourceProbeResult(grant_id=request.grant_id, status="error", requested_url=_safe_report_url(url), error_code="probe_host_not_allowed")
        try:
            response = self._request(url, {}, allowed_hosts=set(request.allowed_hosts), max_redirects=MAX_REDIRECTS, timeout=request.timeout_seconds, max_bytes=request.max_bytes)
            if response.status != 200:
                return SourceProbeResult(grant_id=request.grant_id, status="error", requested_url=_safe_report_url(url), final_url=_safe_report_url(response.url), error_code=f"http_{response.status}")
            detected = self._detect_type(response.headers.get("content-type", ""), response.body)
            if detected not in request.allowed_types:
                return SourceProbeResult(grant_id=request.grant_id, status="unsupported", requested_url=_safe_report_url(url), final_url=_safe_report_url(response.url), detected_type=detected, raw_sha256=_sha(response.body), error_code="content_type_not_allowed")
            info = self._inspect_payload(detected, response.body, request.json_mapping)
            if detected in {"application/rss+xml", "application/atom+xml"}:
                info = {**info, "missing_fields": ["publisher"] if not info.get("publisher") else []}
            missing = tuple(info.get("missing_fields", ()))
            return SourceProbeResult(grant_id=request.grant_id, status="needs_metadata" if missing else "ready", requested_url=_safe_report_url(url), final_url=_safe_report_url(response.url), detected_type=detected, title=info.get("title"), publisher=info.get("publisher"), author=info.get("author"), published_at=info.get("published_at"), event_time=info.get("event_time"), language=info.get("language"), missing_fields=missing, preview_text=info.get("preview_text", "")[:4000], raw_sha256=_sha(response.body))
        except ValueError as exc:
            return SourceProbeResult(grant_id=request.grant_id, status="error", requested_url=_safe_report_url(url), error_code=_safe_error_code(str(exc)))
        except Exception:
            return SourceProbeResult(grant_id=request.grant_id, status="error", requested_url=_safe_report_url(url), error_code="probe_failed")

    def _collect_source(self, snapshot: SourcePolicySnapshot, source: SourceGrant, now: datetime, force: bool) -> dict[str, Any]:
        outcome: dict[str, Any] = {"fetched": 0, "unchanged": 0, "skipped": [], "pending_metadata": [], "errors": [], "documents": []}
        if source.content_type not in {"application/rss+xml", "application/atom+xml", "application/json"} and not snapshot.permits_document({
                "tenant_id": snapshot.tenant_id, "source_id": source.source_id,
                "publisher": source.publisher or _host(source.uri), "canonical_uri": source.uri}):
            outcome["skipped"].append({"source_id": source.source_id, "reason": "policy_excluded_document"})
            return outcome
        state, reason = self._claim_poll(snapshot.tenant_id, source, now, force=force)
        if reason:
            outcome["skipped"].append({"source_id": source.source_id, "reason": reason,
                                       "next_poll": state.get("next_poll") if state else None})
            return outcome
        conditional = {}
        if state and state["etag"]:
            conditional["If-None-Match"] = state["etag"]
        if state and state["last_modified"]:
            conditional["If-Modified-Since"] = state["last_modified"]
        try:
            response = self._request(source.uri, conditional, allowed_hosts={_host(source.uri), *source.allowed_redirect_hosts}, max_redirects=MAX_REDIRECTS, timeout=source.timeout_seconds, max_bytes=source.max_bytes)
            if response.status == 304:
                outcome["unchanged"] = 1
                self._save_state(snapshot.tenant_id, source, now + timedelta(seconds=source.poll_interval_seconds), state, response, None, success=True)
                return outcome
            if response.status == 429 or response.status >= 500:
                raise _RetryableFetchError(f"http_{response.status}", response)
            if response.status != 200:
                raise ValueError(f"http_{response.status}")
            detected = self._detect_type(response.content_type, response.body)
            compatible_types = {source.content_type}
            if source.content_type in {"application/rss+xml", "application/atom+xml"}:
                compatible_types = {"application/rss+xml", "application/atom+xml"}
            if detected not in compatible_types:
                raise ValueError("configured_content_type_mismatch")
            raw_hash = _sha(response.body)
            if source.content_type in {"application/rss+xml", "application/atom+xml"}:
                raw_ref = self._save_raw(snapshot.tenant_id, raw_hash, response.body)
                docs, pending = self._ingest_feed(snapshot, source, response.body, raw_hash, raw_ref, now)
                outcome["documents"].extend(docs)
                outcome["pending_metadata"].extend(pending)
                if source.fetch_articles:
                    article_docs, article_pending = self._fetch_feed_articles(snapshot, source, response.body, now)
                    outcome["documents"].extend(article_docs)
                    outcome["pending_metadata"].extend(article_pending)
            elif source.content_type == "application/json":
                raw_ref = self._save_raw(snapshot.tenant_id, raw_hash, response.body)
                docs, pending = self._ingest_json(snapshot, source, response.body, raw_hash, raw_ref, now)
                outcome["documents"].extend(docs)
                outcome["pending_metadata"].extend(pending)
            else:
                info = self._inspect_payload(source.content_type, response.body, source.json_mapping)
                if not info.get("title") and source.title:
                    info = {**info, "title": source.title}
                publisher = info.get("publisher") or source.publisher or _host(response.url)
                if not snapshot.permits_document({"tenant_id": snapshot.tenant_id,
                        "source_id": source.source_id, "publisher": publisher,
                        "canonical_uri": response.url}):
                    outcome["fetched"] = 1
                    self._save_state(snapshot.tenant_id, source,
                        now + timedelta(seconds=source.poll_interval_seconds), state,
                        response, None, success=True)
                    return outcome
                raw_ref = self._save_raw(snapshot.tenant_id, raw_hash, response.body)
                published = _aware(info.get("published_at")) or _aware(source.published_at)
                missing = [field for field, value in (("title", info.get("title")), ("published_at", published)) if not value]
                if missing:
                    outcome["pending_metadata"].append({"source_id": source.source_id, "url": _safe_report_url(response.url), "raw_sha256": raw_hash, "missing_fields": missing})
                    self._record_receipt(snapshot.tenant_id, source.source_id, response.url, raw_hash, raw_ref, source.content_type, info, None, now)
                else:
                    outcome["documents"].append(self._ingest_document(snapshot, source, response.url, info, response.body, response.content_type or source.content_type, published, _aware(info.get("event_time")) or _aware(source.event_time), info.get("author") or "", publisher, raw_hash, raw_ref, now))
            outcome["fetched"] = 1
            self._save_state(snapshot.tenant_id, source, now + timedelta(seconds=source.poll_interval_seconds), state, response, None, success=True)
            return outcome
        except _RetryableFetchError as exc:
            retry = self._backoff(source, state, now, exc.response.headers.get("retry-after"))
            message = str(exc)
            self._save_state(snapshot.tenant_id, source, retry, state, exc.response, message, success=False)
            outcome["errors"].append({"source_id": source.source_id, "error": message, "retry_at": _iso(retry)})
        except Exception as exc:
            message = _safe_error_code(str(exc)) if isinstance(exc, ValueError) else "collection_failed"
            retry = self._backoff(source, state, now, None)
            self._save_state(snapshot.tenant_id, source, retry, state, None, message, success=False)
            outcome["errors"].append({"source_id": source.source_id, "error": message, "retry_at": _iso(retry)})
        return outcome

    def _request(self, url: str, headers: dict[str, str], *, allowed_hosts: set[str], max_redirects: int, timeout: int, max_bytes: int) -> "_Response":
        current = _https_url(url)
        conditional_headers = {}
        for key, value in headers.items():
            normalized = key.casefold()
            if normalized in {"if-none-match", "if-modified-since"} and len(str(value)) <= 512 and not any(ch in str(value) for ch in "\r\n\x00"):
                conditional_headers["If-None-Match" if normalized == "if-none-match" else "If-Modified-Since"] = str(value)
        headers = conditional_headers
        for redirect_count in range(max_redirects + 1):
            if _host(current) not in allowed_hosts:
                raise ValueError("redirect_host_not_allowed")
            response = self.fetcher(current, headers, timeout=timeout, max_bytes=max_bytes)
            if not isinstance(response, FetchResult):
                response = FetchResult.model_validate(response)
            response.headers = {str(key).casefold(): str(value) for key, value in response.headers.items()}
            if len(response.body) > max_bytes:
                raise ValueError("response_too_large")
            if response.status not in {301, 302, 303, 307, 308}:
                return _Response(response.status, response.headers, response.body, current)
            location = response.headers.get("location")
            if not location or redirect_count >= max_redirects:
                raise ValueError("redirect_limit")
            current = _https_url(urllib.parse.urljoin(current, location))
            headers = {}  # conditional validators apply only to the configured resource
        raise ValueError("redirect_limit")

    @staticmethod
    def _detect_type(header: str, body: bytes) -> str:
        mime = header.split(";", 1)[0].strip().lower()
        if body.startswith(b"%PDF-") or mime == "application/pdf":
            return "application/pdf"
        sample = body[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
        if b"<feed" in sample or b"<entry" in sample:
            return "application/atom+xml"
        if b"<rss" in sample or b"<channel" in sample or b"<item" in sample:
            return "application/rss+xml"
        if mime in {"application/rss+xml", "application/atom+xml", "text/html", "application/json"}:
            return mime
        if sample.startswith((b"{", b"[")):
            return "application/json"
        if b"<html" in sample or b"<!doctype html" in sample:
            return "text/html"
        return mime or "application/octet-stream"

    def _inspect_payload(self, content_type: str, body: bytes, mapping: dict[str, str] | None = None) -> dict[str, Any]:
        if content_type == "application/pdf":
            parsed = parse_content(base64.b64encode(body).decode("ascii"), "application/pdf")
            return {"title": None, "publisher": None, "author": None, "published_at": None, "event_time": None, "language": None, "preview_text": parsed.text[:4000], "content": parsed.text, "content_type": "application/pdf", "missing_fields": ["title", "publisher", "author", "published_at"]}
        text = body.decode("utf-8-sig", errors="replace")
        if content_type == "text/html":
            parser = _HTMLMetadata(); parser.feed(text); parser.close()
            parsed = parse_content(text, "text/html")
            m = parser.metadata
            title = m.get("og:title") or m.get("twitter:title") or " ".join(x for x in parser.title_parts if x)
            publisher = m.get("og:site_name") or m.get("publisher")
            author = m.get("author") or m.get("article:author")
            published = _metadata_datetime(m.get("article:published_time") or m.get("datepublished") or m.get("date"))
            event_time = _metadata_datetime(m.get("event:start_time") or m.get("eventdate"))
            missing = [field for field, value in (("title", title), ("publisher", publisher), ("published_at", published)) if not value]
            return {"title": title or None, "publisher": publisher or None, "author": author or None, "published_at": published, "event_time": event_time, "language": m.get("og:locale"), "preview_text": parsed.text[:4000], "content": parsed.text, "content_type": "text/html", "missing_fields": missing}
        if content_type in {"application/rss+xml", "application/atom+xml"}:
            parsed = parse_content(text, "application/rss+xml")
            from defusedxml import ElementTree as SafeET
            root = SafeET.fromstring(text)
            publisher = ""
            feed_title = ""
            for node in root.iter():
                local = node.tag.rsplit("}", 1)[-1].lower()
                if local in {"channel", "feed"}:
                    values = [child for child in node if child.tag.rsplit("}", 1)[-1].lower() == "title"]
                    if values and values[0].text:
                        publisher = values[0].text.strip()
                        feed_title = publisher
                    break
            missing = ["publisher"] if not publisher else []
            return {"title": feed_title or (parsed.items[0].title if parsed.items else None), "publisher": publisher or None, "author": None, "published_at": None, "event_time": None, "language": None, "preview_text": "\n".join(item.text for item in parsed.items[:5])[:4000], "content": text, "content_type": content_type, "missing_fields": missing}
        if content_type == "application/json":
            return self._inspect_json(text, mapping)
        raise ValueError("unsupported_content_type")

    def _inspect_json(self, text: str, mapping: dict[str, str] | None) -> dict[str, Any]:
        if not mapping:
            raise ValueError("json_adapter_mapping_required")
        if isinstance(mapping, JSONAdapterMapping):
            mapping = mapping.model_dump(exclude_none=True)
        payload = json.loads(text)
        rows = _nested(payload, mapping["items"])
        if not isinstance(rows, list):
            raise ValueError("json_adapter_items_not_array")
        first = rows[0] if rows else {}
        title = _nested(first, mapping["title"])
        content = _nested(first, mapping["content"])
        published = _aware(_nested(first, mapping["published_at"]))
        return {"title": str(title) if title else None, "publisher": str(_nested(first, mapping.get("publisher", "")) or "") or None, "author": str(_nested(first, mapping.get("author", "")) or "") or None, "published_at": published, "event_time": _aware(_nested(first, mapping.get("event_time", ""))), "language": str(_nested(first, mapping.get("language", "")) or "") or None, "preview_text": str(content or "")[:4000], "content": text, "content_type": "application/json", "missing_fields": [field for field, value in (("title", title), ("published_at", published), ("items", rows)) if not value]}

    def _ingest_feed(self, snapshot: SourcePolicySnapshot, source: SourceGrant, raw: bytes, raw_hash: str, raw_ref: str, now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from defusedxml import ElementTree as SafeET

        xml = raw.decode("utf-8-sig", errors="replace")
        parsed = parse_content(xml, "application/rss+xml")
        root = SafeET.fromstring(xml)
        publisher = source.publisher
        authors: dict[str, str] = {}
        event_times: dict[str, datetime] = {}
        feed_language = root.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") or root.attrib.get("lang") or ""
        for node in root.iter():
            local = node.tag.rsplit("}", 1)[-1].lower()
            if local == "channel" and not publisher:
                publisher = next((child.text.strip() for child in node if child.tag.rsplit("}", 1)[-1].lower() == "title" and child.text and child.text.strip()), "")
            if local in {"channel", "feed"}:
                feed_language = node.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") or node.attrib.get("lang") or feed_language
            if local not in {"item", "entry"}:
                continue
            values = {child.tag.rsplit("}", 1)[-1].lower(): (" ".join(child.itertext()).strip() or child.attrib.get("href", "")).strip() for child in node}
            uri = values.get("link") or values.get("id") or values.get("guid")
            author = values.get("creator") or values.get("author") or values.get("name") or ""
            if uri and author:
                authors[uri] = author[:1000]
            event_time = _aware(values.get("event_time") or values.get("eventtime") or values.get("eventdate") or values.get("start_time"))
            if uri and event_time:
                event_times[uri] = event_time
        docs, pending = [], []
        for item in parsed.items[:MAX_FEED_ITEMS]:
            published = _aware(item.published_at)
            if published is None:
                pending.append({"source_id": source.source_id, "url": _safe_report_url(item.uri) if item.uri else None, "title": item.title or None, "raw_sha256": raw_hash, "missing_fields": ["published_at"]})
                self._record_receipt(snapshot.tenant_id, source.source_id, item.uri or source.uri, raw_hash, raw_ref, source.content_type, {"title": item.title or None, "publisher": source.publisher, "content": item.text}, None, now)
                continue
            if not item.text.strip() or not item.title.strip():
                missing = [field for field, present in (("title", bool(item.title.strip())), ("content", bool(item.text.strip()))) if not present]
                pending.append({"source_id": source.source_id, "url": _safe_report_url(item.uri) if item.uri else None, "title": item.title or None, "raw_sha256": raw_hash, "missing_fields": missing})
                self._record_receipt(snapshot.tenant_id, source.source_id, item.uri or source.uri, raw_hash, raw_ref, source.content_type, {"title": item.title or None, "publisher": source.publisher, "content": item.text}, None, now)
                continue
            uri = item.uri.strip() if item.uri else source.uri + "#item-" + hashlib.sha256((item.title + item.text).encode()).hexdigest()[:20]
            if item.uri:
                try:
                    item_url = _https_url(item.uri)
                except ValueError:
                    pending.append({"source_id": source.source_id, "url": _safe_report_url(item.uri), "title": item.title or None, "raw_sha256": raw_hash, "missing_fields": ["valid_article_url"]})
                    self._record_receipt(snapshot.tenant_id, source.source_id, source.uri, raw_hash, raw_ref, source.content_type, {"title": item.title, "publisher": publisher, "content": item.text}, None, now)
                    continue
                if _host(item_url) not in {_host(source.uri), *source.article_hosts, *source.allowed_redirect_hosts}:
                    pending.append({"source_id": source.source_id, "url": _safe_report_url(item.uri), "title": item.title or None, "raw_sha256": raw_hash, "missing_fields": ["allowed_article_host"]})
                    self._record_receipt(snapshot.tenant_id, source.source_id, item_url, raw_hash, raw_ref, source.content_type, {"title": item.title, "publisher": publisher, "content": item.text}, None, now)
                    continue
                uri = item_url
            if not snapshot.permits_document({"tenant_id": snapshot.tenant_id, "source_id": source.source_id, "publisher": publisher or source.publisher, "canonical_uri": uri}):
                continue
            info = {"title": item.title, "content": item.text[:200_000], "content_type": "text/plain", "language": feed_language or source.language}
            docs.append(self._ingest_document(snapshot, source, uri, info, raw, source.content_type, published, event_times.get(item.uri or "") or _aware(source.event_time), authors.get(item.uri or "", ""), publisher, raw_hash, raw_ref, now))
        return docs, pending

    def _ingest_json(self, snapshot: SourcePolicySnapshot, source: SourceGrant, raw: bytes, raw_hash: str, raw_ref: str, now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        payload = json.loads(raw.decode("utf-8-sig", errors="replace"))
        mapping = source.json_mapping or {}
        if isinstance(mapping, JSONAdapterMapping):
            mapping = mapping.model_dump(exclude_none=True)
        rows = _nested(payload, mapping.get("items", ""))
        if not isinstance(rows, list):
            raise ValueError("json_adapter_items_not_array")
        docs, pending = [], []
        for row in rows[:MAX_JSON_ITEMS]:
            title = _nested(row, mapping["title"])
            content = _nested(row, mapping["content"])
            uri = _nested(row, mapping["url"])
            published = _aware(_nested(row, mapping["published_at"]))
            if not title or not content or not published or not uri:
                pending.append({"source_id": source.source_id, "url": _safe_report_url(str(uri)) if uri else source.uri, "title": str(title)[:1000] if title else None, "raw_sha256": raw_hash, "missing_fields": [name for name, value in (("title", title), ("content", content), ("published_at", published), ("url", uri)) if not value]})
                self._record_receipt(snapshot.tenant_id, source.source_id, source.uri, raw_hash, raw_ref, "application/json", {"title": title, "publisher": source.publisher, "author": "", "published_at": published, "event_time": None, "language": source.language, "content": content}, None, now)
                continue
            try:
                document_url = _https_url(str(uri)) if uri else source.uri
            except ValueError:
                pending.append({"source_id": source.source_id, "url": source.uri, "title": str(title)[:1000], "raw_sha256": raw_hash, "missing_fields": ["valid_article_url"]})
                self._record_receipt(snapshot.tenant_id, source.source_id, source.uri, raw_hash, raw_ref, "application/json", {"title": title, "publisher": source.publisher, "content": content}, None, now)
                continue
            if _host(document_url) not in {_host(source.uri), *source.article_hosts}:
                pending.append({"source_id": source.source_id, "url": _safe_report_url(document_url), "title": str(title)[:1000], "raw_sha256": raw_hash, "missing_fields": ["allowed_article_host"]})
                self._record_receipt(snapshot.tenant_id, source.source_id, document_url, raw_hash, raw_ref, "application/json", {"title": title, "publisher": source.publisher, "content": content}, None, now)
                continue
            if not snapshot.permits_document({"tenant_id": snapshot.tenant_id, "source_id": source.source_id, "publisher": str(_nested(row, mapping.get("publisher", "")) or source.publisher), "canonical_uri": document_url}):
                continue
            info = {"title": str(title)[:1000], "content": str(content)[:MAX_RESPONSE_TEXT], "content_type": "text/plain"}
            docs.append(self._ingest_document(snapshot, source, document_url, info, raw, "application/json", published, _aware(_nested(row, mapping.get("event_time", ""))) or _aware(source.event_time), str(_nested(row, mapping.get("author", "")) or ""), str(_nested(row, mapping.get("publisher", "")) or source.publisher), raw_hash, raw_ref, now))
        return docs, pending

    def _fetch_feed_articles(self, snapshot: SourcePolicySnapshot, source: SourceGrant, feed_raw: bytes, now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        parsed = parse_content(feed_raw.decode("utf-8-sig", errors="replace"), "application/rss+xml")
        docs, pending = [], []
        allowed = set(source.article_hosts)
        for item in parsed.items:
            if len(docs) + len(pending) >= MAX_ARTICLES:
                break
            if not item.uri or _host(item.uri) not in allowed:
                continue
            published = _aware(item.published_at)
            if published is None:
                pending.append({"source_id": source.source_id, "url": _safe_report_url(item.uri), "title": item.title or None, "missing_fields": ["published_at"]})
                continue
            try:
                url = _https_url(item.uri)
                if not snapshot.permits_document({"tenant_id": snapshot.tenant_id, "source_id": source.source_id, "publisher": source.publisher, "canonical_uri": url}):
                    continue
                article = self._request(url, {}, allowed_hosts={_host(url), *allowed, *source.allowed_redirect_hosts}, max_redirects=MAX_REDIRECTS, timeout=source.timeout_seconds, max_bytes=source.max_bytes)
                if article.status != 200:
                    continue
                kind = self._detect_type(article.content_type, article.body)
                if kind != "text/html":
                    continue
                info = self._inspect_payload(kind, article.body)
                published = _aware(info.get("published_at")) or published
                digest = _sha(article.body)
                reference = self._save_raw(snapshot.tenant_id, digest, article.body)
                docs.append(self._ingest_document(snapshot, source, article.url, info, article.body, kind, published, _aware(source.event_time), info.get("author") or "", info.get("publisher") or source.publisher, digest, reference, now))
            except Exception:
                    pending.append({"source_id": source.source_id, "url": _safe_report_url(item.uri), "title": item.title or None, "missing_fields": ["article_fetch_failed"]})
        return docs, pending

    def _ingest_document(self, snapshot: SourcePolicySnapshot, source: SourceGrant, uri: str, info: dict[str, Any], original: bytes, content_type: str, published: datetime, event_time: datetime | None, author: str, publisher: str, raw_hash: str, raw_ref: str, now: datetime) -> dict[str, Any]:
        ingest_content = info.get("content") or ""
        ingest_type = info.get("content_type") or content_type
        if content_type == "application/pdf":
            ingest_content = base64.b64encode(original).decode("ascii")
            ingest_type = "application/pdf"
        existing = self.store.list_documents(tenant_id=snapshot.tenant_id,
            source_ids=[source.source_id], as_of=_iso(now), policy=snapshot)
        prior = next((item for item in existing if item.get("canonical_uri") == uri), None)
        classification = source.data_classification
        if prior:
            previous_class = prior.get("data_classification", "confidential")
            # A newer grant can restrict an existing document, but cannot make
            # confidential content public or relabel a synthetic document as public.
            if previous_class == "confidential" or classification == "confidential":
                classification = "confidential"
            elif previous_class == "synthetic" or classification == "synthetic":
                classification = "synthetic" if previous_class == "synthetic" else "public"
        synthetic = bool(prior.get("synthetic")) if prior else classification == "synthetic"
        if synthetic:
            classification = "synthetic"
        result = self.store.ingest({"tenant_id": snapshot.tenant_id, "source_id": source.source_id, "source_role": source.source_role, "data_classification": classification, "canonical_uri": uri, "title": (info.get("title") or source.title or source.source_id)[:1000], "publisher": publisher[:1000], "author": author[:1000], "event_time": _iso(event_time), "raw_sha256": raw_hash, "raw_ref": raw_ref, "language": (info.get("language") or source.language)[:32], "content": ingest_content, "content_type": ingest_type, "published_at": _iso(published), "fetched_at": _iso(now), "synthetic": synthetic})
        doc_ref = result.get("document_revision_ref")
        if not doc_ref and result.get("items"):
            doc_ref = result["items"][0].get("document_revision_ref")
        if not doc_ref:
            # An empty or duplicate-free feed does not generate an ingestion receipt.
            return {"tenant_id": snapshot.tenant_id, "source_id": source.source_id, "source_role": source.source_role, "data_classification": source.data_classification, "canonical_uri": uri, "title": info.get("title") or source.title or source.source_id, "content": info.get("content") or "", "content_type": content_type, "published_at": _iso(published), "fetched_at": _iso(now), "synthetic": source.data_classification == "synthetic", "publisher": publisher, "author": author, "event_time": _iso(event_time), "language": info.get("language") or source.language, "document_revision_ref": None, "content_hash": result.get("content_hash"), "raw_sha256": raw_hash, "raw_ref": raw_ref, "duplicate_of": result.get("duplicate_of")}
        # Corpus classification is immutable per normalized revision. A changed
        # grant may trigger a fresh fetch, but cannot retroactively promote an
        # already stored confidential revision to public/synthetic.
        stored = self.store.list_documents(tenant_id=snapshot.tenant_id,
            source_ids=[source.source_id], as_of=_iso(now), policy=snapshot)
        persisted = next((item for item in stored if item.get("document_revision_ref") == doc_ref), None)
        persisted_classification = (persisted or {}).get("data_classification", source.data_classification)
        parsed = parse_content(ingest_content if isinstance(ingest_content, str) else "", ingest_type)
        normalized = parsed.text
        self._record_receipt(snapshot.tenant_id, source.source_id, uri, raw_hash, raw_ref, content_type, {**info, "title": info.get("title") or source.title or source.source_id, "publisher": publisher, "author": author, "published_at": published, "event_time": event_time, "language": info.get("language") or source.language, "data_classification": persisted_classification}, doc_ref, now)
        return {"tenant_id": snapshot.tenant_id, "source_id": source.source_id, "source_role": source.source_role, "data_classification": persisted_classification, "canonical_uri": uri, "title": (info.get("title") or source.title or source.source_id)[:1000], "content": normalized[:MAX_RESPONSE_TEXT], "content_type": content_type, "published_at": _iso(published), "fetched_at": _iso(now), "synthetic": persisted_classification == "synthetic", "publisher": publisher[:1000], "author": author[:1000], "event_time": _iso(event_time), "language": info.get("language") or source.language, "document_revision_ref": doc_ref, "content_hash": result.get("content_hash"), "raw_sha256": raw_hash, "raw_ref": raw_ref, "duplicate_of": result.get("duplicate_of")}

    def _record_receipt(self, tenant_id: str, source_id: str, uri: str, raw_hash: str, raw_ref: str, content_type: str, info: dict[str, Any], revision_ref: str | None, fetched_at: datetime) -> None:
        metadata = {key: value for key, value in info.items() if key not in {"content", "preview_text"}}
        with sqlite3.connect(self.state_path) as db:
            db.execute("INSERT OR IGNORE INTO raw_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (tenant_id, source_id, uri, raw_hash, raw_ref, content_type, str(info.get("title") or "") or None, str(info.get("publisher") or "") or None, str(info.get("author") or "") or None, _iso(_aware(info.get("published_at"))), _iso(_aware(info.get("event_time"))), str(info.get("language") or "") or None, revision_ref, _iso(fetched_at), json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)))

    def _save_raw(self, tenant_id: str, digest: str, body: bytes) -> str:
        if self.raw_dir is None:
            return "sha256:" + digest
        tenant_component = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        target = self.raw_dir / "tenants" / tenant_component / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            for directory in (self.raw_dir / "tenants", self.raw_dir / "tenants" / tenant_component, target.parent):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
        if not target.exists():
            temp = target.with_name(target.name + ".tmp-" + str(os.getpid()) + "-" + str(random.randrange(1 << 30)))
            try:
                with temp.open("xb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    temp.replace(target)
                except FileExistsError:
                    temp.unlink(missing_ok=True)
            finally:
                temp.unlink(missing_ok=True)
        if os.name == "posix":
            target.chmod(0o600)
        return str(target)

    def _state(self, tenant_id: str, source_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.state_path) as db:
            row = db.execute("SELECT next_poll,attempts,etag,last_modified,last_error,last_status,lease_until,grant_fingerprint FROM poll_state WHERE tenant_id=? AND source_id=?", (tenant_id, source_id)).fetchone()
        return dict(zip(("next_poll", "attempts", "etag", "last_modified", "last_error", "last_status", "lease_until", "grant_fingerprint"), row)) if row else None

    @staticmethod
    def _grant_fingerprint(source: SourceGrant) -> str:
        """Identity of the effective source grant, excluding snapshot version."""
        serialized = json.dumps(source.model_dump(mode="json"), ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _claim_poll(self, tenant_id: str, source: SourceGrant, now: datetime, *, force: bool) -> tuple[dict[str, Any] | None, str | None]:
        """Atomically reserve a source poll across processes before any network I/O."""
        fingerprint = self._grant_fingerprint(source)
        with sqlite3.connect(self.state_path, timeout=10, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT next_poll,attempts,etag,last_modified,last_error,last_status,lease_until,grant_fingerprint FROM poll_state WHERE tenant_id=? AND source_id=?", (tenant_id, source.source_id)).fetchone()
            state = dict(zip(("next_poll", "attempts", "etag", "last_modified", "last_error", "last_status", "lease_until", "grant_fingerprint"), row)) if row else None
            if state and state.get("lease_until") and (_aware(state["lease_until"]) or now) > now:
                db.rollback()
                return state, "already_running"
            if state and state.get("grant_fingerprint") != fingerprint:
                # URI, parser/adapter, permission classification or limits changed.
                # Drop validators and schedule state so the new grant is fetched now.
                state.update(next_poll=_iso(now), attempts=0, etag=None, last_modified=None,
                             last_error=None, last_status=None, grant_fingerprint=fingerprint)
                db.execute("UPDATE poll_state SET next_poll=?,attempts=0,etag=NULL,last_modified=NULL,last_error=NULL,last_status=NULL,grant_fingerprint=? WHERE tenant_id=? AND source_id=?",
                           (_iso(now), fingerprint, tenant_id, source.source_id))
            due = _aware(state["next_poll"]) if state else None
            if not force and due and due > now:
                db.rollback()
                return state, "not_due"
            # Longer than the bounded HTTP timeout, leaving room for DNS/TLS setup.
            lease_until = now + timedelta(seconds=max(30, source.timeout_seconds + 10))
            next_poll = state["next_poll"] if state else _iso(now)
            db.execute("INSERT INTO poll_state(tenant_id,source_id,next_poll,attempts,etag,last_modified,last_error,last_status,lease_until,grant_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,source_id) DO UPDATE SET lease_until=excluded.lease_until,grant_fingerprint=excluded.grant_fingerprint",
                       (tenant_id, source.source_id, next_poll, state["attempts"] if state else 0,
                        state.get("etag") if state else None, state.get("last_modified") if state else None,
                        state.get("last_error") if state else None, state.get("last_status") if state else None,
                        _iso(lease_until), fingerprint))
            db.commit()
            if state is None:
                state = {"next_poll": next_poll, "attempts": 0, "etag": None, "last_modified": None,
                         "last_error": None, "last_status": None, "lease_until": _iso(lease_until),
                         "grant_fingerprint": fingerprint}
            else:
                state["lease_until"] = _iso(lease_until)
                state["grant_fingerprint"] = fingerprint
            return state, None

    def _save_state(self, tenant_id: str, source: SourceGrant, next_poll: datetime, prior: dict[str, Any] | None, response: "_Response | FetchResult | None", error: str | None, *, success: bool) -> None:
        headers = response.headers if response else {}
        attempts = 0 if success else (prior["attempts"] if prior else 0) + 1
        fingerprint = self._grant_fingerprint(source)
        with sqlite3.connect(self.state_path, timeout=10, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT grant_fingerprint FROM poll_state WHERE tenant_id=? AND source_id=?", (tenant_id, source.source_id)).fetchone()
            # A slower request under an old grant may not overwrite newer grant state.
            if current and current[0] != fingerprint:
                db.rollback()
                return
            db.execute("""INSERT INTO poll_state(tenant_id,source_id,next_poll,attempts,etag,last_modified,last_error,last_status,lease_until,grant_fingerprint) VALUES(?,?,?,?,?,?,?,?,NULL,?) ON CONFLICT(tenant_id,source_id) DO UPDATE SET
              next_poll=excluded.next_poll,attempts=excluded.attempts,etag=excluded.etag,last_modified=excluded.last_modified,last_error=excluded.last_error,last_status=excluded.last_status,lease_until=NULL,grant_fingerprint=excluded.grant_fingerprint""", (tenant_id, source.source_id, _iso(next_poll), attempts, headers.get("etag") or (prior["etag"] if prior else None), headers.get("last-modified") or (prior["last_modified"] if prior else None), error, response.status if response else None, fingerprint))
            db.commit()

    @staticmethod
    def _backoff(source: SourceGrant, state: dict[str, Any] | None, now: datetime, retry_after: str | None) -> datetime:
        retry_date = _aware(retry_after)
        if retry_date and now < retry_date <= now + timedelta(hours=6):
            return retry_date
        attempt = (state["attempts"] if state else 0) + 1
        ceiling = min(source.poll_interval_seconds, 60 * (2 ** min(attempt, 10)))
        return now + timedelta(seconds=max(1, int(random.uniform(ceiling / 2, ceiling))))


class _RetryableFetchError(ValueError):
    def __init__(self, code: str, response: "_Response") -> None:
        super().__init__(code)
        self.response = response


class _Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url
        self.content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
