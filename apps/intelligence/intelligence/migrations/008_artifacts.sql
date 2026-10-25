-- Request idempotency, job fencing, and durable Core result delivery.
-- The runtime sets intelligence.store_key on each connection before use.
CREATE TABLE ai_artifacts.candidates (
    tenant_id text NOT NULL,
    request_id text NOT NULL,
    payload_hash text NOT NULL,
    response text NOT NULL,
    store_key text NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, request_id)
);

CREATE TABLE ai_artifacts.reservations (
    tenant_id text NOT NULL,
    request_id text NOT NULL,
    payload_hash text NOT NULL,
    owner_token text,
    store_key text NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id, request_id)
);

CREATE TABLE ai_artifacts.outbox (
    message_id text NOT NULL,
    job_id text NOT NULL,
    payload text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at double precision NOT NULL DEFAULT 0,
    retry_deadline double precision NOT NULL DEFAULT 0,
    claim_until double precision NOT NULL DEFAULT 0,
    claim_token text,
    last_error text,
    store_key text NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, message_id)
);

CREATE INDEX outbox_due
    ON ai_artifacts.outbox (store_key, next_attempt_at, retry_deadline, claim_until, created_at, message_id);

CREATE TABLE ai_artifacts.outbox_dead (
    message_id text NOT NULL,
    job_id text NOT NULL,
    payload text NOT NULL,
    reason text NOT NULL,
    failed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    store_key text NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, message_id)
);

CREATE TABLE ai_artifacts.job_runs (
    job_id text NOT NULL,
    lease_generation integer NOT NULL,
    input_hash text NOT NULL,
    status text NOT NULL,
    message_id text,
    payload text,
    reason text,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    store_key text NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, job_id, lease_generation)
);
