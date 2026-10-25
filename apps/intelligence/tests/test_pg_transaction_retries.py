"""The storage retry helper retries only the DB callback after rollback."""
from psycopg.errors import DeadlockDetected, SerializationFailure
import pytest

from intelligence.storage import Database


def _database(tmp_path):
    return Database("workflow", tmp_path / "retry-test.pg")


def _insert(db, request_id):
    db.execute(
        "INSERT INTO workflow_stages(tenant,request,digest,stage,payload) VALUES(%s,%s,%s,%s,%s)",
        ("tenant-retry-check", request_id, "d" * 64, "test", "{}"),
    )


def _count(database, request_id):
    with database.connection() as db:
        return db.execute(
            "SELECT count(*) FROM workflow_stages WHERE tenant=%s AND request=%s",
            ("tenant-retry-check", request_id),
        ).fetchone()[0]


def test_transaction_rolls_back_failed_attempts_then_commits_third(tmp_path):
    database = _database(tmp_path)
    attempts = []
    external_work = []

    def operation(db):
        attempts.append(len(attempts) + 1)
        _insert(db, "check-retry-success")
        if len(attempts) < 3:
            raise SerializationFailure("injected serialization failure")
        return "committed"

    assert database.transaction(operation, attempts=3) == "committed"
    assert attempts == [1, 2, 3]
    assert external_work == []
    assert _count(database, "check-retry-success") == 1


def test_transaction_stops_after_three_retryable_failures_and_leaves_no_rows(tmp_path):
    database = _database(tmp_path)
    attempts = []

    def operation(db):
        attempts.append(len(attempts) + 1)
        _insert(db, "check-retry-exhausted")
        raise DeadlockDetected("injected deadlock")

    with pytest.raises(DeadlockDetected, match="injected deadlock"):
        database.transaction(operation, attempts=3)
    assert attempts == [1, 2, 3]
    assert _count(database, "check-retry-exhausted") == 0


def test_retry_count_must_be_positive_and_bounded(tmp_path):
    import pytest
    from intelligence.storage import Database
    database = Database("workflow", tmp_path / "invalid-retries")
    for value in (0, -1, True, 6, 1.5):
        with pytest.raises(ValueError, match="transaction_attempts"):
            database.transaction(lambda db: None, attempts=value)


def test_migration_rerun_does_not_lock_live_application_tables(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from intelligence.database_cli import migrate
    from intelligence.storage import Database
    database = Database("artifacts", tmp_path / "migration-noop")
    with database.connection() as db:
        db.execute("INSERT INTO candidates(tenant_id,request_id,payload_hash,response) VALUES(%s,%s,%s,%s)",
                   ("tenant", "check-live-writer", "digest", "{}"))
        # This live writer holds RowExclusiveLock until the surrounding commit.
        # A migration rerun without new files must not request AccessExclusiveLock.
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert workers.submit(migrate).result(timeout=2) == {"status": "migrated", "applied": []}
