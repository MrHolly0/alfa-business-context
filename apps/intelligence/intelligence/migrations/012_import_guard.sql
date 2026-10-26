ALTER TABLE ai_meta.imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_meta.imports FORCE ROW LEVEL SECURITY;
CREATE POLICY import_scope ON ai_meta.imports
USING(store_key=current_setting('intelligence.store_key',true))
WITH CHECK(store_key=current_setting('intelligence.store_key',true));
GRANT SELECT ON ai_meta.imports TO intelligence_app;
