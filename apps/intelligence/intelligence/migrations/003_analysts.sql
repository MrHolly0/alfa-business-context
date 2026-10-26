CREATE SCHEMA IF NOT EXISTS ai_analysts;
CREATE TABLE ai_analysts.configs (
    ref TEXT NOT NULL,
    config_json TEXT NOT NULL CHECK (config_json IS JSON OBJECT),
    content_hash TEXT NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, ref)
);
CREATE TABLE ai_analysts.activations (
    scope TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    ref TEXT,
    reviewer TEXT NOT NULL,
    report_digest TEXT,
    action TEXT NOT NULL CHECK (action IN ('promote', 'rollback')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, scope, revision),
    FOREIGN KEY (store_key, ref) REFERENCES ai_analysts.configs (store_key, ref)
);
CREATE INDEX activations_latest ON ai_analysts.activations (store_key, scope, revision DESC);
