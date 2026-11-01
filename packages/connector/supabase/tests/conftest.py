"""Integration tests create/drop objects only in an explicitly disposable cluster."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture
def postgres_fixture():
    raw = os.getenv("SUPABASE_TEST_ADMIN_URL")
    if not raw or os.getenv("SUPABASE_TEST_DISPOSABLE") != "1":
        pytest.skip("Requires SUPABASE_TEST_DISPOSABLE=1 and SUPABASE_TEST_ADMIN_URL")
    url = make_url(raw)
    if url.host not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("Disposable integration cluster must be on loopback")
    suffix = uuid4().hex
    database, role = f"connector_test_{suffix}", f"reader_{suffix}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    writer = reader = None
    try:
        with admin.connect() as db:
            db.execute(text(f'CREATE ROLE "{role}" LOGIN'))
            db.execute(text(f'CREATE DATABASE "{database}"'))
        writer = create_engine(url.set(database=database))
        with writer.begin() as db:
            for schema in ("project_a", "project_b"):
                db.execute(text(f"CREATE SCHEMA {schema}"))
                db.execute(
                    text(
                        f"CREATE TABLE {schema}.customers (id integer PRIMARY KEY, "
                        "name text, private_note text, updated_at timestamp NOT NULL)"
                    )
                )
                db.execute(text(f"CREATE TABLE {schema}.unselected (secret text)"))
                db.execute(text(f'GRANT USAGE ON SCHEMA {schema} TO "{role}"'))
                db.execute(text(f'GRANT SELECT ON {schema}.customers TO "{role}"'))
        reader = create_engine(url.set(database=database, username=role, password=None))
        yield writer, reader
    finally:
        if reader is not None:
            reader.dispose()
        if writer is not None:
            writer.dispose()
        with admin.connect() as db:
            db.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            db.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()
