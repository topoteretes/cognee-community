"""Fixtures and mock Slab GraphQL server using httpx.MockTransport."""

import json
from typing import Any

import httpx
import pytest


class FakeSlabServer:
    """Mock in-memory Slab GraphQL API server."""

    def __init__(self) -> None:
        self.valid_token = "valid_slab_token"
        self.rate_limit_hits = 0
        self.server_error_hits = 0
        self.return_partial_errors = False
        self.return_fatal_errors = False

        self.posts: list[dict[str, Any]] = [
            {
                "id": "post-101",
                "title": "Architecture Overview",
                "content": (
                    "This document outlines our microservices architecture and graph memory."
                ),
                "insertedAt": "2026-10-01T12:00:00Z",
                "updatedAt": "2026-10-10T15:30:00Z",
                "owner": {"id": "user-1", "name": "Alice Engineer", "email": "alice@example.com"},
                "topics": [{"id": "topic-eng", "name": "Engineering"}],
                "comments": [
                    {
                        "id": "comment-1",
                        "content": "Should we also mention the vector database?",
                        "insertedAt": "2026-10-11T09:00:00Z",
                        "author": {"id": "user-2", "name": "Bob Architect"},
                    }
                ],
            },
            {
                "id": "post-102",
                "title": "Onboarding Guide",
                "content": (
                    "Welcome to the team! Here are the steps to get your local environment set up."
                ),
                "insertedAt": "2026-10-02T10:00:00Z",
                "updatedAt": "2026-10-12T11:00:00Z",
                "owner": {"id": "user-3", "name": "Carol Manager", "email": "carol@example.com"},
                "topics": [{"id": "topic-hr", "name": "Human Resources"}],
                "comments": [],
            },
        ]

        self.topics: dict[str, dict[str, Any]] = {
            "topic-eng": {
                "id": "topic-eng",
                "name": "Engineering",
                "description": "Technical design docs and architecture specs",
                "post_ids": ["post-101"],
            },
            "topic-hr": {
                "id": "topic-hr",
                "name": "Human Resources",
                "description": "Team onboarding and policies",
                "post_ids": ["post-102"],
            },
        }

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if self.valid_token not in auth:
            return httpx.Response(401, json={"message": "Unauthorized: invalid API token"})

        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return httpx.Response(429, json={"message": "Too Many Requests"})

        if self.server_error_hits > 0:
            self.server_error_hits -= 1
            return httpx.Response(503, json={"message": "Service Temporarily Unavailable"})

        if self.return_fatal_errors:
            return httpx.Response(
                200,
                json={"data": None, "errors": [{"message": "Fatal GraphQL schema syntax error"}]},
            )

        body = json.loads(request.content.decode("utf-8"))
        query = body.get("query", "")
        variables = body.get("variables", {}) or {}

        errors_payload = (
            [{"message": "Warning: Field 'owner.email' is deprecated."}]
            if self.return_partial_errors
            else None
        )

        # Handling Topic-filtered query
        if "topicId" in variables or "GetTopicPosts" in query:
            topic_id = variables.get("topicId")
            topic = self.topics.get(topic_id)
            if not topic:
                payload: dict[str, Any] = {"data": {"topic": None}}
                if errors_payload:
                    payload["errors"] = errors_payload
                return httpx.Response(200, json=payload)

            matching_posts = [p for p in self.posts if p["id"] in topic.get("post_ids", [])]
            first = variables.get("first", 50)
            after = variables.get("after")

            start_idx = 0
            if after:
                for idx, p in enumerate(matching_posts):
                    if p["id"] == after:
                        start_idx = idx + 1
                        break

            slice_posts = matching_posts[start_idx : start_idx + first]
            has_next = (start_idx + len(slice_posts)) < len(matching_posts)
            end_cursor = slice_posts[-1]["id"] if slice_posts else None

            edges = [{"node": p, "cursor": p["id"]} for p in slice_posts]
            resp_data = {
                "data": {
                    "topic": {
                        "id": topic["id"],
                        "name": topic["name"],
                        "description": topic["description"],
                        "posts": {
                            "edges": edges,
                            "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                        },
                    }
                }
            }
            if errors_payload:
                resp_data["errors"] = errors_payload
            return httpx.Response(200, json=resp_data)

        # Handling Organization posts query
        first = variables.get("first", 50)
        after = variables.get("after")

        start_idx = 0
        if after:
            for idx, p in enumerate(self.posts):
                if p["id"] == after:
                    start_idx = idx + 1
                    break

        slice_posts = self.posts[start_idx : start_idx + first]
        has_next = (start_idx + len(slice_posts)) < len(self.posts)
        end_cursor = slice_posts[-1]["id"] if slice_posts else None

        edges = [{"node": p, "cursor": p["id"]} for p in slice_posts]
        resp_data = {
            "data": {
                "organization": {
                    "posts": {
                        "edges": edges,
                        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                    }
                }
            }
        }
        if errors_payload:
            resp_data["errors"] = errors_payload
        return httpx.Response(200, json=resp_data)


@pytest.fixture
def fake_slab():
    return FakeSlabServer()


@pytest.fixture
def slab_client(fake_slab):
    transport = httpx.MockTransport(fake_slab.handle_request)
    return httpx.Client(
        headers={"Authorization": "valid_slab_token", "Content-Type": "application/json"},
        transport=transport,
    )
