CREATE SCHEMA IF NOT EXISTS ai_corpus;

CREATE TABLE ai_corpus.documents (
    document_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_role TEXT NOT NULL CHECK (source_role IN ('external', 'bank')),
    canonical_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    language TEXT NOT NULL,
    synthetic SMALLINT NOT NULL CHECK (synthetic IN (0, 1)),
    created_at TIMESTAMPTZ NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, document_id),
    UNIQUE (store_key, tenant_id, source_id, canonical_uri)
);

CREATE TABLE ai_corpus.revisions (
    document_revision_ref TEXT NOT NULL,
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    content_type TEXT NOT NULL,
    extracted_text TEXT NOT NULL,
    warnings TEXT NOT NULL CHECK (warnings IS JSON ARRAY),
    publisher TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'ru',
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, document_revision_ref),
    UNIQUE (store_key, document_id, revision),
    FOREIGN KEY (store_key, document_id)
        REFERENCES ai_corpus.documents (store_key, document_id)
);

CREATE INDEX revisions_lookup
    ON ai_corpus.revisions (store_key, document_id, fetched_at, revision);
CREATE INDEX revisions_hash
    ON ai_corpus.revisions (store_key, content_hash);

CREATE TABLE ai_corpus.evidence (
    evidence_id TEXT NOT NULL,
    document_revision_ref TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    locator TEXT NOT NULL,
    quote TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, evidence_id),
    UNIQUE (store_key, document_revision_ref, ordinal),
    FOREIGN KEY (store_key, document_revision_ref)
        REFERENCES ai_corpus.revisions (store_key, document_revision_ref)
);

CREATE INDEX evidence_revision
    ON ai_corpus.evidence (store_key, document_revision_ref);

CREATE TABLE ai_corpus.document_provenance (
    document_revision_ref TEXT NOT NULL,
    author TEXT NOT NULL,
    event_time TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL,
    raw_sha256 TEXT NOT NULL,
    data_classification TEXT NOT NULL DEFAULT 'confidential'
        CHECK (data_classification IN ('public', 'synthetic', 'confidential')),
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, document_revision_ref),
    FOREIGN KEY (store_key, document_revision_ref)
        REFERENCES ai_corpus.revisions (store_key, document_revision_ref)
);
