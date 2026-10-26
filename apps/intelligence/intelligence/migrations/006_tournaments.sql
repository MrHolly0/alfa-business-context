CREATE SCHEMA IF NOT EXISTS ai_tournaments;
CREATE TABLE ai_tournaments.runs (
    run_id TEXT NOT NULL,
    manifest TEXT NOT NULL CHECK (manifest IS JSON OBJECT),
    report TEXT CHECK (report IS NULL OR report IS JSON OBJECT),
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, run_id)
);
CREATE TABLE ai_tournaments.holdout_exposures (
    group_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, group_hash, run_id),
    FOREIGN KEY (store_key, run_id) REFERENCES ai_tournaments.runs (store_key, run_id)
);
CREATE TABLE ai_tournaments.shadow_exposures (
    group_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, group_hash, run_id),
    FOREIGN KEY (store_key, run_id) REFERENCES ai_tournaments.runs (store_key, run_id)
);
CREATE TABLE ai_tournaments.cells (
    cell_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'complete')),
    payload TEXT CHECK (payload IS NULL OR payload IS JSON OBJECT),
    reserved_at DOUBLE PRECISION NOT NULL,
    store_key TEXT NOT NULL DEFAULT current_setting('intelligence.store_key'),
    PRIMARY KEY (store_key, cell_key),
    FOREIGN KEY (store_key, run_id) REFERENCES ai_tournaments.runs (store_key, run_id)
);
CREATE INDEX tournament_cells_run ON ai_tournaments.cells (store_key, run_id, state);
