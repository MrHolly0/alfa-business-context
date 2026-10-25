"""Persistence checks require a real PostgreSQL database dedicated to tests."""
import os
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest


@pytest.fixture(scope="session", autouse=True)
def dedicated_postgres_database():
    """Refuse accidental execution against the application's ordinary database."""
    databases = []
    for variable in ("INTELLIGENCE_DATABASE_URL", "INTELLIGENCE_MIGRATION_DATABASE_URL"):
        value = os.getenv(variable)
        if not value:
            raise pytest.UsageError(f"{variable} is required; use the PostgreSQL test setup")
        name = conninfo_to_dict(value).get("dbname", "")
        if not name.endswith("_test"):
            raise pytest.UsageError("PostgreSQL test database name must end with _test")
        databases.append(name)
    if len(set(databases)) != 1:
        raise pytest.UsageError("Application and migration test DSNs must name the same database")
    with psycopg.connect(os.environ["INTELLIGENCE_DATABASE_URL"]) as connection:
        actual = connection.execute("SELECT current_database()").fetchone()[0]
        if actual != databases[0]:
            raise pytest.UsageError("Unexpected PostgreSQL test database")


@pytest.fixture(autouse=True)
def isolated_storage_namespace(monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_STORAGE_NAMESPACE", "test-" + uuid4().hex)
