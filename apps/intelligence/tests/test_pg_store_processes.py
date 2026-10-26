"""PostgreSQL concurrency, rollback, and namespace isolation integration checks."""
import multiprocessing
import os
import uuid

import psycopg
import pytest

from intelligence.artifacts import ArtifactStore, payload_hash
from intelligence.corpus import CorpusStore
from intelligence.events import EventStore
from intelligence.storage import Database


def _run_parallel(operation, arguments):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(arguments))
    outcomes = context.Queue()
    processes = [context.Process(target=operation, args=(*args, barrier, outcomes)) for args in arguments]
    for process in processes:
        process.start()
    results = [outcomes.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    return results


def _ingest_document(path, document, barrier, outcomes):
    store = CorpusStore(path)
    barrier.wait(timeout=20)
    outcomes.put(store.ingest(document))


def _ingest_publication(path, publication, barrier, outcomes):
    store = EventStore(path)
    barrier.wait(timeout=20)
    outcomes.put(store.ingest(publication))


def _save_candidate(path, request_id, payload, barrier, outcomes):
    store = ArtifactStore(path)
    barrier.wait(timeout=20)
    outcomes.put(store.save_candidate("tenant-process", request_id, payload_hash(payload), payload))


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_DATABASE_URL"), reason="Postgres integration DSN is not configured")
def test_independent_processes_replay_one_canonical_corpus_revision(tmp_path):
    path = tmp_path / "corpus.sqlite3"  # logical database identity, not a SQLite database
    document = {
        "tenant_id": "test-process-corpus",
        "source_id": "test-feed",
        "source_role": "external",
        "canonical_uri": "https://source.example/article/1",
        "title": "Проверка логистического события",
        "content": "Повторная запись должна сохранить единственную версию документа в PostgreSQL.",
        "content_type": "text/plain",
        "published_at": "2026-10-25T09:00:00Z",
        "fetched_at": "2026-10-25T09:05:00Z",
        "synthetic": True,
        "data_classification": "synthetic",
    }
    results = _run_parallel(_ingest_document, [(path, document)] * 2)

    assert len({item["document_id"] for item in results}) == 1
    assert [item["revision"] for item in results] == [1, 1]
    with CorpusStore(path)._connect() as db:
        assert db.execute("SELECT count(*) FROM ai_corpus.documents WHERE tenant_id=%s", (document["tenant_id"],)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM ai_corpus.revisions WHERE document_id=%s", (results[0]["document_id"],)).fetchone()[0] == 1


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_DATABASE_URL"), reason="Postgres integration DSN is not configured")
def test_independent_urls_with_near_identical_text_share_one_event(tmp_path):
    path = tmp_path / "events.sqlite3"  # logical database identity, not a SQLite database
    tenant = "test-process-events"
    content = (
        "The regional logistics authority reports a temporary restriction on cargo movement through the northern port. "
        "Operators should confirm the affected schedules, alternative routes, and expected duration with the port office. "
        "The notice applies to commercial freight services while inspection work is underway."
    )
    near_copy = content.replace("temporary restriction", "short temporary restriction", 1)
    published = "2026-10-25T09:00:00Z"
    publications = [
        {
            "tenant_id": tenant, "source_id": "publisher-a", "source_role": "external",
            "canonical_uri": "https://publisher-a.example/news/port-update",
            "title": "Port cargo notice from publisher A", "content": content,
            "content_type": "text/plain", "published_at": published,
            "fetched_at": "2026-10-25T09:05:00Z", "synthetic": True,
            "data_classification": "synthetic",
        },
        {
            "tenant_id": tenant, "source_id": "publisher-b", "source_role": "external",
            "canonical_uri": "https://publisher-b.example/brief/port-update",
            "title": "Port cargo notice from publisher B", "content": near_copy,
            "content_type": "text/plain", "published_at": published,
            "fetched_at": "2026-10-25T09:06:00Z", "synthetic": True,
            "data_classification": "synthetic",
        },
    ]
    results = _run_parallel(_ingest_publication, [(path, item) for item in publications])

    assert len({item["event_id"] for item in results}) == 1
    with EventStore(path)._connect() as db:
        assert db.execute("SELECT count(*) FROM ai_events.events WHERE tenant_id=%s", (tenant,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM ai_events.publications WHERE tenant_id=%s", (tenant,)).fetchone()[0] == 2


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_DATABASE_URL"), reason="Postgres integration DSN is not configured")
def test_independent_processes_replay_candidate_once(tmp_path):
    path = tmp_path / "artifacts.sqlite3"  # logical database identity, not a SQLite database
    request_id = "request-" + uuid.uuid4().hex
    payload = {"request_id": request_id, "status": "ready", "candidate": {"revision": 1}}
    results = _run_parallel(_save_candidate, [(path, request_id, payload)] * 2)

    assert results == [payload, payload]
    with ArtifactStore(path)._db() as db:
        assert db.execute(
            "SELECT count(*) FROM ai_artifacts.candidates WHERE tenant_id=%s AND request_id=%s",
            ("tenant-process", request_id),
        ).fetchone()[0] == 1


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_DATABASE_URL"), reason="Postgres integration DSN is not configured")
def test_exception_after_insert_rolls_back_database_transaction(tmp_path):
    store = ArtifactStore(tmp_path / "rollback.sqlite3")
    tenant_id, request_id = "tenant-rollback", "request-rollback-" + uuid.uuid4().hex
    digest = payload_hash({"status": "ready"})

    with pytest.raises(RuntimeError, match="injected_failure"):
        with store._db() as db:
            db.lock(key="write")
            db.execute(
                """INSERT INTO ai_artifacts.candidates(tenant_id,request_id,payload_hash,response)
                   VALUES(%s,%s,%s,%s)""",
                (tenant_id, request_id, digest, '{"status":"ready"}'),
            )
            raise RuntimeError("injected_failure")

    assert store.get_candidate(tenant_id, request_id, digest) is None


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_DATABASE_URL"), reason="Postgres integration DSN is not configured")
def test_rls_isolates_store_namespaces_and_app_role_cannot_create_tables(tmp_path):
    path = tmp_path / "namespace.sqlite3"
    namespace_a = "test-isolation-a-" + uuid.uuid4().hex
    namespace_b = "test-isolation-b-" + uuid.uuid4().hex
    store_a = ArtifactStore(path, namespace=namespace_a)
    store_b = ArtifactStore(path, namespace=namespace_b)
    payload = {"status": "ready"}
    digest = payload_hash(payload)

    store_a.save_candidate("tenant-a", "request-a", digest, payload)
    store_b.save_candidate("tenant-a", "request-a", digest, payload)
    assert store_a.get_candidate("tenant-a", "request-a", digest) == payload
    assert store_b.get_candidate("tenant-a", "request-a", digest) == payload
    with store_a._db() as db:
        assert db.execute(
            "SELECT count(*) FROM ai_artifacts.candidates WHERE tenant_id=%s AND request_id=%s",
            ("tenant-a", "request-a"),
        ).fetchone()[0] == 1
        identity = db.execute(
            "SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert identity["rolsuper"] is False and identity["rolbypassrls"] is False

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with store_a._db() as db:
            db.execute("CREATE TABLE ai_artifacts.test_application_ddl_denied(id integer)")
