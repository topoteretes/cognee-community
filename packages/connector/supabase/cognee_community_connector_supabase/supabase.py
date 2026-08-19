"""Read-only Supabase PostgreSQL source for cognee.

OAuth is used only to discover projects. Supabase does not expose an existing
database password through OAuth, so ingestion takes a separate connection URL
for a dedicated read-only PostgreSQL role.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import MetaData, Table, create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine

SUPABASE_API_URL = "https://api.supabase.com"
SUPABASE_AUTHORIZE_URL = f"{SUPABASE_API_URL}/v1/oauth/authorize"
SUPABASE_TOKEN_URL = f"{SUPABASE_API_URL}/v1/oauth/token"
SUPABASE_ROWS_TABLE = "supabase_rows"
# Shared protocol constant from cognee.tasks.ingestion.dlt_utils. Keeping the
# literal local avoids importing cognee's full runtime merely to construct a source.
DOCUMENT_SOURCE_ATTR = "cognee_document_source"
_OAUTH_SCOPE = "projects:read"
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_]+")
_DOCUMENT_COLUMNS = {
    "id": {"data_type": "text", "nullable": False},
    "title": {"data_type": "text", "nullable": False},
    "content": {"data_type": "text", "nullable": False},
    "_deleted": {"data_type": "bool", "hard_delete": True},
}


class SupabaseManagementAPIError(RuntimeError):
    """A sanitized Supabase Management API error (never includes credentials)."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Supabase Management API request failed with HTTP {status_code}.")


@dataclass(frozen=True)
class SupabaseAuthorization:
    """PKCE authorization request details; keep ``code_verifier`` private."""

    url: str
    state: str
    code_verifier: str = field(repr=False)


@dataclass(frozen=True)
class SupabaseOAuthTokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    token_type: str = "Bearer"
    expires_in: int | None = None


@dataclass(frozen=True)
class SupabaseProject:
    ref: str
    name: str
    organization_id: str | None = None
    region: str | None = None
    status: str | None = None
    database_host: str | None = None


def create_supabase_authorization_url(
    client_id: str,
    redirect_uri: str,
    *,
    state: str | None = None,
    organization_slug: str | None = None,
) -> SupabaseAuthorization:
    """Create a Supabase OAuth authorization URL using PKCE (S256)."""
    if not client_id or not redirect_uri:
        raise ValueError("client_id and redirect_uri are required.")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    resolved_state = state or secrets.token_urlsafe(32)
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _OAUTH_SCOPE,
        "state": resolved_state,
        "code_challenge": challenge.decode(),
        "code_challenge_method": "S256",
    }
    if organization_slug:
        query["organization_slug"] = organization_slug
    return SupabaseAuthorization(
        url=f"{SUPABASE_AUTHORIZE_URL}?{urlencode(query)}",
        state=resolved_state,
        code_verifier=verifier,
    )


def exchange_supabase_oauth_code(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    *,
    session: Any = None,
) -> SupabaseOAuthTokens:
    """Exchange an OAuth authorization code for tokens."""
    payload = _management_request(
        "post",
        SUPABASE_TOKEN_URL,
        session=session,
        auth=(client_id, client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )
    return _tokens_from_payload(payload)


def refresh_supabase_oauth_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    session: Any = None,
) -> SupabaseOAuthTokens:
    """Refresh Supabase OAuth tokens."""
    payload = _management_request(
        "post",
        SUPABASE_TOKEN_URL,
        session=session,
        auth=(client_id, client_secret),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    return _tokens_from_payload(payload)


def discover_supabase_projects(access_token: str, *, session: Any = None) -> list[SupabaseProject]:
    """List projects visible to a Supabase OAuth access token."""
    payload = _management_request(
        "get",
        f"{SUPABASE_API_URL}/v1/projects",
        session=session,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if not isinstance(payload, list):
        raise SupabaseManagementAPIError(502)
    return [
        SupabaseProject(
            ref=str(item["ref"]),
            name=str(item.get("name") or item["ref"]),
            organization_id=item.get("organization_id"),
            region=item.get("region"),
            status=item.get("status"),
            database_host=(item.get("database") or {}).get("host"),
        )
        for item in payload
        if isinstance(item, dict) and item.get("ref")
    ]


def discover_supabase_schema(
    connection_string: str | Engine,
    *,
    schema: str = "public",
    enforce_read_only: bool = True,
) -> dict[str, dict[str, Any]]:
    """Return columns and primary keys for tables in one database schema."""
    engine, owns_engine = _coerce_engine(connection_string, enforce_read_only=enforce_read_only)
    try:
        inspector = inspect(engine)
        result: dict[str, dict[str, Any]] = {}
        for table_name in inspector.get_table_names(schema=schema):
            result[table_name] = {
                "columns": [column["name"] for column in inspector.get_columns(table_name, schema)],
                "primary_key": (
                    inspector.get_pk_constraint(table_name, schema).get("constrained_columns") or []
                ),
            }
        return result
    finally:
        if owns_engine:
            engine.dispose()


def supabase_source(
    connection_string: str | Engine,
    *,
    project_ref: str,
    tables: Sequence[str],
    columns: Mapping[str, Sequence[str]],
    cursor_columns: Mapping[str, str],
    schema: str = "public",
    primary_keys: Mapping[str, str | Sequence[str]] | None = None,
    initial_values: Mapping[str, Any] | None = None,
    source_name: str = "supabase",
    chunk_size: int = 5_000,
    enforce_read_only: bool = True,
):
    """Build an incremental, deletion-aware dlt source for selected tables.

    Every selected table must declare both an explicit column allow-list and a
    monotonic timestamp cursor. Primary keys are reflected, or may be supplied
    with ``primary_keys``. The upstream database is only reflected and SELECTed.

    Pass the returned source to ``cognee.remember`` with ``primary_key="id"``
    and ``write_disposition="merge"``.
    """
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer.")
    try:
        import dlt
        from dlt.sources.sql_database import sql_table
    except ImportError as exc:  # pragma: no cover - dependency installation error
        raise ImportError(
            "Install cognee-community-connector-supabase with its default dependencies."
        ) from exc

    engine, _ = _coerce_engine(connection_string, enforce_read_only=enforce_read_only)
    config = _validate_selection(
        engine,
        project_ref=project_ref,
        schema=schema,
        tables=tables,
        columns=columns,
        cursor_columns=cursor_columns,
        primary_keys=primary_keys or {},
        enforce_read_only=enforce_read_only,
    )
    initial_values = initial_values or {}
    dlt_source_name = _dlt_name(source_name)
    row_resources = []

    for table_name, table_config in config.items():
        selected_columns = table_config["columns"]
        key_columns = table_config["primary_key"]
        cursor_column = table_config["cursor"]
        reflected_columns = list(dict.fromkeys([*selected_columns, *key_columns, cursor_column]))
        fingerprint = _fingerprint(
            project_ref, schema, table_name, selected_columns, key_columns, cursor_column
        )
        parent = sql_table(
            credentials=engine,
            table=table_name,
            schema=schema,
            included_columns=reflected_columns,
            chunk_size=chunk_size,
            primary_key=key_columns,
            incremental=dlt.sources.incremental(
                cursor_column,
                initial_value=initial_values.get(table_name),
                primary_key=key_columns,
            ),
        ).with_name(f"{dlt_source_name}_{_dlt_name(table_name)}_{fingerprint}")

        def make_transform(table_name, selected_columns, key_columns):
            def to_documents(batch):
                rows = batch if isinstance(batch, list) else [batch]
                for row in rows:
                    yield _document_row(
                        row,
                        project_ref=project_ref,
                        schema=schema,
                        table=table_name,
                        columns=selected_columns,
                        primary_key=key_columns,
                    )

            return to_documents

        transformer = dlt.transformer(
            make_transform(table_name, selected_columns, key_columns),
            data_from=parent,
            name=f"map_{_dlt_name(table_name)}_{fingerprint}",
            table_name=SUPABASE_ROWS_TABLE,
            primary_key="id",
            write_disposition="merge",
            columns=_DOCUMENT_COLUMNS,
        )
        row_resources.append(transformer)

    @dlt.resource(
        name=f"{dlt_source_name}_deletion_sweep",
        table_name=SUPABASE_ROWS_TABLE,
        primary_key="id",
        write_disposition="merge",
        columns=_DOCUMENT_COLUMNS,
    )
    def deletion_sweep():
        state = dlt.current.resource_state()
        previous: dict[str, list[str]] = state.get("known_ids", {})
        current: dict[str, list[str]] = {}

        with engine.connect() as db:
            for table_name, table_config in config.items():
                metadata = MetaData()
                table = Table(table_name, metadata, schema=schema, autoload_with=db)
                key_columns = table_config["primary_key"]
                rows = db.execute(select(*(table.c[key] for key in key_columns)))
                ids = {
                    _document_id(project_ref, schema, table_name, dict(row._mapping), key_columns)
                    for row in rows
                }
                current[table_name] = sorted(ids)

        previous_ids = {item for values in previous.values() for item in values}
        current_ids = {item for values in current.values() for item in values}
        for deleted_id in sorted(previous_ids - current_ids):
            yield _tombstone(deleted_id)

        anchor_id = _anchor_id(project_ref, schema, source_name)
        anchor_active = bool(state.get("anchor_active"))
        if previous_ids and not current_ids and not anchor_active:
            yield {
                "id": anchor_id,
                "title": f"{project_ref}.{schema} sync state",
                "content": _json(
                    {
                        "source": "supabase",
                        "project_ref": project_ref,
                        "schema": schema,
                        "type": "sync_state",
                        "record_count": 0,
                    }
                ),
                "_deleted": False,
            }
            anchor_active = True
        elif current_ids and anchor_active:
            yield _tombstone(anchor_id)
            anchor_active = False

        state["known_ids"] = current
        state["anchor_active"] = anchor_active

    @dlt.source(name=dlt_source_name)
    def _supabase():
        return [*row_resources, deletion_sweep]

    source = _supabase()
    marker = f"supabase:{project_ref}:{schema}:{source_name}"
    setattr(source, DOCUMENT_SOURCE_ATTR, marker)
    return source


def _management_request(method: str, url: str, *, session: Any = None, **kwargs):
    if session is None:
        import requests

        session = requests
    try:
        response = getattr(session, method)(url, timeout=30, **kwargs)
    except Exception as exc:
        raise SupabaseManagementAPIError(0) from exc
    if not 200 <= response.status_code < 300:
        raise SupabaseManagementAPIError(response.status_code)
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise SupabaseManagementAPIError(502) from exc


def _tokens_from_payload(payload: Any) -> SupabaseOAuthTokens:
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise SupabaseManagementAPIError(502)
    return SupabaseOAuthTokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        token_type=payload.get("token_type", "Bearer"),
        expires_in=payload.get("expires_in"),
    )


def _coerce_engine(
    connection_string: str | Engine, *, enforce_read_only: bool
) -> tuple[Engine, bool]:
    if isinstance(connection_string, Engine):
        engine = connection_string
        owns_engine = False
    elif isinstance(connection_string, str) and connection_string:
        url = connection_string
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url.removeprefix("postgres://")
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
        engine = create_engine(url, pool_pre_ping=True)
        owns_engine = True
    else:
        raise TypeError("connection_string must be a SQLAlchemy Engine or database URL.")

    if (
        enforce_read_only
        and engine.dialect.name == "postgresql"
        and not event.contains(engine, "checkout", _set_postgres_read_only)
    ):
        event.listen(engine, "checkout", _set_postgres_read_only)
    return engine, owns_engine


def _set_postgres_read_only(dbapi_connection, _connection_record, _connection_proxy) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    finally:
        cursor.close()


def _validate_selection(
    engine: Engine,
    *,
    project_ref: str,
    schema: str,
    tables: Sequence[str],
    columns: Mapping[str, Sequence[str]],
    cursor_columns: Mapping[str, str],
    primary_keys: Mapping[str, str | Sequence[str]],
    enforce_read_only: bool,
) -> dict[str, dict[str, Any]]:
    if not project_ref or not schema:
        raise ValueError("project_ref and schema are required.")
    if isinstance(tables, str) or not tables or len(set(tables)) != len(tables):
        raise ValueError("tables must contain unique, explicitly selected table names.")
    if set(columns) != set(tables):
        raise ValueError("columns must define an explicit allow-list for every selected table.")
    if set(cursor_columns) != set(tables):
        raise ValueError("cursor_columns must define an incremental cursor for every table.")
    if not set(primary_keys) <= set(tables):
        raise ValueError("primary_keys contains a table that is not selected.")

    inspector = inspect(engine)
    available_tables = set(inspector.get_table_names(schema=schema))
    missing_tables = set(tables) - available_tables
    if missing_tables:
        raise ValueError(f"Unknown selected table(s): {', '.join(sorted(missing_tables))}.")

    config = {}
    for table_name in tables:
        if not isinstance(table_name, str) or not table_name:
            raise ValueError("Every selected table name must be a non-empty string.")
        if isinstance(columns[table_name], str):
            raise ValueError(f"columns[{table_name!r}] must be a sequence of column names.")
        selected = list(columns[table_name])
        if (
            not selected
            or not all(isinstance(column, str) and column for column in selected)
            or len(set(selected)) != len(selected)
        ):
            raise ValueError(f"columns[{table_name!r}] must contain unique column names.")
        if not isinstance(cursor_columns[table_name], str) or not cursor_columns[table_name]:
            raise ValueError(f"cursor_columns[{table_name!r}] must be a column name.")
        available_columns = {
            column["name"] for column in inspector.get_columns(table_name, schema=schema)
        }
        unknown = (set(selected) | {cursor_columns[table_name]}) - available_columns
        if unknown:
            raise ValueError(f"Unknown column(s) for {table_name}: {', '.join(sorted(unknown))}.")
        supplied_key = primary_keys.get(table_name)
        reflected_key = (
            inspector.get_pk_constraint(table_name, schema=schema).get("constrained_columns") or []
        )
        if isinstance(supplied_key, str):
            key_columns = [supplied_key]
        elif supplied_key is None:
            key_columns = list(reflected_key)
        elif isinstance(supplied_key, Sequence):
            key_columns = list(supplied_key)
        else:
            raise ValueError(f"primary_keys[{table_name!r}] must contain column names.")
        if (
            not key_columns
            or not all(isinstance(column, str) and column for column in key_columns)
            or len(set(key_columns)) != len(key_columns)
            or not set(key_columns) <= available_columns
        ):
            raise ValueError(f"A valid primary key is required for {table_name}.")
        config[table_name] = {
            "columns": selected,
            "primary_key": key_columns,
            "cursor": cursor_columns[table_name],
        }

    if enforce_read_only and engine.dialect.name == "postgresql":
        _validate_postgres_read_only_role(engine, schema, tables)
    return config


def _validate_postgres_read_only_role(engine: Engine, schema: str, tables: Sequence[str]) -> None:
    with engine.connect() as db:
        if str(db.execute(text("SHOW transaction_read_only")).scalar()).lower() != "on":
            raise PermissionError("The PostgreSQL session is not read-only.")
        for table_name in tables:
            qualified = f"{schema}.{table_name}"
            can_select = db.execute(
                text("SELECT has_table_privilege(current_user, :table, 'SELECT')"),
                {"table": qualified},
            ).scalar()
            can_write = db.execute(
                text(
                    "SELECT has_table_privilege(current_user, :table, "
                    "'INSERT, UPDATE, DELETE, TRUNCATE')"
                ),
                {"table": qualified},
            ).scalar()
            if not can_select or can_write:
                raise PermissionError(
                    f"Database role must have SELECT and no write privileges on {qualified}."
                )


def _document_row(
    row: Mapping[str, Any],
    *,
    project_ref: str,
    schema: str,
    table: str,
    columns: Sequence[str],
    primary_key: Sequence[str],
) -> dict[str, Any]:
    values = dict(row)
    key = {name: values[name] for name in primary_key}
    document_id = _document_id(project_ref, schema, table, values, primary_key)
    return {
        "id": document_id,
        "title": f"{schema}.{table} {_json(key)}",
        "content": _json(
            {
                "source": "supabase",
                "project_ref": project_ref,
                "schema": schema,
                "table": table,
                "primary_key": key,
                "row": {name: values.get(name) for name in columns},
            }
        ),
        "_deleted": False,
    }


def _document_id(
    project_ref: str,
    schema: str,
    table: str,
    row: Mapping[str, Any],
    primary_key: Sequence[str],
) -> str:
    key = {name: row[name] for name in primary_key}
    return f"supabase:{project_ref}:{schema}:{table}:{_json(key)}"


def _anchor_id(project_ref: str, schema: str, source_name: str) -> str:
    return f"supabase:{project_ref}:{schema}:{source_name}:__sync_anchor__"


def _tombstone(document_id: str) -> dict[str, Any]:
    # Hard-delete rows are normalized into the same non-null document schema
    # before dlt applies the marker during merge.
    return {"id": document_id, "title": "", "content": "", "_deleted": True}


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha256(_json(parts).encode()).hexdigest()[:10]


def _dlt_name(value: str) -> str:
    normalized = _SAFE_NAME.sub("_", value).strip("_").lower()
    if not normalized:
        raise ValueError("source_name must contain at least one letter or digit.")
    return normalized
