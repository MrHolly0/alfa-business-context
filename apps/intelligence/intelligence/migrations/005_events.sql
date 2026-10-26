CREATE SCHEMA IF NOT EXISTS ai_events;
CREATE TABLE ai_events.events (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, event_id)
);
CREATE TABLE ai_events.publications (
    tenant_id TEXT NOT NULL,
    publication_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_role TEXT NOT NULL CHECK (source_role IN ('external', 'bank')),
    canonical_uri TEXT NOT NULL,
    origin_group_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, publication_id),
    UNIQUE (store_key, tenant_id, source_id, canonical_uri),
    FOREIGN KEY (store_key, tenant_id, event_id)
        REFERENCES ai_events.events (store_key, tenant_id, event_id)
);
CREATE INDEX publications_event ON ai_events.publications (store_key, tenant_id, event_id, source_id);
CREATE TABLE ai_events.publication_versions (
    tenant_id TEXT NOT NULL,
    publication_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    event_time TIMESTAMPTZ,
    content_type TEXT NOT NULL,
    publisher TEXT NOT NULL,
    author TEXT NOT NULL,
    language TEXT NOT NULL,
    synthetic SMALLINT NOT NULL CHECK (synthetic IN (0, 1)),
    document_revision_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    data_classification TEXT NOT NULL DEFAULT 'confidential'
        CHECK (data_classification IN ('public', 'synthetic', 'confidential')),
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, publication_id, version),
    FOREIGN KEY (store_key, tenant_id, publication_id)
        REFERENCES ai_events.publications (store_key, tenant_id, publication_id)
);
CREATE INDEX publication_hash ON ai_events.publication_versions (store_key, tenant_id, content_hash);
CREATE INDEX publication_versions_latest ON ai_events.publication_versions
    (store_key, tenant_id, publication_id, version DESC);
CREATE TABLE ai_events.event_revisions (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    snapshot_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, event_id, revision),
    FOREIGN KEY (store_key, tenant_id, event_id)
        REFERENCES ai_events.events (store_key, tenant_id, event_id)
);
CREATE TABLE ai_events.event_revision_publications (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_revision INTEGER NOT NULL,
    publication_id TEXT NOT NULL,
    publication_revision INTEGER NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, event_id, event_revision, publication_id),
    FOREIGN KEY (store_key, tenant_id, event_id, event_revision)
        REFERENCES ai_events.event_revisions (store_key, tenant_id, event_id, revision),
    FOREIGN KEY (store_key, tenant_id, publication_id, publication_revision)
        REFERENCES ai_events.publication_versions (store_key, tenant_id, publication_id, version)
);
CREATE TABLE ai_events.impact_packages (
    tenant_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    source_policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    allowed_sources_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (payload_json IS JSON OBJECT),
    created_at TIMESTAMPTZ NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, package_id),
    FOREIGN KEY (store_key, tenant_id, event_id, revision)
        REFERENCES ai_events.event_revisions (store_key, tenant_id, event_id, revision)
);
CREATE TABLE ai_events.claim_extractions (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    allowed_sources_hash TEXT NOT NULL,
    semantic_signature TEXT NOT NULL,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    claims_json TEXT NOT NULL CHECK (claims_json IS JSON ARRAY),
    metadata_json TEXT NOT NULL CHECK (metadata_json IS JSON OBJECT),
    created_at TIMESTAMPTZ NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, event_id, source_policy_id, policy_version,
        allowed_sources_hash, semantic_signature, model_version, prompt_version),
    FOREIGN KEY (store_key, tenant_id, event_id)
        REFERENCES ai_events.events (store_key, tenant_id, event_id)
);
CREATE TABLE ai_events.claim_extraction_reservations (
    tenant_id TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'uncertain')),
    created_at TIMESTAMPTZ NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL,
    owner_token TEXT NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, reservation_id)
);
CREATE INDEX claim_reservations_lease ON ai_events.claim_extraction_reservations
    (store_key, lease_until);
CREATE TABLE ai_events.snapshot_markers (
    tenant_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id)
);
