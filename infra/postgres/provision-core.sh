#!/bin/sh
# Bootstrap only: separate Core databases and non-superuser runtime/migration roles.
set -eu
: "${CORE_DB_PASSWORD:?Core app password is required}"
: "${CORE_MIGRATION_DB_PASSWORD:?Core migration password is required}"
psql --username "${PGUSER:-${POSTGRES_USER:-bootstrap_admin}}" --dbname postgres --set=ON_ERROR_STOP=1 \
  --set=app_password="$CORE_DB_PASSWORD" --set=migration_password="$CORE_MIGRATION_DB_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE core_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='core_owner')\gexec
SELECT format('CREATE ROLE core_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='core_app')\gexec
SELECT format('CREATE ROLE core_migrator LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', :'migration_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='core_migrator')\gexec
GRANT core_owner TO core_migrator;
SELECT 'CREATE DATABASE business_context_core OWNER core_owner'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='business_context_core')\gexec
SELECT 'CREATE DATABASE business_context_core_test OWNER core_owner'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='business_context_core_test')\gexec
REVOKE CONNECT ON DATABASE business_context_core,business_context_core_test FROM PUBLIC;
GRANT CONNECT ON DATABASE business_context_core,business_context_core_test TO core_app,core_migrator;
SQL
for core_database in business_context_core business_context_core_test; do
  psql --username "${PGUSER:-${POSTGRES_USER:-bootstrap_admin}}" --dbname "$core_database" --set=ON_ERROR_STOP=1 <<'SQL'
ALTER SCHEMA public OWNER TO core_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO core_app;
GRANT USAGE,CREATE ON SCHEMA public TO core_migrator;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO core_app;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO core_app;
ALTER DEFAULT PRIVILEGES FOR ROLE core_migrator IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO core_app;
ALTER DEFAULT PRIVILEGES FOR ROLE core_migrator IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO core_app;
SQL
done
