"""Immutable tenant-scoped document revisions and lexical evidence retrieval."""
from __future__ import annotations

import hashlib
import base64
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from .parsers import parse_content

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, source_id TEXT NOT NULL,
  source_role TEXT NOT NULL, canonical_uri TEXT NOT NULL, title TEXT NOT NULL,
  publisher TEXT NOT NULL, language TEXT NOT NULL, synthetic INTEGER NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(tenant_id, source_id, canonical_uri)
);
CREATE TABLE IF NOT EXISTS revisions (
  document_revision_ref TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(document_id),
  revision INTEGER NOT NULL, content_hash TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL, content_type TEXT NOT NULL, extracted_text TEXT NOT NULL,
  warnings TEXT NOT NULL, publisher TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT 'ru',
  UNIQUE(document_id, revision)
);
CREATE INDEX IF NOT EXISTS revisions_lookup ON revisions(document_id, fetched_at, revision);
CREATE INDEX IF NOT EXISTS revisions_hash ON revisions(content_hash);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY, document_revision_ref TEXT NOT NULL REFERENCES revisions(document_revision_ref),
  ordinal INTEGER NOT NULL, locator TEXT NOT NULL, quote TEXT NOT NULL, content_hash TEXT NOT NULL,
  UNIQUE(document_revision_ref, ordinal)
);
CREATE INDEX IF NOT EXISTS evidence_revision ON evidence(document_revision_ref);
"""


def _aware(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an aware ISO datetime")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an aware ISO datetime") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field} must include timezone")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.findall(r"[\w]+", normalized, flags=re.UNICODE)


def _chunks(text: str, size: int = 850, overlap: int = 100) -> list[tuple[str, str]]:
    """Stable chunks with character/page locators; prefer sentence boundaries."""
    chunks: list[tuple[str, str]] = []
    page_start = 0
    page_no = 1
    # PDF parser marks page starts. Keep this explicit locator information.
    for segment in re.split(r"(?=\[page \d+\]\n)", text):
        if not segment:
            continue
        match = re.match(r"\[page (\d+)\]\n", segment)
        if match:
            page_no = int(match.group(1))
            segment = segment[match.end():]
            page_start = 0
        pos = 0
        while pos < len(segment):
            end = min(pos + size, len(segment))
            if end < len(segment):
                boundary = max(segment.rfind(". ", pos + size // 2, end), segment.rfind("\n", pos + size // 2, end))
                if boundary > pos:
                    end = boundary + 1
            quote = segment[pos:end].strip()
            if quote:
                locator = f"page:{page_no};chars:{page_start + pos}-{page_start + end}" if "[page " in text else f"chars:{page_start + pos}-{page_start + end}"
                chunks.append((locator, quote[:1000]))
            if end >= len(segment):
                break
            pos = max(end - overlap, pos + 1)
        page_start += len(segment)
    return chunks


class CorpusStore:
    def __init__(self, path: str | Path, *, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("CorpusStore requires a persistent SQLite path")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            # Serialize schema inspection and additive migration. Each initializer
            # re-reads columns only after acquiring the write lock.
            db.execute("CREATE TABLE IF NOT EXISTS document_provenance(document_revision_ref TEXT PRIMARY KEY, author TEXT NOT NULL, event_time TEXT, received_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            provenance_columns={row[1] for row in db.execute("PRAGMA table_info(document_provenance)")}
            if "data_classification" not in provenance_columns:
                db.execute("ALTER TABLE document_provenance ADD COLUMN data_classification TEXT NOT NULL DEFAULT 'confidential'")
                db.execute("UPDATE document_provenance SET data_classification='synthetic' WHERE document_revision_ref IN (SELECT r.document_revision_ref FROM revisions r JOIN documents d USING(document_id) WHERE d.synthetic=1)")
            revision_columns = {row[1] for row in db.execute("PRAGMA table_info(revisions)")}
            if "publisher" not in revision_columns:
                db.execute("ALTER TABLE revisions ADD COLUMN publisher TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE revisions SET publisher=(SELECT publisher FROM documents WHERE documents.document_id=revisions.document_id)")
            if "language" not in revision_columns:
                db.execute("ALTER TABLE revisions ADD COLUMN language TEXT NOT NULL DEFAULT 'ru'")
                db.execute("UPDATE revisions SET language=(SELECT language FROM documents WHERE documents.document_id=revisions.document_id)")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ingest(self, document: dict) -> dict:
        required = ("tenant_id", "source_id", "source_role", "canonical_uri", "title", "content", "content_type", "published_at", "fetched_at", "synthetic")
        missing = [key for key in required if key not in document]
        if missing:
            raise ValueError(f"missing document fields: {', '.join(missing)}")
        if not isinstance(document["tenant_id"], str) or not document["tenant_id"] or len(document["tenant_id"]) > 256 or not isinstance(document["source_id"], str) or not document["source_id"] or len(document["source_id"]) > 256:
            raise ValueError("tenant_id/source_id must be non-empty strings")
        tenant_id, source_id = document["tenant_id"], document["source_id"]
        source_role = document["source_role"]
        if source_role not in {"external", "bank"}:
            raise ValueError("tenant_id/source_id must be non-empty and source_role external|bank")
        published_at = _aware(document["published_at"], "published_at")
        fetched_at = _aware(document["fetched_at"], "fetched_at")
        content_type = document["content_type"]
        parsed = parse_content(document["content"], content_type)
        if parsed.items:
            item_results = []
            for item in parsed.items:
                if not item.text.strip():
                    continue
                item_published = published_at
                if item.published_at:
                    try:
                        item_dt = datetime.fromisoformat(item.published_at.replace("Z", "+00:00"))
                    except ValueError:
                        try:
                            item_dt = parsedate_to_datetime(item.published_at)
                        except (TypeError, ValueError, OverflowError):
                            continue
                    if item_dt.tzinfo is None or item_dt.utcoffset() is None:
                        continue
                    item_published = item_dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                item_uri = item.uri.strip() if item.uri and item.uri.strip() else (str(document["canonical_uri"]).rstrip("/") + "#item-" + _hash("\0".join((item.title, item.text, item.published_at or "")))[:20])
                item_results.append(self.ingest({**document, "canonical_uri": item_uri, "title": item.title or str(document["title"]), "content": item.text, "content_type": "text/plain", "published_at": item_published}))
            base = item_results[0] if item_results else {"document_id": "doc_" + _hash(tenant_id + "\0" + source_id + "\0" + str(document["canonical_uri"]))[:32], "revision": 0, "document_revision_ref": None, "content_hash": _hash(""), "duplicate_of": None, "evidence_count": 0, "synthetic": bool(document["synthetic"])}
            return {**base, "items": item_results, "parse_warnings": list(parsed.warnings)}
        extracted = parsed.text
        content_hash = _hash(extracted)
        for field in ("canonical_uri", "title", "content_type"):
            maximum = 4096 if field == "canonical_uri" else 1000
            if not isinstance(document[field], str) or not document[field] or len(document[field]) > maximum:
                raise ValueError(f"{field} must be a non-empty string of at most {maximum} characters")
        if not isinstance(document["synthetic"], bool):
            raise ValueError("synthetic must be a boolean")
        if not isinstance(document.get("publisher", ""), str) or not isinstance(document.get("language", "ru"), str):
            raise ValueError("publisher and language must be strings")
        canonical_uri = document["canonical_uri"]
        title = document["title"]
        publisher = str(document.get("publisher", ""))
        language = str(document.get("language", "ru"))
        now = fetched_at
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM documents WHERE tenant_id=? AND source_id=? AND canonical_uri=?", (tenant_id, source_id, canonical_uri)).fetchone()
            if row:
                if row["source_role"] != source_role or bool(row["synthetic"]) != document["synthetic"]:
                    raise ValueError("source_role and synthetic are immutable for a canonical URI")
                doc_id = row["document_id"]
            else:
                doc_id = "doc_" + _hash("\0".join((tenant_id, source_id, canonical_uri)))[:32]
                db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)", (doc_id, tenant_id, source_id, source_role, canonical_uri, title, str(document.get("publisher", "")), str(document.get("language", "ru")), int(bool(document["synthetic"])), now))
            previous = db.execute("SELECT revision, content_hash, title, published_at, fetched_at, content_type, publisher, language, (SELECT p.data_classification FROM document_provenance p WHERE p.document_revision_ref=revisions.document_revision_ref) AS data_classification FROM revisions WHERE document_id=? ORDER BY revision DESC LIMIT 1", (doc_id,)).fetchone()
            if previous and (previous["content_hash"], previous["title"], previous["published_at"], previous["content_type"], previous["publisher"], previous["language"],previous["data_classification"]) == (content_hash, title, published_at, content_type, publisher, language,"synthetic" if document["synthetic"] else document.get("data_classification","confidential")):
                ref = f"{doc_id}:{previous['revision']}"
                duplicate = db.execute("SELECT d.document_id FROM revisions r JOIN documents d USING(document_id) WHERE r.content_hash=? AND d.document_id<>? AND d.tenant_id=? AND d.source_role=? ORDER BY r.fetched_at,d.document_id LIMIT 1", (content_hash, doc_id, tenant_id, source_role)).fetchone()
                self._save_original(document, ref, db)
                return {"document_id": doc_id, "revision": previous["revision"], "document_revision_ref": ref, "content_hash": content_hash, "duplicate_of": duplicate["document_id"] if duplicate else None, "evidence_count": db.execute("SELECT count(*) FROM evidence WHERE document_revision_ref=?", (ref,)).fetchone()[0], "parse_warnings": list(parsed.warnings), "synthetic": bool(document["synthetic"])}
            revision = (previous["revision"] + 1) if previous else 1
            ref = f"{doc_id}:{revision}"
            if previous and fetched_at <= previous["fetched_at"]:
                prior_known_at = datetime.fromisoformat(previous["fetched_at"].replace("Z", "+00:00"))
                fetched_at = (prior_known_at + timedelta(microseconds=1)).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            duplicate = db.execute("SELECT d.document_id FROM revisions r JOIN documents d USING(document_id) WHERE r.content_hash=? AND d.document_id<>? AND d.tenant_id=? AND d.source_role=? ORDER BY r.fetched_at,d.document_id LIMIT 1", (content_hash, doc_id, tenant_id, source_role)).fetchone()
            db.execute("INSERT INTO revisions(document_revision_ref,document_id,revision,content_hash,title,published_at,fetched_at,content_type,extracted_text,warnings,publisher,language) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (ref, doc_id, revision, content_hash, title, published_at, fetched_at, content_type, extracted, json.dumps(parsed.warnings), publisher, language))
            self._save_original(document, ref, db)
            for ordinal, (locator, quote) in enumerate(_chunks(extracted)):
                evidence_id = "ev_" + _hash(f"{ref}\0{ordinal}\0{quote}")[:40]
                db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?)", (evidence_id, ref, ordinal, locator, quote, _hash(quote)))
            count = db.execute("SELECT count(*) FROM evidence WHERE document_revision_ref=?", (ref,)).fetchone()[0]
        return {"document_id": doc_id, "revision": revision, "document_revision_ref": ref, "content_hash": content_hash, "duplicate_of": duplicate["document_id"] if duplicate else None, "evidence_count": count, "parse_warnings": list(parsed.warnings), "synthetic": bool(document["synthetic"])}

    def search(self, *, tenant_id: str, source_ids: list[str], as_of: str, query: str, document_revision_refs: list[str] | None = None, role: str | None = None, limit: int = 8) -> list[dict]:
        if not isinstance(query, str) or len(query) > 10_000:
            raise ValueError("query must be a string of at most 10000 characters")
        as_of_value = _aware(as_of, "as_of")
        if role not in (None, "external", "bank"):
            raise ValueError("role must be external|bank")
        if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 256 or not isinstance(source_ids, list) or len(source_ids) > 500 or any(not isinstance(s, str) or not s or len(s) > 256 for s in source_ids):
            raise ValueError("tenant_id/source_ids must be bounded non-empty strings (up to 500 sources)")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if not source_ids:
            return []
        terms = list(dict.fromkeys(_tokens(query)))
        if not terms:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        params: list[Any] = [tenant_id, *source_ids, as_of_value, as_of_value]
        sql = f"""SELECT e.*, d.document_id,d.tenant_id,d.source_id,d.source_role,r.title,r.publisher,r.language,d.canonical_uri,d.synthetic,
                         r.revision,r.document_revision_ref,r.published_at,r.fetched_at,p.author,p.event_time,p.received_at,p.raw_sha256,p.data_classification
                  FROM evidence e JOIN revisions r USING(document_revision_ref) JOIN documents d USING(document_id) LEFT JOIN document_provenance p USING(document_revision_ref)
                  WHERE d.tenant_id=? AND d.source_id IN ({placeholders}) AND d.source_role IN ('external','bank')
                    AND r.fetched_at<=? AND r.published_at<=?"""
        if role:
            sql += " AND d.source_role=?"
            params.append(role)
        if document_revision_refs is not None and (not isinstance(document_revision_refs, list) or len(document_revision_refs) > 500 or any(not isinstance(ref, str) or not ref or len(ref) > 512 for ref in document_revision_refs)):
            raise ValueError("document_revision_refs must be a list of non-empty strings")
        refs = list(dict.fromkeys(document_revision_refs or []))
        if refs:
            sql += " AND r.document_revision_ref IN (" + ",".join("?" for _ in refs) + ")"
            params.extend(refs)
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        # By default only the latest available revision for a document is searchable.
        if not refs:
            # Determine the latest eligible revision from the revision table, even when
            # that revision produced no chunks; otherwise stale evidence could leak.
            with self._connect() as db:
                latest_rows = db.execute(
                    f"SELECT d.document_id, MAX(r.revision) AS revision FROM documents d "
                    f"JOIN revisions r USING(document_id) WHERE d.tenant_id=? "
                    f"AND d.source_id IN ({placeholders}) AND r.fetched_at<=? AND r.published_at<=? "
                    + ("AND d.source_role=? " if role else "") + f"GROUP BY d.document_id",
                    [tenant_id, *source_ids, as_of_value, as_of_value] + ([role] if role else [])
                ).fetchall()
            latest = {row["document_id"]: row["revision"] for row in latest_rows}
            rows = [row for row in rows if row["revision"] == latest.get(row["document_id"])]
        scored = []
        for row in rows:
            tokens = _tokens(row["quote"])
            if not tokens:
                continue
            counts = {term: tokens.count(term) for term in terms}
            matched = sum(count > 0 for count in counts.values())
            if not matched:
                continue
            score = matched / len(terms) + min(sum(counts.values()), 10) * 0.005
            scored.append((score, row))
        # Exact reprints form one ranking group, so they cannot crowd the result by repetition.
        scored.sort(key=lambda pair: (-pair[0], pair[1]["fetched_at"], pair[1]["evidence_id"]))
        selected: list[tuple[float, sqlite3.Row]] = []
        seen_content: set[tuple[str, str]] = set()
        for score, row in scored:
            dedup_key = (row["content_hash"], row["quote"] )
            if dedup_key in seen_content:
                continue
            seen_content.add(dedup_key)
            selected.append((score, row))
            if len(selected) >= limit:
                break
        return [self._result(row, score) for score, row in selected]

    @staticmethod
    def _result(row: sqlite3.Row, score: float) -> dict:
        return {"evidence_id": row["evidence_id"], "document_id": row["document_id"], "document_revision_ref": row["document_revision_ref"], "source_id": row["source_id"], "source_role": row["source_role"], "title": row["title"], "canonical_uri": row["canonical_uri"], "published_at": row["published_at"], "fetched_at": row["fetched_at"], "locator": row["locator"], "quote": row["quote"][:1000], "content_hash": row["content_hash"], "synthetic": bool(row["synthetic"]), "score": float(score), "author":row["author"] or "", "event_time":row["event_time"], "received_at":row["received_at"], "raw_sha256":row["raw_sha256"],"data_classification":row["data_classification"] or "confidential"}

    def get_evidence(self, *, tenant_id: str, source_ids: list[str], as_of: str, evidence_id: str) -> dict | None:
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError("evidence_id must be a non-empty string")
        as_of_value = _aware(as_of, "as_of")
        if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 256 or not isinstance(source_ids, list) or len(source_ids) > 500 or any(not isinstance(s, str) or not s or len(s) > 256 for s in source_ids):
            raise ValueError("tenant_id/source_ids must be bounded non-empty strings (up to 500 sources)")
        if not source_ids:
            return None
        marks = ",".join("?" for _ in source_ids)
        with self._connect() as db:
            row = db.execute(f"""SELECT e.*,d.document_id,d.tenant_id,d.source_id,d.source_role,r.title,r.publisher,r.language,d.canonical_uri,d.synthetic,
                                      r.revision,r.document_revision_ref,r.published_at,r.fetched_at,p.author,p.event_time,p.received_at,p.raw_sha256,p.data_classification
                               FROM evidence e JOIN revisions r USING(document_revision_ref) JOIN documents d USING(document_id) LEFT JOIN document_provenance p USING(document_revision_ref)
                               WHERE e.evidence_id=? AND d.tenant_id=? AND d.source_id IN ({marks})
                                 AND r.fetched_at<=? AND r.published_at<=?
                                 AND r.revision=(SELECT MAX(r2.revision) FROM revisions r2 WHERE r2.document_id=d.document_id AND r2.fetched_at<=? AND r2.published_at<=?)""", (evidence_id, tenant_id, *source_ids, as_of_value, as_of_value, as_of_value, as_of_value)).fetchone()
        if row is None:
            return None
        return self._result(row, 0.0)


    def _save_original(self, document, ref, db):
        """Store an immutable original and a server receipt; client fetched_at is not receipt time."""
        raw = base64.b64decode(document["content"], validate=True) if document["content_type"] == "application/pdf" else document["content"].encode("utf-8")
        # Only the internal collector can pass an upstream raw reference. Never read
        # arbitrary paths from an ingest payload, even when a matching hash is supplied.
        raw_ref = document.get("raw_ref")
        if raw_ref:
            candidate = Path(raw_ref).resolve()
            managed = (Path(self.path).parent / "raw").resolve()
            if not candidate.is_relative_to(managed) or not candidate.is_file():
                raise ValueError("raw_ref_outside_managed_storage")
            upstream = candidate.read_bytes()
            if len(upstream) > 20_000_000 or hashlib.sha256(upstream).hexdigest() != document.get("raw_sha256"):
                raise ValueError("raw_receipt_hash_mismatch")
            raw = upstream
        digest = hashlib.sha256(raw).hexdigest()
        folder = Path(self.path).parent / "raw" / "corpus" / _hash(document["tenant_id"])
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / digest
        if not target.exists():
            try:
                with target.open("xb") as out: out.write(raw)
                target.chmod(0o600)
            except FileExistsError: pass
        event_time = document.get("event_time")
        if event_time: event_time = _aware(event_time, "event_time")
        classification="synthetic" if document["synthetic"] else document.get("data_classification","confidential")
        if classification not in {"public","synthetic","confidential"}: raise ValueError("invalid_data_classification")
        db.execute("INSERT OR IGNORE INTO document_provenance(document_revision_ref,author,event_time,received_at,raw_sha256,data_classification) VALUES(?,?,?,?,?,?)", (ref, str(document.get("author", "")), event_time,
            self.clock().isoformat(), digest, classification))

    def list_documents(self, *, tenant_id, source_ids, as_of, policy=None, as_known=False):
        """Latest eligible versions, with policy exclusions applied before retrieval."""
        if not source_ids: return []
        instant = _aware(as_of, "as_of")
        marks = ",".join("?" for _ in source_ids)
        with self._connect() as db:
            rows = db.execute(f"""SELECT d.*,r.*,p.author,p.event_time,p.received_at,p.raw_sha256,p.data_classification
                FROM documents d JOIN revisions r USING(document_id)
                LEFT JOIN document_provenance p USING(document_revision_ref)
                WHERE d.tenant_id=? AND d.source_id IN ({marks}) AND r.published_at<=? AND r.fetched_at<=?
                ORDER BY d.document_id,r.revision DESC""", (tenant_id,*source_ids,instant,instant)).fetchall()
        selected = {}
        cutoff = datetime.fromisoformat(instant.replace("Z", "+00:00"))
        for row in rows:
            item = dict(row)
            if as_known and (not item.get("received_at") or datetime.fromisoformat(item["received_at"]) > cutoff): continue
            if item["document_id"] in selected: continue
            if policy and not policy.permits_document(item): continue
            item["content"] = item.pop("extracted_text")
            item["content_type"] = "text/plain"
            item["synthetic"] = bool(item["synthetic"])
            selected[item["document_id"]] = item
        return list(selected.values())

    def read_original(self, *, tenant_id, source_ids, document_revision_ref, as_of, policy=None):
        documents = self.list_documents(tenant_id=tenant_id, source_ids=source_ids, as_of=as_of, policy=policy)
        item = next((d for d in documents if d["document_revision_ref"] == document_revision_ref), None)
        if item is None or not item.get("raw_sha256"): raise ValueError("original_not_permitted")
        return (Path(self.path).parent / "raw" / "corpus" / _hash(tenant_id) / item["raw_sha256"]).read_bytes()
