"""Explicit PostgreSQL migrations and read-only SQLite import entry points."""
from __future__ import annotations
import argparse,hashlib,json,os
from pathlib import Path
import psycopg
from psycopg import sql
from .storage import connection_url,COMPONENTS
MIGRATIONS=Path(__file__).with_name('migrations')

def migrate():
    applied=[]
    with psycopg.connect(connection_url(migration=True),autocommit=True) as connection:
        with connection.transaction():
            connection.execute('SET ROLE intelligence_owner')
            connection.execute('SELECT pg_advisory_xact_lock(%s)',(713046091001,))
            connection.execute("SET LOCAL lock_timeout='5s'")
            connection.execute("SET LOCAL statement_timeout='30s'")
            # A no-op rerun must not take DDL locks on live application tables.
            # Validate every recorded checksum before deciding there is no work.
            if connection.execute("SELECT to_regclass('ai_meta.schema_migrations')").fetchone()[0]:
                recorded=dict(connection.execute('SELECT version,checksum FROM ai_meta.schema_migrations').fetchall())
                files={path.stem:path for path in MIGRATIONS.glob('*.sql')}
                if set(recorded)-set(files):raise ValueError('database_has_newer_migrations')
                for version,checksum in recorded.items():
                    if hashlib.sha256(files[version].read_text().encode()).hexdigest()!=checksum:
                        raise ValueError('migration_checksum_mismatch:'+version)
                if set(recorded)==set(files):return {'status':'migrated','applied':[]}
            connection.execute('CREATE SCHEMA IF NOT EXISTS ai_meta')
            connection.execute('CREATE TABLE IF NOT EXISTS ai_meta.schema_migrations(version TEXT PRIMARY KEY,checksum TEXT NOT NULL,applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            connection.execute("CREATE TABLE IF NOT EXISTS ai_meta.application_metadata(application TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO ai_meta.application_metadata VALUES('business-context-intelligence') ON CONFLICT DO NOTHING")
            connection.execute("CREATE TABLE IF NOT EXISTS ai_meta.imports(import_id TEXT PRIMARY KEY,component TEXT NOT NULL,store_key TEXT NOT NULL,source_hash TEXT NOT NULL,manifest TEXT NOT NULL CHECK(jsonb_typeof(manifest::jsonb)='object'),imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            for component in sorted(COMPONENTS):
                connection.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier('ai_'+component)))
            for path in sorted(MIGRATIONS.glob('*.sql')):
                contents=path.read_text();checksum=hashlib.sha256(contents.encode()).hexdigest()
                prior=connection.execute('SELECT checksum FROM ai_meta.schema_migrations WHERE version=%s',(path.stem,)).fetchone()
                if prior:
                    if prior[0]!=checksum:raise ValueError('migration_checksum_mismatch:'+path.stem)
                    continue
                connection.execute(contents)
                connection.execute('INSERT INTO ai_meta.schema_migrations(version,checksum) VALUES(%s,%s)',(path.stem,checksum))
                applied.append(path.stem)
            for component in sorted(COMPONENTS):
                schema='ai_'+component
                if not connection.execute('SELECT 1 FROM information_schema.schemata WHERE schema_name=%s',(schema,)).fetchone():continue
                connection.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO intelligence_app').format(sql.Identifier(schema)))
                tables=connection.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s",(schema,)).fetchall()
                for (table,) in tables:
                    qualified=sql.Identifier(schema,table)
                    connection.execute(sql.SQL('ALTER TABLE {} ENABLE ROW LEVEL SECURITY').format(qualified))
                    connection.execute(sql.SQL('ALTER TABLE {} FORCE ROW LEVEL SECURITY').format(qualified))
                    connection.execute(sql.SQL('DROP POLICY IF EXISTS storage_scope ON {}').format(qualified))
                    connection.execute(sql.SQL("CREATE POLICY storage_scope ON {} USING(store_key=current_setting('intelligence.store_key',true)) WITH CHECK(store_key=current_setting('intelligence.store_key',true))").format(qualified))
                    connection.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON {} TO intelligence_app').format(qualified))
                connection.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO intelligence_app').format(sql.Identifier(schema)))
            connection.execute('GRANT USAGE ON SCHEMA ai_meta TO intelligence_app')
            connection.execute('GRANT SELECT ON ai_meta.schema_migrations,ai_meta.application_metadata TO intelligence_app')
    return {'status':'migrated','applied':applied}

def main(argv=None):
    parser=argparse.ArgumentParser(description='PostgreSQL migrations and controlled legacy import')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('migrate')
    check=commands.add_parser('check')
    recovery=commands.add_parser('recover-execution')
    recovery.add_argument('--state-dir',required=True)
    recovery.add_argument('--tenant',required=True)
    recovery.add_argument('--request',required=True)
    recovery.add_argument('--confirm-stopped',action='store_true',required=True)
    importing=commands.add_parser('import-sqlite')
    importing.add_argument('--source',required=True)
    importing.add_argument('--component',choices=sorted(COMPONENTS),required=True)
    importing.add_argument('--logical-path',required=True)
    importing.add_argument('--confirm-quiesced',action='store_true')
    importing.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    if args.command=='migrate':result=migrate()
    elif args.command=='recover-execution':
        from .workflow import WorkflowState
        state=WorkflowState(Path(args.state_dir)/'workflow.sqlite3')
        state.recover_execution(args.tenant,args.request)
        result={'status':'execution_recovery_authorized'}
    elif args.command=='check':
        from .storage import Database
        with Database('workflow','database-check').connection() as db:
            row=db.execute("SELECT current_user,version(),current_database()").fetchone()
            result={'status':'connected','role':row[0],'version':row[1],'database':row[2]}
    else:
        from .sqlite_import import import_database
        result=import_database(args.source,args.component,args.logical_path,confirm_quiesced=args.confirm_quiesced,dry_run=args.dry_run)
    print(json.dumps(result,ensure_ascii=False,default=str))
    return 0
if __name__=='__main__':raise SystemExit(main())
