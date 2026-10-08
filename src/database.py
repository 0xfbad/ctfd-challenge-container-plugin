from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import unquote, urlsplit

_SCHEMA_BOOTSTRAP_LOCK_PREFIX = "ctfd_cc_schema_"
_SCHEMA_BOOTSTRAP_LOCK_TIMEOUT = 60


def _lock_identity(database_url: str) -> tuple[str, int]:
    database_name = unquote(urlsplit(database_url).path.lstrip("/"))
    if not database_name:
        raise RuntimeError("challenge containers database URL must select a database")

    digest = hashlib.sha256(database_name.encode("utf-8")).digest()
    return (
        f"{_SCHEMA_BOOTSTRAP_LOCK_PREFIX}{digest.hex()[:24]}",  # mariadb caps lock names at 64 chars
        int.from_bytes(digest[:8], "big", signed=True),
    )


def _add_instance_password_column(connection: Any) -> None:
    from sqlalchemy import inspect, text

    columns = inspect(connection).get_columns("container_instances")
    if any(column["name"] == "ssh_password" for column in columns):
        return

    connection.execute(text("ALTER TABLE container_instances ADD COLUMN ssh_password VARCHAR(8) NULL"))


def _create_all(app: Any, connection: Any = None) -> None:
    app.db.create_all()
    try:
        if not isinstance(app.config.get("SQLALCHEMY_DATABASE_URI"), str):
            return

        if connection is not None:
            _add_instance_password_column(connection)
            return

        with app.db.engine.begin() as connection:
            _add_instance_password_column(connection)
    finally:
        app.db.session.remove()  # mysql invalidates open transaction metadata after ddl


def prepare_database(app: Any) -> None:
    database_url = app.config.get("SQLALCHEMY_DATABASE_URI")
    if not isinstance(database_url, str):
        _create_all(app)  # test app doubles omit the database url
        return

    dialect = app.db.engine.dialect.name
    if dialect not in {"mysql", "mariadb", "postgresql"}:
        _create_all(app)  # sqlite deployments are single process
        return

    from sqlalchemy import text

    lock_name, lock_key = _lock_identity(database_url)
    with app.db.engine.connect() as connection:
        if dialect == "postgresql":
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        else:
            acquired = connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": lock_name, "timeout": _SCHEMA_BOOTSTRAP_LOCK_TIMEOUT},
            ).scalar()
            if acquired != 1:
                raise RuntimeError("timed out waiting for the challenge containers schema bootstrap lock")

        try:
            _create_all(app, connection)  # workers without preload must serialize schema changes
        finally:
            try:
                if dialect == "postgresql":
                    released = connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}).scalar()
                else:
                    released = connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}).scalar()
            except BaseException:
                connection.invalidate()  # a failed release may still hold the lock
                raise

            if not released:
                connection.invalidate()
                raise RuntimeError("failed to release the challenge containers schema bootstrap lock")
