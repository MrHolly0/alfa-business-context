"""PostgreSQL-only persistence boundary; Core uses a separate service/database.

Paths remain logical store identities for compatibility/import, not SQLite files.
All connections belong to a bounded per-process pool; transactions never replay
external effects. Monetary values are returned as Decimal, not float.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime,date,timezone
import hashlib,os,random,re,threading,time
from pathlib import Path
from typing import Callable
import psycopg
from psycopg_pool import ConnectionPool

COMPONENTS=frozenset({'corpus','collection','analysts','company_analysts','events','tournaments','budget','artifacts','workflow'})
_pools={};_guard=threading.Lock()

def connection_url(*,migration=False):
    value=os.getenv('INTELLIGENCE_MIGRATION_DATABASE_URL' if migration else 'INTELLIGENCE_DATABASE_URL','')
    if not value:raise RuntimeError('INTELLIGENCE_DATABASE_URL_required' if not migration else 'INTELLIGENCE_MIGRATION_DATABASE_URL_required')
    return value

def identifier(value):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,50}',value):raise ValueError('invalid_database_identifier')
    return value

def schema_for(component):
    if component not in COMPONENTS:raise ValueError('invalid_storage_component')
    return identifier('ai_'+component)

def logical_namespace(path,component):
    base=os.getenv('INTELLIGENCE_STORAGE_NAMESPACE','main')
    if not base or len(base)>128:raise ValueError('invalid_storage_namespace')
    # One provider budget account for all state directories in a deployment.
    if component=='budget':return base+':shared-budget'
    root=Path(os.getenv('INTELLIGENCE_STATE_DIR',str(Path(__file__).resolve().parents[1]/'.state'))).resolve()
    target=Path(path).resolve()
    if component=='events' and target.parent.name=='historical-events' and re.fullmatch(r'[a-f0-9]{64}',target.stem):
        return logical_namespace(target.parent.parent/'events.sqlite3','events')+':history:'+target.stem
    try:key=target.relative_to(root).as_posix()
    except ValueError:key=target.as_posix()
    return base+':'+hashlib.sha256(key.encode()).hexdigest()

def _value(value):
    if isinstance(value,datetime):return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    if isinstance(value,date):return value.isoformat()
    return value

class Record(dict):
    def __getitem__(self,key):
        return tuple(self.values())[key] if isinstance(key,int) else super().__getitem__(key)

def _row_factory(cursor):
    fields=[column.name for column in cursor.description] if cursor.description else []
    return lambda values:Record((key,_value(value)) for key,value in zip(fields,values) if key!='store_key')

class Session:
    def __init__(self,connection,component,namespace):self.connection,self.component,self.namespace=connection,component,namespace
    def execute(self,query,params=None):
        if params is not None:
            params=tuple(int(item) if isinstance(item,bool) else item for item in params)
        return self.connection.execute(query,params)
    def executemany(self,query,params):
        cursor=self.connection.cursor();cursor.executemany(query,params);return cursor
    def __iter__(self):return iter(self.connection)
    def lock(self,key='write'):
        raw=(self.component+'\0'+self.namespace+'\0'+str(key)).encode()
        lock_id=int.from_bytes(hashlib.sha256(raw).digest()[:8],'big',signed=True)
        self.connection.execute('SELECT pg_advisory_xact_lock(%s)',(lock_id,))
    def commit(self):self.connection.commit()
    def rollback(self):self.connection.rollback()


def _pool():
    key=(os.getpid(),connection_url())
    with _guard:
        if key not in _pools:
            maximum=int(os.getenv('INTELLIGENCE_DB_POOL_MAX','4'))
            if not 1<=maximum<=32:raise ValueError('database_pool_max_out_of_range')
            pool=ConnectionPool(conninfo=key[1],min_size=0,max_size=maximum,
                timeout=float(os.getenv('INTELLIGENCE_DB_POOL_TIMEOUT','5')),max_waiting=64,
                kwargs={'autocommit':True,'row_factory':_row_factory,'connect_timeout':5,
                        'application_name':f'business-context-intelligence:{os.getpid()}'},
                reconnect_timeout=5,check=ConnectionPool.check_connection,open=True)
            _pools[key]=pool
        return _pools[key]

def close_pools():
    with _guard:
        pools=list(_pools.values());_pools.clear()
    for pool in pools:pool.close()

class Database:
    def __init__(self,component,path,namespace=None):
        self.component=component;self.schema=schema_for(component);self.path=str(path)
        self.namespace=namespace if namespace is not None else logical_namespace(path,component)
        if not isinstance(self.namespace,str) or not self.namespace or len(self.namespace)>512:raise ValueError('invalid_storage_namespace')
    @contextmanager
    def connection(self):
        with _pool().connection() as connection:
            with connection.transaction():
                identity=connection.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
                if identity['rolsuper'] or identity['rolbypassrls']:raise RuntimeError('privileged_database_role_forbidden')
                if connection.execute("SELECT pg_has_role(current_user,'intelligence_owner','member')").fetchone()[0]:raise RuntimeError('migration_database_role_forbidden_at_runtime')
                connection.execute("SELECT set_config('search_path',%s,true)",(self.schema+',pg_catalog',))
                connection.execute("SELECT set_config('intelligence.store_key',%s,true)",(self.namespace,))
                connection.execute("SELECT set_config('lock_timeout',%s,true)",('5000ms',))
                connection.execute("SELECT set_config('statement_timeout',%s,true)",('30000ms',))
                try:
                    migration=connection.execute("SELECT count(*) FROM ai_meta.schema_migrations").fetchone()[0]
                except psycopg.errors.UndefinedTable as exc:raise RuntimeError('database_migrations_required') from exc
                if not migration:raise RuntimeError('database_migrations_required')
                yield Session(connection,self.component,self.namespace)
    def transaction(self,operation:Callable,*,attempts=3):
        """Finite retries for a DB-only callback. Never pass HTTP/model work here."""
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError('transaction_attempts_must_be_between_1_and_5')
        for attempt in range(attempts):
            try:
                with self.connection() as db:return operation(db)
            except (psycopg.errors.SerializationFailure,psycopg.errors.DeadlockDetected):
                if attempt+1==attempts:raise
                time.sleep(random.uniform(.01,.04)*(2**attempt))


# Pools/threads cannot be reused after fork. Leave inherited sockets untouched
# in the child; its own process creates a new bounded pool.
_inherited_pools=[]
def _after_fork():
    global _pools,_guard
    _inherited_pools.extend(_pools.values())
    _pools={};_guard=threading.Lock()
if hasattr(os,'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)
import atexit
atexit.register(close_pools)
