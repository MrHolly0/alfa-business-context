#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 \
  --set=app_password="$INTELLIGENCE_DB_PASSWORD" --set=migration_password="$INTELLIGENCE_MIGRATION_DB_PASSWORD" <<'SQL'
CREATE ROLE intelligence_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE intelligence_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE intelligence_migrator LOGIN PASSWORD :'migration_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT;
GRANT intelligence_owner TO intelligence_migrator;
ALTER DATABASE business_context_ai OWNER TO intelligence_owner;
CREATE DATABASE business_context_ai_test OWNER intelligence_owner;
REVOKE CONNECT ON DATABASE business_context_ai_test FROM PUBLIC;
GRANT CONNECT ON DATABASE business_context_ai_test TO intelligence_app,intelligence_migrator;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CONNECT ON DATABASE business_context_ai FROM PUBLIC;
GRANT CONNECT ON DATABASE business_context_ai TO intelligence_app,intelligence_migrator;
SQL
