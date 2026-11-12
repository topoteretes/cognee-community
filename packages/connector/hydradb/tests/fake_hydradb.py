"""Deterministic in-memory mock client for HydraDB API tests."""

from __future__ import annotations

from typing import Any

from cognee_community_connector_hydradb.hydradb import (
    HydraDBAPIError,
    HydraDBAuthError,
    HydraDBNotFoundError,
    HydraDBRateLimitError,
)


class FakeHydraDBClient:
    """Mock client returning canned HydraDB responses for testing."""

    def __init__(
        self,
        should_fail: bool = False,
        error_type: str | None = None,
    ) -> None:
        self.should_fail = should_fail
        self.error_type = error_type
        self.calls: list[dict[str, Any]] = []

    def ping(self) -> bool:
        return not self.should_fail

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append({"endpoint": endpoint, "params": params})

        if self.should_fail:
            if self.error_type == "auth":
                raise HydraDBAuthError("HydraDB authentication failed (401)")
            if self.error_type == "not_found":
                raise HydraDBNotFoundError("HydraDB resource not found (404)")
            if self.error_type == "rate_limit":
                raise HydraDBRateLimitError("HydraDB rate limit exceeded (429)")
            raise HydraDBAPIError("General HydraDB API failure")

        if endpoint.startswith("/contexts/ctx_"):
            cid = endpoint.split("/")[-1]
            return {
                "id": cid,
                "title": f"Detailed Context {cid}",
                "content": f"Full body text content for {cid} extracted from HydraDB inspect API.",
                "database_id": "test_db",
                "collection_id": "test_coll",
                "category": "knowledge",
            }

        if "/contexts" in endpoint:
            page = (params or {}).get("page", 1)
            if page > 2:
                return {"contexts": [], "has_more": False}

            if page == 1:
                return {
                    "contexts": [
                        {
                            "id": "ctx_001",
                            "title": "Architecture Overview",
                            "content": (
                                "Overview of autonomous agent architecture and memory layers."
                            ),
                            "database_id": "core_engine",
                            "collection_id": "architecture",
                            "category": "knowledge",
                            "created_at": "2026-11-01T12:00:00Z",
                        },
                        {
                            "id": "ctx_002",
                            "title": "API Specification",
                            "content": None,  # Will trigger inspect_context
                            "database_id": "core_engine",
                            "collection_id": "api",
                            "category": "specs",
                            "created_at": "2026-11-02T14:30:00Z",
                        },
                    ],
                    "has_more": True,
                }
            elif page == 2:
                return {
                    "contexts": [
                        {
                            "id": "ctx_003",
                            "title": "Operational Guidelines",
                            "content": "Deployment and operational safety policies.",
                            "database_id": "ops",
                            "collection_id": "runbooks",
                            "category": "operations",
                            "created_at": "2026-11-03T09:15:00Z",
                        }
                    ],
                    "has_more": False,
                }

        return {}

    def list_contexts(
        self,
        database_id: str | None = None,
        collection_id: str | None = None,
        category: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "limit": limit}
        if category:
            params["category"] = category

        endpoint = "/contexts"
        if database_id and collection_id:
            endpoint = f"/databases/{database_id}/collections/{collection_id}/contexts"

        return self.get(endpoint, params=params)

    def inspect_context(self, context_id: str) -> dict[str, Any]:
        return self.get(f"/contexts/{context_id}")
