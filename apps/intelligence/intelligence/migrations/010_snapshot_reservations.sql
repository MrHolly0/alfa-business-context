-- Tournament run/cell/exposure IDs are independently addressable. Their
-- application-level lifecycle checks do not require creation ordering.
ALTER TABLE ai_tournaments.holdout_exposures
    DROP CONSTRAINT IF EXISTS holdout_exposures_store_key_run_id_fkey;
ALTER TABLE ai_tournaments.shadow_exposures
    DROP CONSTRAINT IF EXISTS shadow_exposures_store_key_run_id_fkey;
ALTER TABLE ai_tournaments.cells
    DROP CONSTRAINT IF EXISTS cells_store_key_run_id_fkey;

-- Freeze empty as well as populated tenant event snapshots in a separate store.
CREATE TABLE IF NOT EXISTS ai_events.snapshot_markers (
    tenant_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, tenant_id)
);
