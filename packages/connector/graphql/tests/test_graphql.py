"""Unit tests for the GraphQL Schema Definition Language data-source connector.

Tests use self-contained GraphQL SDL fixtures generated in ``tmp_path``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_graphql.graphql_schema import (
    DOCUMENT_SOURCE_ATTR,
    GRAPHQL_SOURCE_NAME,
    GRAPHQL_TABLE_NAME,
    graphql_source,
    iter_graphql_schema_entities,
    parse_graphql_sdl_content,
)

SAMPLE_GRAPHQL_SCHEMA = '''
"""
Role assigned to a platform account
"""
enum UserRole {
  ADMIN
  MEMBER
  GUEST
}

interface Node {
  id: ID!
}

"""
User entity in the platform
"""
type User implements Node {
  id: ID!
  email: String!
  username: String
  role: UserRole!
  posts(limit: Int = 10, offset: Int): [Post!]!
}

type Post {
  id: ID!
  title: String!
  content: String
  author: User!
}

input CreateUserInput {
  email: String!
  username: String
  role: UserRole = MEMBER
}

type Query {
  me: User
  user(id: ID!): User
  users(limit: Int): [User!]!
}

type Mutation {
  createUser(input: CreateUserInput!): User!
  deleteUser(id: ID!): Boolean!
}

union SearchResult = User | Post
'''


def test_parse_graphql_sdl_content():
    entities = parse_graphql_sdl_content(SAMPLE_GRAPHQL_SCHEMA)
    names = {e["name"]: e for e in entities}

    assert "UserRole" in names
    assert names["UserRole"]["kind"] == "enum"
    assert "ADMIN" in names["UserRole"]["enum_values"]
    assert "Role assigned to a platform account" in names["UserRole"]["description"]

    assert "User" in names
    assert names["User"]["kind"] == "type"
    assert names["User"]["implements"] == "Node"
    assert "User entity in the platform" in names["User"]["description"]

    user_fields = [f["name"] for f in names["User"]["fields"]]
    assert "id" in user_fields
    assert "email" in user_fields
    assert any("posts(limit: Int = 10" in f["signature"] for f in names["User"]["fields"])

    assert "Query" in names
    query_fields = [f["name"] for f in names["Query"]["fields"]]
    assert "me" in query_fields
    assert "user(id: ID!)" in [f["name"] for f in names["Query"]["fields"]]

    assert "SearchResult" in names
    assert names["SearchResult"]["kind"] == "union"
    assert names["SearchResult"]["members"] == ["User", "Post"]


def test_iter_graphql_schema_entities(tmp_path):
    schema_file = tmp_path / "schema.graphql"
    schema_file.write_text(SAMPLE_GRAPHQL_SCHEMA)

    docs = list(iter_graphql_schema_entities(schema_file))
    # UserRole, Node, User, Post, CreateUserInput, Query, Mutation, SearchResult = 8
    assert len(docs) == 8

    user_doc = next(d for d in docs if d["name"] == "User")
    assert user_doc["id"] == "graphql:type:user"
    assert "Implements: Node" in user_doc["content"]
    assert "posts(limit: Int = 10, offset: Int): [Post!]!" in user_doc["content"]

    mutation_doc = next(d for d in docs if d["name"] == "Mutation")
    assert mutation_doc["id"] == "graphql:type:mutation"
    assert "createUser(input: CreateUserInput!): User!" in mutation_doc["content"]


def test_orphan_cleanup_simulation(tmp_path):
    """Removing an entity from the schema drops it from the yielded records."""
    v1_file = tmp_path / "v1.graphql"
    v1_file.write_text(SAMPLE_GRAPHQL_SCHEMA)

    # v2 drops SearchResult union
    v2_text = SAMPLE_GRAPHQL_SCHEMA.split("union SearchResult")[0]
    v2_file = tmp_path / "v2.graphql"
    v2_file.write_text(v2_text)

    docs_v1 = list(iter_graphql_schema_entities(v1_file))
    docs_v2 = list(iter_graphql_schema_entities(v2_file))

    names_v1 = {d["name"] for d in docs_v1}
    names_v2 = {d["name"] for d in docs_v2}

    assert "SearchResult" in names_v1
    assert "SearchResult" not in names_v2


def test_directory_with_multiple_gql_files(tmp_path):
    sub = tmp_path / "schema"
    sub.mkdir()
    (sub / "types.graphql").write_text("type Item { id: ID! }")
    (sub / "query.gql").write_text("type Query { item: Item }")

    docs = list(iter_graphql_schema_entities(sub))
    names = {d["name"] for d in docs}
    assert "Item" in names
    assert "Query" in names


def test_missing_and_corrupt_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(iter_graphql_schema_entities(tmp_path / "nonexistent.graphql"))

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert list(iter_graphql_schema_entities(empty_dir)) == []


def test_graphql_source_resource():
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
        resource = graphql_source("/dummy/schema.graphql")

        assert getattr(resource, DOCUMENT_SOURCE_ATTR) == GRAPHQL_SOURCE_NAME
        assert resource._kwargs.get("name") == GRAPHQL_TABLE_NAME
        assert resource._kwargs.get("primary_key") == "id"
        assert resource._kwargs.get("write_disposition") == "replace"
