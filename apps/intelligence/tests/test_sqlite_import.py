"""Safety and PostgreSQL integration tests for the one-time SQLite importer."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from intelligence import sqlite_import


def _make_collection_source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE poll_state (
                tenant_id TEXT NOT NULL, source_id TEXT NOT NULL,
                next_poll TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                etag TEXT, last_modified TEXT, last_error TEXT, last_status INTEGER,
                lease_until TEXT, grant_fingerprint TEXT, lease_generation INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT, PRIMARY KEY (tenant_id, source_id)
            );
            CREATE TABLE raw_receipts (
                tenant_id TEXT NOT NULL, source_id TEXT NOT NULL, canonical_uri TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL, raw_ref TEXT NOT NULL, content_type TEXT NOT NULL,
                title TEXT, publisher TEXT, author TEXT, published_at TEXT, event_time TEXT,
                language TEXT, document_revision_ref TEXT, fetched_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (tenant_id, source_id, canonical_uri, raw_sha256)
            );
            INSERT INTO poll_state (tenant_id,source_id,next_poll,attempts,etag)
            VALUES ('tenant-x','feed-x','2026-10-25T10:00:00Z',2,'etag-1');
            INSERT INTO raw_receipts (
                tenant_id,source_id,canonical_uri,raw_sha256,raw_ref,content_type,
                title,fetched_at,metadata_json
            ) VALUES (
                'tenant-x','feed-x','https://example.invalid/a','sha256-a','blobs/a',
                'text/html','Synthetic fixture','2026-10-25T10:01:00Z','{}'
            );
        """)


def test_import_requires_explicit_quiescence_before_opening_source(tmp_path, monkeypatch):
    source = tmp_path / "legacy.sqlite3"
    source.write_bytes(b"not opened")
    monkeypatch.delenv("INTELLIGENCE_MIGRATION_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="quiesced_and_confirmed"):
        sqlite_import.import_database(source, "collection", tmp_path / "logical")
    assert source.read_bytes() == b"not opened"


def test_snapshot_hashes_and_reads_only_a_copy(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample VALUES (1, 'fixture')")
    before = sqlite_import._bundle_hash(source)
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    snapshot, snapshot_hash = sqlite_import._copy_snapshot(source, snapshot_dir)
    with closing(sqlite_import._sqlite_snapshot(snapshot)) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "fixture"
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    assert snapshot_hash == before
    assert sqlite_import._bundle_hash(source) == before


def test_source_integrity_and_foreign_keys_are_preflighted(tmp_path):
    source = tmp_path / "broken.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (parent_id INTEGER REFERENCES parent(id));
        """)
        # SQLite checks FK constraints on insert only when enforcement is enabled
        # for that connection; make a deliberately invalid legacy source.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO child VALUES (99)")
    with closing(sqlite_import._sqlite_snapshot(source)) as connection:
        with pytest.raises(ValueError, match="foreign_key_check_failed"):
            sqlite_import._inspect_source(connection)


def test_source_schema_rejects_unknown_columns_and_tables():
    target = {"documents": [{"name": "document_id", "nullable": False,
                              "default": None, "generated": False, "identity": False}]}
    with pytest.raises(ValueError, match="unknown_tables"):
        sqlite_import._validate_source_schema({"rogue": ["id"]}, {"rogue": 0}, target)
    with pytest.raises(ValueError, match="unknown_columns"):
        sqlite_import._validate_source_schema({"documents": ["document_id", "secret"]},
                                             {"documents": 0}, target)


def test_empty_legacy_table_may_omit_required_target_fields_but_nonempty_may_not():
    target = {"reservations": [{"name": "id", "nullable": False, "default": None,
                                "generated": False, "identity": False}]}
    assert sqlite_import._validate_source_schema({"reservations": []},
        {"reservations": 0}, target) == {"reservations": []}
    with pytest.raises(ValueError, match="missing_required_columns"):
        sqlite_import._validate_source_schema({"reservations": []},
            {"reservations": 1}, target)


def test_numeric_source_float_is_rejected_instead_of_claiming_exactness():
    numeric = {"data_type": "numeric", "udt_name": "numeric"}
    with pytest.raises(ValueError, match="source_money_requires_exact_representation"):
        sqlite_import._convert(0.1, numeric)
    assert sqlite_import._convert("0.10", numeric).as_tuple().exponent == -2


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_MIGRATION_DATABASE_URL"),
                    reason="requires the isolated PostgreSQL migration test service")
def test_sequence_adjustment_never_rewinds_a_shared_sequence():
    import psycopg
    from intelligence.storage import connection_url

    with psycopg.connect(connection_url(migration=True)) as connection:
        with connection.transaction():
            connection.execute("CREATE TEMP TABLE import_sequence_fixture (id BIGINT GENERATED BY DEFAULT AS IDENTITY)")
            connection.execute("INSERT INTO pg_temp.import_sequence_fixture(id) OVERRIDING SYSTEM VALUE VALUES (500)")
            sequence = connection.execute(
                "SELECT pg_get_serial_sequence('pg_temp.import_sequence_fixture','id')").fetchone()[0]
            connection.execute("SELECT setval(%s::regclass,900,true)", (sequence,))
            sqlite_import._advance_sequences(connection, "pg_temp", {
                "import_sequence_fixture": [{"name": "id", "identity": True, "default": None}]
            })
            next_value = connection.execute("SELECT nextval(%s::regclass)", (sequence,)).fetchone()[0]
            assert next_value == 901


def test_legacy_outbox_zero_deadline_is_derived_from_creation_time():
    assert sqlite_import._legacy_outbox_deadline({"created_at": 1000}) == 87400
    assert sqlite_import._legacy_outbox_deadline(
        {"created_at": "2026-10-25T10:00:00Z"}
    ) == sqlite_import._legacy_outbox_deadline(
        {"created_at": "2026-10-25T10:00:00+00:00"}
    )


def test_legacy_outbox_deadline_requires_creation_time():
    with pytest.raises(ValueError, match="created_at_required"):
        sqlite_import._legacy_outbox_deadline({})


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_MIGRATION_DATABASE_URL"),
                    reason="requires the isolated PostgreSQL migration test service")
def test_postgres_import_is_atomic_preserves_rows_and_is_idempotent(tmp_path, monkeypatch):
    source = tmp_path / "collection.sqlite3"
    _make_collection_source(source)
    state_root = tmp_path / "state"
    state_root.mkdir()
    logical_path = state_root / "collection.sqlite3"
    monkeypatch.setenv("INTELLIGENCE_STATE_DIR", str(state_root))

    first = sqlite_import.import_database(source, "collection", logical_path,
                                          confirm_quiesced=True)
    assert first["status"] == "imported"
    assert first["counts"] == {"poll_state": 1, "raw_receipts": 1}
    assert first["invariants"]["ids_and_references_preserved"] is True

    repeated = sqlite_import.import_database(source, "collection", logical_path,
                                             confirm_quiesced=True)
    assert repeated["status"] == "already_imported"
    assert repeated["import_id"] == first["import_id"]
    assert repeated["counts"] == first["counts"]


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_MIGRATION_DATABASE_URL"),
                    reason="requires the isolated PostgreSQL migration test service")
def test_postgres_import_rejects_nonempty_scope(tmp_path, monkeypatch):
    from intelligence.storage import Database

    source = tmp_path / "collection.sqlite3"
    _make_collection_source(source)
    state_root = tmp_path / "state"
    state_root.mkdir()
    logical_path = state_root / "collection.sqlite3"
    monkeypatch.setenv("INTELLIGENCE_STATE_DIR", str(state_root))
    database = Database("collection", logical_path)
    with database.connection() as db:
        db.execute("INSERT INTO poll_state (tenant_id,source_id,next_poll) VALUES (%s,%s,%s)",
                   ("tenant-existing", "existing", "2026-10-25T10:00:00Z"))

    with pytest.raises(ValueError, match="namespace_not_empty"):
        sqlite_import.import_database(source, "collection", logical_path,
                                      confirm_quiesced=True)


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_MIGRATION_DATABASE_URL"),
                    reason="requires the isolated PostgreSQL migration test service")
def test_postgres_import_converts_legacy_outbox_retry_deadline(tmp_path, monkeypatch):
    from intelligence.storage import Database

    source = tmp_path / "artifacts.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("""CREATE TABLE outbox (
            message_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL,
            created_at REAL NOT NULL
        )""")
        connection.execute("""INSERT INTO outbox
            (message_id,job_id,payload,created_at)
            VALUES ('message-1','job-1','{}',1000)""")
    state_root = tmp_path / "state"
    state_root.mkdir()
    logical_path = state_root / "artifacts.sqlite3"
    monkeypatch.setenv("INTELLIGENCE_STATE_DIR", str(state_root))

    result = sqlite_import.import_database(source, "artifacts", logical_path,
                                           confirm_quiesced=True)
    assert result["status"] == "imported"
    database = Database("artifacts", logical_path)
    with database.connection() as db:
        row = db.execute("SELECT retry_deadline FROM outbox WHERE message_id=%s",
                         ("message-1",)).fetchone()
    assert row[0] == 87400


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_MIGRATION_DATABASE_URL"),
                    reason="requires the isolated PostgreSQL migration test service")
def test_postgres_import_marks_legacy_claim_reservations_uncertain(tmp_path, monkeypatch):
    from intelligence.storage import Database

    source = tmp_path / "events.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("""CREATE TABLE claim_extraction_reservations (
            tenant_id TEXT NOT NULL, reservation_id TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id,reservation_id)
        )""")
        connection.execute("""INSERT INTO claim_extraction_reservations
            VALUES ('tenant-x','reservation-1','started','2026-10-25T10:00:00Z')""")
    state_root = tmp_path / "state"
    state_root.mkdir()
    logical_path = state_root / "events.sqlite3"
    monkeypatch.setenv("INTELLIGENCE_STATE_DIR", str(state_root))

    result = sqlite_import.import_database(source, "events", logical_path,
                                           confirm_quiesced=True)
    assert result["compatibility_transforms"] == [
        "claim_extraction_reservations: marked uncertain with expired lease and legacy owner"
    ]
    with Database("events", logical_path).connection() as db:
        row = db.execute("""SELECT status,lease_until,owner_token
            FROM claim_extraction_reservations WHERE tenant_id=%s AND reservation_id=%s""",
            ("tenant-x", "reservation-1")).fetchone()
    assert row["status"] == "uncertain"
    assert row["lease_until"] == "1970-01-01T00:00:00.000000Z"
    assert row["owner_token"] == "legacy-imported"
