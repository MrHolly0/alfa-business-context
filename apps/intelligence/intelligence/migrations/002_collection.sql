CREATE SCHEMA IF NOT EXISTS ai_collection;

CREATE TABLE ai_collection.poll_state (
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    next_poll TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    etag TEXT,
    last_modified TEXT,
    last_error TEXT,
    last_status INTEGER CHECK (last_status IS NULL OR last_status BETWEEN 100 AND 599),
    lease_until TIMESTAMPTZ,
    grant_fingerprint TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_token TEXT,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, source_id)
);

CREATE INDEX poll_state_lease
    ON ai_collection.poll_state (store_key, lease_until)
    WHERE lease_until IS NOT NULL;

CREATE TABLE ai_collection.raw_receipts (
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    canonical_uri TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    raw_ref TEXT NOT NULL,
    content_type TEXT NOT NULL,
    title TEXT,
    publisher TEXT,
    author TEXT,
    published_at TIMESTAMPTZ,
    event_time TIMESTAMPTZ,
    language TEXT,
    document_revision_ref TEXT,
    fetched_at TIMESTAMPTZ NOT NULL,
    metadata_json TEXT NOT NULL CHECK (metadata_json IS JSON OBJECT),
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, source_id, canonical_uri, raw_sha256)
);

CREATE INDEX raw_receipts_revision
    ON ai_collection.raw_receipts (store_key, document_revision_ref)
    WHERE document_revision_ref IS NOT NULL;
CREATE INDEX raw_receipts_source_fetched
    ON ai_collection.raw_receipts (store_key, tenant_id, source_id, fetched_at);
