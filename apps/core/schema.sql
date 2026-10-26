CREATE TABLE IF NOT EXISTS app_users (
  id VARCHAR(36) PRIMARY KEY,
  email VARCHAR(254) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  display_name VARCHAR(160) NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  disabled_at VARCHAR(40)
);
CREATE TABLE IF NOT EXISTS import_batches (
  id VARCHAR(36) PRIMARY KEY,
  dataset_version VARCHAR(100) NOT NULL UNIQUE,
  source_name VARCHAR(255) NOT NULL,
  sha256 VARCHAR(64) NOT NULL,
  imported_at VARCHAR(40) NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
  id VARCHAR(36) PRIMARY KEY,
  import_batch_id VARCHAR(36) NOT NULL REFERENCES import_batches(id),
  synthetic_u VARCHAR(100) NOT NULL,
  org_type TEXT, okfs_name TEXT, company_age_years_gradation TEXT,
  eco_mark_label TEXT, mainokved_code TEXT, mainokved_name TEXT,
  negativemark TEXT, amount_workers_fns_gradation TEXT,
  amount_workers_spark_gradation TEXT, cl_type TEXT,
  cl_product_rko_mark TEXT, cl_product_gar_mark TEXT,
  cl_product_loa_mark TEXT, cl_product_lea_mark TEXT,
  cl_product_dep_mark TEXT, cl_product_zpp_mark TEXT,
  fts_flag TEXT, fts_country_name TEXT, factoring_mark TEXT,
  revenue_company_raw TEXT, group_id TEXT, city TEXT, region TEXT,
  source_row INTEGER NOT NULL, raw_json TEXT NOT NULL, raw_hash VARCHAR(64) NOT NULL,
  UNIQUE(import_batch_id,synthetic_u)
);
CREATE TABLE IF NOT EXISTS company_memberships (
  user_id VARCHAR(36) NOT NULL REFERENCES app_users(id),
  company_id VARCHAR(36) NOT NULL REFERENCES companies(id),
  role VARCHAR(30) NOT NULL CHECK(role IN ('viewer','editor','admin')),
  created_at VARCHAR(40) NOT NULL,
  PRIMARY KEY(user_id,company_id)
);
CREATE TABLE IF NOT EXISTS person_company_links (
  id VARCHAR(36) PRIMARY KEY,
  import_batch_id VARCHAR(36) NOT NULL REFERENCES import_batches(id),
  synthetic_f VARCHAR(100) NOT NULL, synthetic_u VARCHAR(100) NOT NULL,
  city_name TEXT, rc_name TEXT, source_row INTEGER NOT NULL,
  raw_json TEXT NOT NULL, raw_hash VARCHAR(64) NOT NULL,
  UNIQUE(import_batch_id,synthetic_f,synthetic_u)
);
CREATE TABLE IF NOT EXISTS payment_aggregates (
  id VARCHAR(36) PRIMARY KEY,
  import_batch_id VARCHAR(36) NOT NULL REFERENCES import_batches(id),
  beneficiary VARCHAR(100) NOT NULL, payer VARCHAR(100) NOT NULL,
  pay_amt_raw TEXT, pay_cnt_raw TEXT, amount_unit TEXT,
  period_start VARCHAR(10), period_end VARCHAR(10), currency VARCHAR(3),
  source_row INTEGER NOT NULL, raw_json TEXT NOT NULL, raw_hash VARCHAR(64) NOT NULL,
  UNIQUE(import_batch_id,source_row)
);
CREATE TABLE IF NOT EXISTS company_context_versions (
  id VARCHAR(36) PRIMARY KEY,
  company_id VARCHAR(36) NOT NULL REFERENCES companies(id),
  version INTEGER NOT NULL, facts_json TEXT NOT NULL,
  source VARCHAR(30) NOT NULL, confirmed_by VARCHAR(36) REFERENCES app_users(id),
  created_at VARCHAR(40) NOT NULL,
  UNIQUE(company_id,version)
);
CREATE TABLE IF NOT EXISTS onboarding_progress (
  user_id VARCHAR(36) PRIMARY KEY REFERENCES app_users(id),
  tour_version INTEGER NOT NULL, next_step INTEGER NOT NULL,
  completed_at VARCHAR(40), updated_at VARCHAR(40) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_group ON companies(group_id);
CREATE INDEX IF NOT EXISTS idx_links_company ON person_company_links(import_batch_id,synthetic_u);
CREATE INDEX IF NOT EXISTS idx_payments_payer ON payment_aggregates(import_batch_id,payer);
CREATE INDEX IF NOT EXISTS idx_payments_beneficiary ON payment_aggregates(import_batch_id,beneficiary);

CREATE TABLE IF NOT EXISTS metric_definitions (
  id VARCHAR(36) PRIMARY KEY,
  company_id VARCHAR(36) NOT NULL REFERENCES companies(id),
  slug VARCHAR(100) NOT NULL,
  created_by VARCHAR(36) NOT NULL REFERENCES app_users(id),
  created_at VARCHAR(40) NOT NULL,
  archived_at VARCHAR(40),
  UNIQUE(company_id,slug)
);
CREATE TABLE IF NOT EXISTS metric_versions (
  id VARCHAR(36) PRIMARY KEY,
  metric_id VARCHAR(36) NOT NULL REFERENCES metric_definitions(id),
  version INTEGER NOT NULL,
  title VARCHAR(160) NOT NULL,
  description TEXT NOT NULL,
  unit VARCHAR(40),
  direction VARCHAR(20) NOT NULL CHECK(direction IN ('up','down','neutral')),
  created_by VARCHAR(36) NOT NULL REFERENCES app_users(id),
  created_at VARCHAR(40) NOT NULL,
  UNIQUE(metric_id,version)
);
CREATE TABLE IF NOT EXISTS metric_observations (
  id VARCHAR(36) PRIMARY KEY,
  metric_id VARCHAR(36) NOT NULL REFERENCES metric_definitions(id),
  value_raw VARCHAR(100) NOT NULL,
  unit VARCHAR(40),
  period_start VARCHAR(10) NOT NULL,
  period_end VARCHAR(10) NOT NULL,
  source_label VARCHAR(200) NOT NULL,
  entered_by VARCHAR(36) NOT NULL REFERENCES app_users(id),
  created_at VARCHAR(40) NOT NULL,
  CHECK(period_start <= period_end)
);
CREATE TABLE IF NOT EXISTS business_events (
  id VARCHAR(36) PRIMARY KEY,
  company_id VARCHAR(36) NOT NULL REFERENCES companies(id),
  title VARCHAR(240) NOT NULL,
  summary TEXT NOT NULL,
  kind VARCHAR(40) NOT NULL,
  published_on VARCHAR(10) NOT NULL,
  source_label VARCHAR(200) NOT NULL,
  source_url TEXT,
  created_at VARCHAR(40) NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_event_links (
  metric_id VARCHAR(36) NOT NULL REFERENCES metric_definitions(id),
  event_id VARCHAR(36) NOT NULL REFERENCES business_events(id),
  explanation TEXT NOT NULL,
  direction VARCHAR(20) NOT NULL CHECK(direction IN ('up','down','mixed','unknown')),
  status VARCHAR(20) NOT NULL CHECK(status IN ('proposed','confirmed')),
  created_at VARCHAR(40) NOT NULL,
  PRIMARY KEY(metric_id,event_id)
);
CREATE TABLE IF NOT EXISTS feedback (
  id VARCHAR(36) PRIMARY KEY,
  company_id VARCHAR(36) NOT NULL REFERENCES companies(id),
  user_id VARCHAR(36) NOT NULL REFERENCES app_users(id),
  subject_type VARCHAR(20) NOT NULL CHECK(subject_type IN ('metric','event','dashboard')),
  subject_id VARCHAR(36),
  kind VARCHAR(30) NOT NULL CHECK(kind IN ('relevant','irrelevant','incorrect','missing_context','other')),
  comment TEXT,
  created_at VARCHAR(40) NOT NULL
);
CREATE TABLE IF NOT EXISTS discussions (
  id VARCHAR(36) PRIMARY KEY,
  company_id VARCHAR(36) NOT NULL REFERENCES companies(id),
  user_id VARCHAR(36) NOT NULL REFERENCES app_users(id),
  subject_type VARCHAR(20) NOT NULL CHECK(subject_type IN ('metric','event','dashboard')),
  subject_id VARCHAR(36),
  parent_id VARCHAR(36) REFERENCES discussions(id),
  body TEXT NOT NULL,
  created_at VARCHAR(40) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_company ON metric_definitions(company_id);
CREATE INDEX IF NOT EXISTS idx_observation_period ON metric_observations(metric_id,period_end);
CREATE INDEX IF NOT EXISTS idx_event_company_date ON business_events(company_id,published_on);
CREATE INDEX IF NOT EXISTS idx_feedback_company ON feedback(company_id,created_at);
CREATE INDEX IF NOT EXISTS idx_discussion_subject ON discussions(company_id,subject_type,subject_id);

CREATE TABLE IF NOT EXISTS source_policy_heads (
  company_id VARCHAR(36) PRIMARY KEY REFERENCES companies(id),
  revision INTEGER NOT NULL,
  updated_at VARCHAR(40) NOT NULL
);
CREATE TABLE IF NOT EXISTS news_sources (
  id VARCHAR(36) PRIMARY KEY,
  company_id VARCHAR(36) NOT NULL REFERENCES companies(id),
  uri TEXT NOT NULL,
  content_type VARCHAR(40) NOT NULL,
  source_role VARCHAR(20) NOT NULL CHECK(source_role IN ('external','bank')),
  classification VARCHAR(20) NOT NULL CHECK(classification IN ('public','synthetic','confidential')),
  title VARCHAR(240) NOT NULL,
  publisher VARCHAR(240) NOT NULL,
  published_at VARCHAR(40),
  article_hosts_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at VARCHAR(40) NOT NULL,
  UNIQUE(company_id,uri)
);
CREATE TABLE IF NOT EXISTS news_runs (
  id VARCHAR(36) PRIMARY KEY,
  company_id VARCHAR(36) NOT NULL REFERENCES companies(id),
  context_version INTEGER NOT NULL,
  policy_version INTEGER NOT NULL,
  status VARCHAR(20) NOT NULL,
  result_json TEXT,
  error_code VARCHAR(100),
  created_at VARCHAR(40) NOT NULL,
  finished_at VARCHAR(40)
);
CREATE TABLE IF NOT EXISTS news_documents (
  id VARCHAR(36) PRIMARY KEY,
  company_id VARCHAR(36) NOT NULL REFERENCES companies(id),
  source_id VARCHAR(36) NOT NULL REFERENCES news_sources(id),
  canonical_uri TEXT NOT NULL,
  title TEXT NOT NULL,
  publisher TEXT NOT NULL,
  author TEXT,
  published_at VARCHAR(40) NOT NULL,
  fetched_at VARCHAR(40) NOT NULL,
  latest_revision_ref TEXT NOT NULL,
  latest_hash VARCHAR(64) NOT NULL,
  excerpt TEXT NOT NULL,
  classification VARCHAR(20) NOT NULL,
  UNIQUE(company_id,source_id,canonical_uri)
);
CREATE TABLE IF NOT EXISTS news_document_revisions (
  id VARCHAR(36) PRIMARY KEY,
  document_id VARCHAR(36) NOT NULL REFERENCES news_documents(id),
  revision_ref TEXT NOT NULL UNIQUE,
  content_hash VARCHAR(64) NOT NULL,
  raw_sha256 VARCHAR(64),
  raw_ref TEXT,
  excerpt TEXT NOT NULL,
  fetched_at VARCHAR(40) NOT NULL
);
CREATE TABLE IF NOT EXISTS news_event_documents (
  event_id VARCHAR(36) NOT NULL REFERENCES business_events(id),
  document_id VARCHAR(36) NOT NULL REFERENCES news_documents(id),
  PRIMARY KEY(event_id,document_id)
);
CREATE INDEX IF NOT EXISTS idx_news_documents_company_date ON news_documents(company_id,published_at);

CREATE TABLE IF NOT EXISTS news_assessments (
  id VARCHAR(36) PRIMARY KEY,
  event_id VARCHAR(36) NOT NULL REFERENCES business_events(id),
  input_signature VARCHAR(128) NOT NULL,
  status VARCHAR(20) NOT NULL,
  execution_mode VARCHAR(20) NOT NULL,
  summary TEXT NOT NULL,
  candidate_json TEXT NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  UNIQUE(event_id,input_signature)
);
CREATE INDEX IF NOT EXISTS idx_assessment_event ON news_assessments(event_id,created_at);
