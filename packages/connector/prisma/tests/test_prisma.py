"""Unit tests for the Prisma ORM schema data-source connector.

Tests use self-contained Prisma schema fixtures generated in ``tmp_path``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_prisma.prisma_schema import (
    DOCUMENT_SOURCE_ATTR,
    PRISMA_SOURCE_NAME,
    PRISMA_TABLE_NAME,
    iter_prisma_schema_entities,
    parse_prisma_schema_content,
    prisma_source,
)

SAMPLE_SCHEMA = """
datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

generator client {
  provider = "prisma-client-js"
}

/// Role of a user in the system
enum Role {
  USER
  ADMIN
  MODERATOR
}

/// Main user account
model User {
  id        String    @id @default(uuid())
  email     String    @unique
  name      String?
  role      Role      @default(USER)
  posts     Post[]
  profile   Profile?
  createdAt DateTime  @default(now())

  @@map("users")
  @@index([email])
}

/// Blog post written by a user
model Post {
  id        Int       @id @default(autoincrement())
  title     String
  content   String?
  published Boolean   @default(false)
  authorId  String
  author    User      @relation(fields: [authorId], references: [id], onDelete: Cascade)
}

model Profile {
  id     Int    @id @default(autoincrement())
  bio    String
  userId String @unique
  user   User   @relation(fields: [userId], references: [id])
}
"""


def test_parse_prisma_schema_content():
    parsed = parse_prisma_schema_content(SAMPLE_SCHEMA)

    assert "User" in parsed["models"]
    assert "Post" in parsed["models"]
    assert "Profile" in parsed["models"]
    assert "Role" in parsed["enums"]
    assert parsed["datasource"]["provider"] == "postgresql"

    user_model = parsed["models"]["User"]
    assert user_model["table_name"] == "users"
    assert user_model["description"] == "Main user account"

    field_names = [f["name"] for f in user_model["fields"]]
    assert "id" in field_names
    assert "email" in field_names
    assert "role" in field_names


def test_iter_prisma_schema_entities(tmp_path):
    schema_file = tmp_path / "schema.prisma"
    schema_file.write_text(SAMPLE_SCHEMA)

    docs = list(iter_prisma_schema_entities(schema_file))

    # 1 enum (Role) + 3 models (User, Post, Profile) = 4 total documents
    assert len(docs) == 4

    role_doc = next(d for d in docs if d["name"] == "Role")
    assert role_doc["id"] == "prisma:enum:role"
    assert role_doc["entity_type"] == "enum"
    assert "USER, ADMIN, MODERATOR" in role_doc["content"]

    user_doc = next(d for d in docs if d["name"] == "User")
    assert user_doc["id"] == "prisma:model:user"
    assert user_doc["table_name"] == "users"
    assert "Database Table: users" in user_doc["content"]
    assert "Primary Key: id" in user_doc["content"]
    assert "Unique Constraints: email" in user_doc["content"]
    assert "- Has many Post" in user_doc["content"]


def test_relation_and_foreign_keys_formatting(tmp_path):
    schema_file = tmp_path / "schema.prisma"
    schema_file.write_text(SAMPLE_SCHEMA)

    docs = list(iter_prisma_schema_entities(schema_file))
    post_doc = next(d for d in docs if d["name"] == "Post")

    assert post_doc["id"] == "prisma:model:post"
    assert "Belongs to User (foreign key: authorId -> id)" in post_doc["content"]
    assert "authorId: String" in post_doc["content"]


def test_orphan_cleanup_simulation(tmp_path):
    """Removing a model from schema.prisma drops it from yielded records."""
    schema_v1 = tmp_path / "v1.prisma"
    schema_v1.write_text(SAMPLE_SCHEMA)

    # v2 drops Profile model
    schema_v2_text = SAMPLE_SCHEMA.split("model Profile")[0]
    schema_v2 = tmp_path / "v2.prisma"
    schema_v2.write_text(schema_v2_text)

    docs_v1 = list(iter_prisma_schema_entities(schema_v1))
    docs_v2 = list(iter_prisma_schema_entities(schema_v2))

    names_v1 = {d["name"] for d in docs_v1}
    names_v2 = {d["name"] for d in docs_v2}

    assert "Profile" in names_v1
    assert "Profile" not in names_v2


def test_directory_with_multiple_prisma_files(tmp_path):
    sub = tmp_path / "prisma"
    sub.mkdir()
    (sub / "auth.prisma").write_text("""
model Session {
  id String @id
  token String @unique
}
""")
    (sub / "billing.prisma").write_text("""
model Invoice {
  id Int @id
  amount Float
}
""")

    docs = list(iter_prisma_schema_entities(sub))
    names = {d["name"] for d in docs}
    assert "Session" in names
    assert "Invoice" in names


def test_missing_and_empty_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(iter_prisma_schema_entities(tmp_path / "nonexistent.prisma"))

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert list(iter_prisma_schema_entities(empty_dir)) == []


def test_prisma_source_resource():
    mock_dlt = MagicMock()

    def mock_resource_decorator(*args, **kwargs):
        def decorator(fn):
            wrapper = MagicMock()
            wrapper.__name__ = fn.__name__
            wrapper._kwargs = kwargs
            return wrapper
        return decorator

    mock_dlt.resource = mock_resource_decorator

    with patch.dict("sys.modules", {"dlt": mock_dlt}):
        resource = prisma_source("/dummy/schema.prisma")

        assert getattr(resource, DOCUMENT_SOURCE_ATTR) == PRISMA_SOURCE_NAME
        assert resource._kwargs.get("name") == PRISMA_TABLE_NAME
        assert resource._kwargs.get("primary_key") == "id"
        assert resource._kwargs.get("write_disposition") == "replace"
