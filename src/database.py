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
    # mariadb caps lock names at 64 chars
    return f"{_SCHEMA_BOOTSTRAP_LOCK_PREFIX}{digest.hex()[:24]}", int.from_bytes(digest[:8], "big", signed=True)


def _create_all(app: Any) -> None:
    app.db.create_all()
    # mysql invalidates open transaction metadata after ddl, so drop the scoped session before seeding
    app.db.session.remove()


def prepare_database(app: Any) -> None:
    """create the schema for a fresh deployment, existing tables are never mutated

    workers without preload build the app at once, so the ddl runs under a database lock
    """
    database_url = app.config.get("SQLALCHEMY_DATABASE_URI")
    if not isinstance(database_url, str):
        _create_all(app)  # test app doubles set no SQLALCHEMY_DATABASE_URI, production always does
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
            _create_all(app)
        finally:
            try:
                if dialect == "postgresql":
                    released = connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}).scalar()
                else:
                    released = connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}).scalar()
            except BaseException:
                # a failed release may still hold the lock, invalidate discards the connection
                connection.invalidate()
                raise

            if not released:
                connection.invalidate()
                raise RuntimeError("failed to release the challenge containers schema bootstrap lock")
