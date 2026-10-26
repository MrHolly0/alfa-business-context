"""Controlled, one-time import from a quiesced legacy SQLite store.

The source database is only ever read through a temporary read-only snapshot.
The PostgreSQL import and its manifest are committed in one transaction.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any

import psycopg
from psycopg import sql

from .storage import COMPONENTS, Database, connection_url


_CHUNK_SIZE = 500


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _source_files(source: Path) -> list[Path]:
    if source.is_symlink() or not source.is_file():
        raise ValueError("sqlite_source_must_be_regular_file")
    files = [source]
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            if sidecar.is_symlink() or not sidecar.is_file():
                raise ValueError("sqlite_sidecar_must_be_regular_file")
            files.append(sidecar)
    return files


def _bundle_hash(source: Path) -> str:
    """Hash the main file and SQLite sidecars without opening the source DB."""
    digest = hashlib.sha256()
    for path in _source_files(source):
        relative = path.name[len(source.name):] or "."
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        size = path.stat().st_size
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _copy_snapshot(source: Path, directory: Path) -> tuple[Path, str]:
    before = _bundle_hash(source)
    target = directory / source.name
    for path in _source_files(source):
        snapshot = Path(str(target) + path.name[len(source.name):])
        with path.open("rb") as src, snapshot.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        snapshot.chmod(0o600)
    after = _bundle_hash(source)
    if before != after:
        raise ValueError("sqlite_source_changed_during_snapshot")
    return target, before


def _sqlite_snapshot(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _inspect_source(connection: sqlite3.Connection) -> tuple[dict[str, list[str]], dict[str, int]]:
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise ValueError("sqlite_source_integrity_check_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("sqlite_source_foreign_key_check_failed")

    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    columns: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for table in tables:
        quoted = _quote_sqlite_identifier(table)
        columns[table] = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
        counts[table] = connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
    return columns, counts


def _introspect_target(connection, component: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, set[str]]]:
    schema = Database(component, "sqlite-import-introspection").schema
    rows = connection.execute("""SELECT table_name,column_name,is_nullable,column_default,
        is_generated,is_identity,data_type,udt_name
        FROM information_schema.columns WHERE table_schema=%s
        ORDER BY table_name,ordinal_position""", (schema,)).fetchall()
    tables: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        tables[row[0]].append({
            "name": row[1], "nullable": row[2] == "YES", "default": row[3],
            "generated": row[4] != "NEVER", "identity": row[5] == "YES",
            "data_type": row[6], "udt_name": row[7],
        })
    dependencies: dict[str, set[str]] = {name: set() for name in tables}
    foreign_keys = connection.execute("""SELECT child.relname,parent.relname
        FROM pg_constraint constraint_row
        JOIN pg_class child ON child.oid=constraint_row.conrelid
        JOIN pg_namespace child_schema ON child_schema.oid=child.relnamespace
        JOIN pg_class parent ON parent.oid=constraint_row.confrelid
        JOIN pg_namespace parent_schema ON parent_schema.oid=parent.relnamespace
        WHERE constraint_row.contype='f' AND child_schema.nspname=%s
          AND parent_schema.nspname=%s""", (schema, schema)).fetchall()
    for child, parent in foreign_keys:
        if child != parent:
            dependencies.setdefault(child, set()).add(parent)
    return dict(tables), dependencies


def _validate_source_schema(source_columns: dict[str, list[str]],
                            source_counts: dict[str, int],
                            target_columns: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    unknown_tables = sorted(set(source_columns) - set(target_columns))
    if unknown_tables:
        raise ValueError("sqlite_source_has_unknown_tables:" + ",".join(unknown_tables))
    source_set: dict[str, set[str]] = {}
    for table, columns in source_columns.items():
        target = target_columns[table]
        target_by_name = {column["name"]: column for column in target}
        transferable = [name for name in columns if name != "store_key"
                        and not target_by_name.get(name, {}).get("generated", False)]
        unknown_columns = sorted(set(transferable) - set(target_by_name))
        if unknown_columns:
            raise ValueError(f"sqlite_source_has_unknown_columns:{table}:" + ",".join(unknown_columns))
        source_set[table] = set(transferable)
        compatible_legacy_missing = (
            {"lease_until", "owner_token"}
            if table == "claim_extraction_reservations" else set()
        )
        missing = [column["name"] for column in target
                   if column["name"] != "store_key"
                   and column["name"] not in source_set[table]
                   and not column["nullable"] and column["default"] is None
                   and not column["generated"] and not column["identity"]
                   and column["name"] not in compatible_legacy_missing]
        if missing and source_counts.get(table, 0) > 0:
            raise ValueError(f"sqlite_source_missing_required_columns:{table}:" + ",".join(missing))
    # A newly introduced target table absent in SQLite has no rows to import.
    return {table: sorted(columns) for table, columns in source_set.items()}


def _table_order(tables: set[str], dependencies: dict[str, set[str]]) -> list[str]:
    incoming = {table: set(dependencies.get(table, set())) & tables for table in tables}
    ready = deque(sorted(table for table, parents in incoming.items() if not parents))
    ordered: list[str] = []
    while ready:
        table = ready.popleft()
        ordered.append(table)
        for child in sorted(incoming):
            if table in incoming[child]:
                incoming[child].remove(table)
                if not incoming[child] and child not in ordered and child not in ready:
                    ready.append(child)
    if len(ordered) != len(tables):
        raise ValueError("postgres_target_has_nondeferrable_import_cycle")
    return ordered


def _target_counts(connection, component: str, table_names: list[str], store_key: str) -> dict[str, int]:
    schema = Database(component, "sqlite-import-counts").schema
    counts = {}
    for table in table_names:
        query = sql.SQL("SELECT count(*) FROM {}.{} WHERE store_key=%s").format(
            sql.Identifier(schema), sql.Identifier(table))
        counts[table] = connection.execute(query, (store_key,)).fetchone()[0]
    return counts


def _convert(value: Any, target_column: dict[str, Any]) -> Any:
    if value is None:
        return None
    if target_column["data_type"] in {"numeric", "decimal"} or target_column["udt_name"] == "numeric":
        if isinstance(value, float):
            raise ValueError("source_money_requires_exact_representation")
        try:
            return value if isinstance(value, Decimal) else Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("sqlite_source_has_invalid_numeric_value") from exc
    if target_column["data_type"] == "timestamp with time zone":
        if isinstance(value, (int, float, Decimal)):
            try:
                micros = int(Decimal(str(value)) * Decimal(1_000_000))
            except InvalidOperation as exc:
                raise ValueError("sqlite_source_has_invalid_timestamp_value") from exc
            return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("sqlite_source_has_invalid_timestamp_value") from exc
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return value


def _copy_table(source: sqlite3.Connection, target, schema: str, table: str,
                columns: list[str], target_columns: dict[str, list[dict[str, Any]]]) -> int:
    if not columns:
        return 0
    quoted = _quote_sqlite_identifier(table)
    target_defs = {column["name"]: column for column in target_columns[table]}
    synthesized_retry_deadline = (
        table == "outbox" and "retry_deadline" not in columns
        and "retry_deadline" in target_defs
    )
    inherited_revision_fields = (
        [name for name in ("publisher", "language")
         if table == "revisions" and name not in columns and name in target_defs]
    )
    legacy_reservation_fields = (
        [name for name in ("lease_until", "owner_token")
         if table == "claim_extraction_reservations" and name not in columns
         and name in target_defs]
    )
    insert_columns = columns + inherited_revision_fields
    insert_columns.extend(legacy_reservation_fields)
    if synthesized_retry_deadline:
        insert_columns.append("retry_deadline")
    statement = sql.SQL("INSERT INTO {}.{} ({})").format(
        sql.Identifier(schema), sql.Identifier(table),
        sql.SQL(",").join(sql.Identifier(column) for column in insert_columns))
    if any(target_defs[column]["identity"] for column in insert_columns):
        statement += sql.SQL(" OVERRIDING SYSTEM VALUE")
    statement += sql.SQL(" VALUES ({})").format(
        sql.SQL(",").join(sql.Placeholder() for _ in insert_columns))
    cursor = source.execute(f"SELECT {','.join(_quote_sqlite_identifier(column) for column in columns)} FROM {quoted}")
    inserted = 0
    while rows := cursor.fetchmany(_CHUNK_SIZE):
        values = []
        for row in rows:
            row_by_name = dict(zip(columns, row))
            converted = []
            for index, column in enumerate(columns):
                value = row[index]
                if table == "claim_extraction_reservations" and legacy_reservation_fields:
                    if column == "status":
                        value = "uncertain"
                    elif column == "lease_until":
                        value = datetime(1970, 1, 1, tzinfo=timezone.utc)
                    elif column == "owner_token":
                        value = "legacy-imported"
                # Legacy ArtifactStore initialized this sentinel from
                # created_at + one day. Preserve that compatibility rule,
                # rather than importing an already-expired zero deadline.
                if table == "outbox" and column == "retry_deadline" and value == 0:
                    value = _legacy_outbox_deadline(row_by_name)
                converted.append(_convert(value, target_defs[column]))
            if inherited_revision_fields:
                parent = source.execute(
                    "SELECT publisher,language FROM documents WHERE document_id=?",
                    (row_by_name.get("document_id"),),
                ).fetchone()
                if parent is None:
                    raise ValueError("legacy_revision_document_not_found")
                parent_fields = {"publisher": parent[0], "language": parent[1]}
                for column in inherited_revision_fields:
                    converted.append(_convert(parent_fields[column], target_defs[column]))
            if legacy_reservation_fields:
                reservation_values = {
                    "lease_until": datetime(1970, 1, 1, tzinfo=timezone.utc),
                    "owner_token": "legacy-imported",
                }
                if "status" in columns:
                    status_index = columns.index("status")
                    converted[status_index] = _convert("uncertain", target_defs["status"])
                for column in legacy_reservation_fields:
                    converted.append(_convert(reservation_values[column], target_defs[column]))
            if synthesized_retry_deadline:
                converted.append(_convert(_legacy_outbox_deadline(row_by_name),
                                          target_defs["retry_deadline"]))
            values.append(tuple(converted))
        target.cursor().executemany(statement, values)
        inserted += len(values)
    return inserted


def _legacy_outbox_deadline(row: dict[str, Any]) -> Decimal:
    created_at = row.get("created_at")
    if created_at is None:
        raise ValueError("legacy_outbox_created_at_required_for_retry_deadline")
    try:
        created_epoch = Decimal(str(created_at))
    except InvalidOperation:
        try:
            parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("legacy_outbox_created_at_invalid") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        created_epoch = Decimal(str(parsed.timestamp()))
    return created_epoch + Decimal(86400)


def _advance_sequences(connection, schema: str,
                       target_columns: dict[str, list[dict[str, Any]]]) -> None:
    for table, columns in target_columns.items():
        for column in columns:
            if not column["identity"] and not (column["default"] or "").startswith("nextval("):
                continue
            sequence = connection.execute("SELECT pg_get_serial_sequence(%s,%s)",
                (f"{schema}.{table}", column["name"])).fetchone()[0]
            if not sequence:
                continue
            seq_schema, seq_name = connection.execute("""SELECT namespace.nspname, relation.relname
                FROM pg_class relation JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
                WHERE relation.oid=%s::regclass""", (sequence,)).fetchone()
            connection.execute("SELECT pg_advisory_xact_lock(%s)",
                (_advisory_key("global-sequence", f"{seq_schema}.{seq_name}"),))
            current_value = connection.execute(sql.SQL("SELECT last_value FROM {}.{}").format(
                sql.Identifier(seq_schema), sql.Identifier(seq_name))).fetchone()[0]
            maximum = connection.execute(sql.SQL("SELECT max({}) FROM {}.{}").format(
                sql.Identifier(column["name"]), sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]
            if maximum is not None:
                connection.execute("SELECT setval(%s::regclass,%s,true)",
                                   (sequence, max(current_value, maximum)))


def _advisory_key(component: str, store_key: str) -> int:
    raw = (component + "\0sqlite-import\0" + store_key).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


def _compatibility_transforms(source_columns: dict[str, list[str]],
                              source_counts: dict[str, int],
                              source: sqlite3.Connection) -> list[str]:
    transforms = []
    outbox_columns = source_columns.get("outbox", [])
    if source_counts.get("outbox", 0) and "retry_deadline" not in outbox_columns:
        transforms.append("outbox.retry_deadline: derived from created_at + 86400 seconds")
    elif source_counts.get("outbox", 0) and "retry_deadline" in outbox_columns:
        zero_count = source.execute("SELECT count(*) FROM outbox WHERE retry_deadline=0").fetchone()[0]
        if zero_count:
            transforms.append("outbox.retry_deadline: zero sentinel values derived from created_at + 86400 seconds")
    missing_revision_metadata = {"publisher", "language"} - set(source_columns.get("revisions", []))
    if source_counts.get("revisions", 0) and missing_revision_metadata:
        transforms.append("revisions.metadata: inherited missing publisher/language from referenced documents")
    reservation_columns = set(source_columns.get("claim_extraction_reservations", []))
    if (source_counts.get("claim_extraction_reservations", 0)
            and {"lease_until", "owner_token"} - reservation_columns):
        transforms.append("claim_extraction_reservations: marked uncertain with expired lease and legacy owner")
    return transforms


def import_database(source: str | Path, component: str, logical_path: str | Path, *,
                    confirm_quiesced: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Import one legacy SQLite database into an empty PostgreSQL namespace.

    The source must be quiesced and explicitly confirmed. The original SQLite
    database and sidecars are never opened for writing or modified.
    """
    if component not in COMPONENTS:
        raise ValueError("invalid_storage_component")
    if confirm_quiesced is not True:
        raise ValueError("sqlite_source_must_be_quiesced_and_confirmed")
    source_path = Path(source).expanduser().absolute()
    database = Database(component, logical_path)
    schema = database.schema
    store_key = database.namespace

    with tempfile.TemporaryDirectory(prefix="sqlite-import-") as snapshot_dir:
        snapshot_path, source_hash = _copy_snapshot(source_path, Path(snapshot_dir))
        with closing(_sqlite_snapshot(snapshot_path)) as sqlite_db:
            source_columns, source_counts = _inspect_source(sqlite_db)
            source_column_map: dict[str, list[str]] | None = None
            manifest_result: dict[str, Any] | None = None

            with psycopg.connect(connection_url(migration=True)) as connection:
                with connection.transaction():
                    connection.execute("SET ROLE intelligence_owner")
                    connection.execute("SELECT set_config('search_path',%s,true)", ("ai_meta,pg_catalog",))
                    connection.execute("SELECT set_config('intelligence.store_key',%s,true)", (store_key,))
                    connection.execute("SELECT pg_advisory_xact_lock(%s)", (_advisory_key(component, store_key),))
                    connection.execute("SET CONSTRAINTS ALL DEFERRED")

                    target_columns, dependencies = _introspect_target(connection, component)
                    source_column_map = _validate_source_schema(source_columns, source_counts, target_columns)
                    compatibility_transforms = _compatibility_transforms(source_columns, source_counts, sqlite_db)
                    table_names = sorted(target_columns)
                    target_counts_before = _target_counts(connection, component, table_names, store_key)

                    import_id = hashlib.sha256(json.dumps(
                        [component, store_key, source_hash], separators=(",", ":"),
                        ensure_ascii=False).encode("utf-8")).hexdigest()
                    prior = connection.execute("""SELECT import_id,source_hash,manifest
                        FROM ai_meta.imports WHERE component=%s AND store_key=%s
                        ORDER BY imported_at""", (component, store_key)).fetchall()
                    if prior:
                        if len(prior) != 1 or prior[0][0] != import_id or prior[0][1] != source_hash:
                            raise ValueError("sqlite_source_changed_after_import")
                        stored_manifest = json.loads(prior[0][2])
                        observed_counts = _target_counts(connection, component, table_names, store_key)
                        if observed_counts != stored_manifest.get("target_rows_by_table"):
                            raise ValueError("sqlite_import_target_count_invariant_failed")
                        if _bundle_hash(source_path) != source_hash:
                            raise ValueError("sqlite_source_changed_during_import")
                        return {
                            "status": "already_imported", "import_id": import_id,
                            "source_sha256": source_hash,
                            "counts": stored_manifest["source_rows_by_table"],
                            "compatibility_transforms": stored_manifest.get("compatibility_transforms", []),
                            "invariants": stored_manifest["invariants"],
                        }

                    if any(target_counts_before.values()):
                        raise ValueError("postgres_target_namespace_not_empty")

                    if dry_run:
                        if _bundle_hash(source_path) != source_hash:
                            raise ValueError("sqlite_source_changed_during_import")
                        return {
                            "status": "dry_run", "component": component,
                            "source_sha256": source_hash, "counts": source_counts,
                            "compatibility_transforms": compatibility_transforms,
                            "invariants": {
                                "source_integrity_check": "passed",
                                "source_foreign_key_check": "passed",
                                "schema_compatible": True,
                                "target_empty": True,
                                "source_stable": True,
                                "raw_blobs_copied": False,
                            },
                        }

                    source_column_map = _validate_source_schema(source_columns, source_counts, target_columns)
                    order = _table_order(set(source_column_map), dependencies)
                    for table in order:
                        inserted = _copy_table(sqlite_db, connection, schema, table,
                                               source_column_map[table], target_columns)
                        if inserted != source_counts[table]:
                            raise ValueError("sqlite_import_row_count_mismatch:" + table)

                    target_counts_after = _target_counts(connection, component, table_names, store_key)
                    expected_counts = {table: source_counts.get(table, 0) for table in table_names}
                    if target_counts_after != expected_counts:
                        raise ValueError("sqlite_import_target_count_invariant_failed")
                    _advance_sequences(connection, schema, target_columns)
                    if _bundle_hash(source_path) != source_hash:
                        raise ValueError("sqlite_source_changed_during_import")

                    manifest_result = {
                        "source_rows_by_table": {table: source_counts[table] for table in sorted(source_counts)},
                        "target_rows_by_table": target_counts_after,
                        "target_tables_absent_from_source": sorted(set(table_names) - set(source_counts)),
                        "compatibility_transforms": compatibility_transforms,
                        "invariants": {
                            "source_integrity_check": "passed",
                            "source_foreign_key_check": "passed",
                            "schema_compatible": True,
                            "target_was_empty": True,
                            "target_row_counts_match": True,
                            "source_stable_before_commit": True,
                            "ids_and_references_preserved": True,
                            "monetary_values_loaded_as_decimal": True,
                            "raw_blobs_copied": False,
                        },
                    }
                    connection.execute("""INSERT INTO ai_meta.imports
                        (import_id,component,store_key,source_hash,manifest)
                        VALUES(%s,%s,%s,%s,%s)""",
                        (import_id, component, store_key, source_hash,
                         json.dumps(manifest_result, sort_keys=True, separators=(",", ":"))))
                    return {
                        "status": "imported", "import_id": import_id,
                        "source_sha256": source_hash,
                        "counts": manifest_result["source_rows_by_table"],
                        "compatibility_transforms": compatibility_transforms,
                        "invariants": manifest_result["invariants"],
                    }
