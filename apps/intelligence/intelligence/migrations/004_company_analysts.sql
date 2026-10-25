CREATE SCHEMA IF NOT EXISTS ai_company_analysts;
CREATE TABLE ai_company_analysts.company_analyst_versions (
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    config_ref TEXT NOT NULL,
    context_version INTEGER NOT NULL CHECK (context_version > 0),
    config_json TEXT NOT NULL CHECK (config_json IS JSON OBJECT),
    content_hash TEXT NOT NULL,
    stable SMALLINT NOT NULL CHECK (stable IN (0, 1)),
    source_config_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    context_hash TEXT NOT NULL DEFAULT '',
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, company_id, revision),
    UNIQUE (store_key, tenant_id, company_id, config_ref)
);
CREATE TABLE ai_company_analysts.company_analyst_current (
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, company_id),
    FOREIGN KEY (store_key, tenant_id, company_id, revision)
        REFERENCES ai_company_analysts.company_analyst_versions (store_key, tenant_id, company_id, revision)
);
CREATE INDEX company_analyst_history ON ai_company_analysts.company_analyst_versions
    (store_key, tenant_id, company_id, revision);
