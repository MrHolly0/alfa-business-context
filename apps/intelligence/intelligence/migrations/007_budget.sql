-- Exact per-store model-call accounting. The runtime sets
-- intelligence.store_key on each connection before using these tables.
CREATE TABLE ai_budget.accounts (
    scope text NOT NULL,
    currency text NOT NULL,
    limit_amount numeric NOT NULL,
    lifetime_limit numeric,
    store_key text NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, scope),
    CHECK (limit_amount >= 0 AND limit_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')),
    CHECK (lifetime_limit IS NULL OR
           (lifetime_limit >= 0 AND lifetime_limit::text NOT IN ('NaN', 'Infinity', '-Infinity')))
);

CREATE TABLE ai_budget.reservations (
    id text NOT NULL,
    scope text NOT NULL,
    currency text NOT NULL,
    period_start timestamptz NOT NULL,
    reserved_amount numeric NOT NULL,
    actual_amount numeric,
    cost_source text,
    request_id text,
    status text NOT NULL CHECK(status IN ('reserved','uncertain','settled')),
    usage_json text,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at timestamptz,
    store_key text NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, id),
    FOREIGN KEY (store_key, scope) REFERENCES ai_budget.accounts (store_key, scope),
    CHECK (reserved_amount >= 0 AND reserved_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')),
    CHECK (actual_amount IS NULL OR
           (actual_amount >= 0 AND actual_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')))
);

CREATE INDEX reservations_scope_period
    ON ai_budget.reservations (store_key, scope, period_start, status);
