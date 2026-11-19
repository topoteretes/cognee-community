"""Unit tests for the Drizzle ORM schema connector.

All tests run hermetically in CI without external network access or live database connections.
"""

from pathlib import Path

from cognee_community_connector_drizzle.drizzle import (
    DRIZZLE_SOURCE_NAME,
    DRIZZLE_TABLE_NAME,
    _extract_enums,
    _extract_relations,
    _extract_tables,
    drizzle_source,
    parse_drizzle_files,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_extract_enums():
    content = """
    export const userRoleEnum = pgEnum("user_role", ["admin", "member", "guest"]);
    export const statusEnum = mysqlEnum("order_status", ["pending", "shipped"]);
    """
    enums = _extract_enums(content)
    assert len(enums) == 2

    assert enums[0]["name"] == "user_role"
    assert enums[0]["dialect"] == "PostgreSQL"
    assert enums[0]["values"] == ["admin", "member", "guest"]

    assert enums[1]["name"] == "order_status"
    assert enums[1]["dialect"] == "MySQL"
    assert enums[1]["values"] == ["pending", "shipped"]


def test_extract_postgres_tables():
    file_path = FIXTURES_DIR / "postgres_schema.ts"
    content = file_path.read_text(encoding="utf-8")
    tables = _extract_tables(content)

    assert len(tables) == 3
    table_names = [t["name"] for t in tables]
    assert "users" in table_names
    assert "profiles" in table_names
    assert "posts" in table_names

    # Check users table columns
    users_tbl = next(t for t in tables if t["name"] == "users")
    assert users_tbl["dialect"] == "PostgreSQL"
    assert len(users_tbl["columns"]) == 4

    id_col = next(c for c in users_tbl["columns"] if c["sql_name"] == "id")
    assert id_col["is_primary_key"] is True
    assert id_col["type_function"] == "serial"

    email_col = next(c for c in users_tbl["columns"] if c["sql_name"] == "email")
    assert email_col["is_not_null"] is True
    assert email_col["is_unique"] is True

    # Check foreign key in profiles
    profiles_tbl = next(t for t in tables if t["name"] == "profiles")
    user_id_col = next(c for c in profiles_tbl["columns"] if c["sql_name"] == "user_id")
    assert user_id_col["references"] == "users.id"
    assert user_id_col["on_delete"] == "cascade"


def test_extract_relations():
    file_path = FIXTURES_DIR / "postgres_schema.ts"
    content = file_path.read_text(encoding="utf-8")
    relations_map = _extract_relations(content)

    assert "users" in relations_map
    user_rels = relations_map["users"]
    assert len(user_rels) == 2

    # One relation to profiles
    profile_rel = next(r for r in user_rels if r["name"] == "profile")
    assert profile_rel["type"] == "one"
    assert profile_rel["target_table"] == "profiles"
    assert profile_rel["fields"] == ["users.id"]
    assert profile_rel["references"] == ["profiles.userId"]

    # Many relation to posts
    posts_rel = next(r for r in user_rels if r["name"] == "posts")
    assert posts_rel["type"] == "many"
    assert posts_rel["target_table"] == "posts"


def test_extract_sqlite_tables():
    file_path = FIXTURES_DIR / "sqlite_schema.ts"
    content = file_path.read_text(encoding="utf-8")
    tables = _extract_tables(content)

    assert len(tables) == 1
    todo_tbl = tables[0]
    assert todo_tbl["name"] == "todos"
    assert todo_tbl["dialect"] == "SQLite"

    id_col = next(c for c in todo_tbl["columns"] if c["sql_name"] == "id")
    assert id_col["is_primary_key"] is True

    completed_col = next(c for c in todo_tbl["columns"] if c["sql_name"] == "completed")
    assert completed_col["is_not_null"] is True
    assert completed_col["default_value"] == "false"


def test_extract_mysql_tables():
    file_path = FIXTURES_DIR / "mysql_schema.ts"
    content = file_path.read_text(encoding="utf-8")
    tables = _extract_tables(content)

    assert len(tables) == 1
    prod_tbl = tables[0]
    assert prod_tbl["name"] == "products"
    assert prod_tbl["dialect"] == "MySQL"

    price_col = next(c for c in prod_tbl["columns"] if c["sql_name"] == "price")
    assert price_col["is_not_null"] is True
    assert price_col["type_function"] == "decimal"


def test_parse_drizzle_files_single_file():
    file_path = FIXTURES_DIR / "postgres_schema.ts"
    docs = parse_drizzle_files(file_path)

    # 1 enum + 3 tables = 4 documents
    assert len(docs) == 4

    doc_ids = [d["id"] for d in docs]
    assert "drizzle:enum:user_role" in doc_ids
    assert "drizzle:table:users" in doc_ids
    assert "drizzle:table:profiles" in doc_ids
    assert "drizzle:table:posts" in doc_ids

    users_doc = next(d for d in docs if d["id"] == "drizzle:table:users")
    assert "Database Table: `users`" in users_doc["content"]
    assert "PRIMARY KEY" in users_doc["content"]
    assert "Relations" in users_doc["content"]
    assert "**Many** `posts`" in users_doc["content"]
    assert "**One** `profile`" in users_doc["content"]


def test_parse_drizzle_files_directory():
    docs = parse_drizzle_files(FIXTURES_DIR)

    # postgres (4) + sqlite (1) + mysql (1) = 6 documents
    assert len(docs) >= 6
    doc_ids = [d["id"] for d in docs]
    assert "drizzle:table:todos" in doc_ids
    assert "drizzle:table:products" in doc_ids
    assert "drizzle:table:users" in doc_ids


def test_parse_drizzle_files_missing_path():
    docs = parse_drizzle_files(Path("/non/existent/path/schema.ts"))
    assert docs == []


def test_drizzle_source_dlt_structure():
    file_path = FIXTURES_DIR / "postgres_schema.ts"
    source = drizzle_source(schema_path=file_path)

    # Verify dlt source name
    assert source.name == DRIZZLE_SOURCE_NAME

    # Verify document mode tag
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == DRIZZLE_SOURCE_NAME

    # Verify resource
    resources = list(source.resources.values())
    assert len(resources) == 1
    assert resources[0].name == DRIZZLE_TABLE_NAME
    assert resources[0].write_disposition == "replace"
