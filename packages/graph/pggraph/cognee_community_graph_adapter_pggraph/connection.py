"""Resolve Postgres connection strings for the pgGraph adapter."""

_ASYNC_DRIVER = "postgresql+asyncpg"


def _normalize_scheme(url: str) -> str:
    """Coerce a Postgres URL to the asyncpg dialect so create_async_engine accepts it.

    SQLAlchemy's async engine requires an explicit async driver; bare
    ``postgresql://`` or ``postgres://`` URLs are rejected at runtime with a
    cryptic error. Rewrite them to ``postgresql+asyncpg://`` here so callers
    can hand us either form.
    """
    if url.startswith(f"{_ASYNC_DRIVER}://") or url.startswith(f"{_ASYNC_DRIVER}+"):
        return url
    if url.startswith("postgresql+"):
        # Some other async/sync driver (e.g. psycopg). Honour the user's choice
        # rather than silently overriding — create_async_engine will surface a
        # clear error if the chosen driver isn't async-capable.
        return url
    if url.startswith("postgresql://"):
        return f"{_ASYNC_DRIVER}://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return f"{_ASYNC_DRIVER}://" + url[len("postgres://"):]
    return url


def resolve_connection_string(
    graph_database_url: str = "",
    graph_database_username: str = "",
    graph_database_password: str = "",
    graph_database_host: str = "",
    graph_database_port: str = "",
    graph_database_name: str = "",
) -> str:
    """Build an asyncpg SQLAlchemy URL from graph config or relational env."""
    if graph_database_url and (
        graph_database_url.startswith("postgresql") or graph_database_url.startswith("postgres://")
    ):
        return _normalize_scheme(graph_database_url)

    if (
        graph_database_host
        and graph_database_port
        and graph_database_name
        and graph_database_username
        and graph_database_password
    ):
        return (
            f"{_ASYNC_DRIVER}://{graph_database_username}:{graph_database_password}"
            f"@{graph_database_host}:{graph_database_port}/{graph_database_name}"
        )

    from cognee.infrastructure.databases.relational import get_relational_config

    relational_config = get_relational_config()
    db_username = relational_config.db_username
    db_password = relational_config.db_password
    db_host = relational_config.db_host
    db_port = relational_config.db_port
    db_name = relational_config.db_name

    if not (db_host and db_port and db_name and db_username and db_password):
        raise EnvironmentError(
            "Missing Postgres credentials. Set GRAPH_DATABASE_* or DB_* environment variables."
        )

    return (
        f"{_ASYNC_DRIVER}://{db_username}:{db_password}"
        f"@{db_host}:{db_port}/{db_name}"
    )
