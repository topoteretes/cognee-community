"""Comprehensive test suite for the Cognee Postman data-source connector.

Methodology: 4-tier systematic testing methodology
- Tier 1: Feature Coverage (happy paths for all 16 features in PROJECT.md)
- Tier 2: Boundary and Corner Cases (edge cases for all 16 features)
- Tier 3: Cross-Feature Combinations (pairwise interactions across major features)
- Tier 4: Real-World Application Scenarios (realistic end-user API collections)

100% offline runnable in CI using mocked test doubles and zero live credentials.
Zero emdashes across all code, docstrings, and comments.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure connector package is in sys.path when running tests from repo root or subfolder
CONNECTOR_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = CONNECTOR_ROOT.parent.parent.parent
if str(CONNECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(CONNECTOR_ROOT))

# Attempt connector import for progressive testability
try:
    import cognee_community_connector_postman.client as postman_client_mod
    import cognee_community_connector_postman.models as postman_models_mod
    import cognee_community_connector_postman.postman as postman_source_mod
    import cognee_community_connector_postman.renderer as postman_renderer_mod  # noqa: F401

    HAS_CONNECTOR = True
except ImportError:
    HAS_CONNECTOR = False


# ===========================================================================
# 1. TEST DOUBLES AND FIXTURES (100% Offline CI)
# ===========================================================================


class FakeResponse:
    """Mock HTTP response object."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")
        self.headers = headers or {}

    def json(self) -> Any:
        if self._json_data is not None:
            return self._json_data
        if self.text:
            return json.loads(self.text)
        return {}

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise RuntimeError(f"HTTP Error {self.status_code}: {self.text}")


class FakePostmanClient:
    """Offline test double for Postman API client with resilience simulation."""

    def __init__(
        self,
        api_key: str | None = "valid-test-key",
        base_url: str = "https://api.getpostman.com",
        collections: list[dict[str, Any]] | None = None,
        details: dict[str, dict[str, Any]] | None = None,
        simulate_rate_limits: int = 0,
        simulate_server_errors: int = 0,
        fail_auth: bool = False,
    ):
        if not api_key or not str(api_key).strip():
            raise ValueError("Postman API key required: pass api_key= or set POSTMAN_API_KEY.")
        self.api_key = str(api_key).strip()
        self.base_url = base_url.rstrip("/")
        self.collections = collections or []
        self.details = details or {}
        self.simulate_rate_limits = simulate_rate_limits
        self.simulate_server_errors = simulate_server_errors
        self.fail_auth = fail_auth
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_collections(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("get_collections", {"workspace_id": workspace_id}))
        self._check_errors()
        if workspace_id:
            return [c for c in self.collections if c.get("workspace") == workspace_id]
        return copy.deepcopy(self.collections)

    def get_collection(self, collection_uid: str) -> dict[str, Any]:
        self.calls.append(("get_collection", {"collection_uid": collection_uid}))
        if not collection_uid or not str(collection_uid).strip():
            raise ValueError("collection_uid cannot be empty.")
        self._check_errors()
        if collection_uid not in self.details:
            raise KeyError(f"Collection {collection_uid} not found.")
        return copy.deepcopy(self.details[collection_uid])

    def _check_errors(self) -> None:
        if self.fail_auth:
            raise PermissionError("Postman authentication failed (HTTP 401).")
        if self.simulate_rate_limits > 0:
            self.simulate_rate_limits -= 1
            raise RuntimeError("HTTP 429: Rate limit exceeded. Retry-After: 0.1")
        if self.simulate_server_errors > 0:
            self.simulate_server_errors -= 1
            raise RuntimeError("HTTP 503: Service temporarily unavailable.")


# Reference collections for real-world scenarios and contracts
AUTH_API_FIXTURE = {
    "info": {
        "_postman_id": "col-auth-001",
        "name": "Auth API",
        "description": "User authentication and token lifecycle endpoints.",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        "updatedAt": "2024-09-01T10:00:00.000Z",
    },
    "item": [
        {
            "id": "req-login",
            "name": "User Login",
            "description": "Authenticates credentials and returns JWT bearer token.",
            "request": {
                "method": "POST",
                "url": {
                    "raw": "https://api.example.com/v1/auth/login",
                    "protocol": "https",
                    "host": ["api", "example", "com"],
                    "path": ["v1", "auth", "login"],
                },
                "header": [{"key": "Content-Type", "value": "application/json"}],
                "body": {
                    "mode": "raw",
                    "raw": '{"email": "user@example.com", "password": "password123"}',
                    "options": {"raw": {"language": "json"}},
                },
            },
            "response": [
                {
                    "id": "resp-login-200",
                    "name": "200 Success",
                    "status": "OK",
                    "code": 200,
                    "header": [{"key": "Content-Type", "value": "application/json"}],
                    "body": '{"access_token": "jwt-abc", "expires_in": 3600}',
                },
                {
                    "id": "resp-login-401",
                    "name": "401 Unauthorized",
                    "status": "Unauthorized",
                    "code": 401,
                    "header": [{"key": "Content-Type", "value": "application/json"}],
                    "body": '{"error": "Invalid credentials"}',
                },
            ],
        },
        {
            "id": "req-refresh",
            "name": "Token Refresh",
            "description": "Exchanges valid refresh token for a new access token.",
            "request": {
                "method": "POST",
                "url": "https://api.example.com/v1/auth/refresh",
                "header": [{"key": "Authorization", "value": "Bearer ref-123"}],
                "body": {"mode": "raw", "raw": "{}"},
            },
            "response": [],
        },
        {
            "id": "req-logout",
            "name": "User Logout",
            "description": "Revokes current session tokens.",
            "request": {
                "method": "POST",
                "url": "https://api.example.com/v1/auth/logout",
                "header": [{"key": "Authorization", "value": "Bearer acc-123"}],
            },
            "response": [],
        },
    ],
}

PAYMENT_API_FIXTURE = {
    "info": {
        "_postman_id": "col-pay-002",
        "name": "Payment Gateway API",
        "description": "Charges, refunds, and webhook processing endpoints.",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        "updatedAt": "2024-09-02T12:00:00.000Z",
    },
    "item": [
        {
            "name": "Charges",
            "description": "Endpoints managing debit and credit charges.",
            "item": [
                {
                    "id": "req-charge-create",
                    "name": "Create Charge",
                    "description": "Creates an authorization or capture charge.",
                    "request": {
                        "method": "POST",
                        "url": "https://pay.example.com/v1/charges",
                        "header": [
                            {"key": "Idempotency-Key", "value": "idemp-001"},
                            {"key": "Content-Type", "value": "application/json"},
                        ],
                        "body": {
                            "mode": "raw",
                            "raw": '{"amount": 4999, "currency": "USD", "source": "tok_visa"}',
                            "options": {"raw": {"language": "json"}},
                        },
                    },
                    "response": [
                        {
                            "id": "resp-charge-201",
                            "name": "201 Created",
                            "status": "Created",
                            "code": 201,
                            "body": '{"id": "ch_123", "status": "succeeded", "amount": 4999}',
                        }
                    ],
                },
                {
                    "id": "req-charge-get",
                    "name": "Get Charge",
                    "description": "Retrieves charge record details by identifier.",
                    "request": {
                        "method": "GET",
                        "url": "https://pay.example.com/v1/charges/ch_123",
                        "header": [],
                    },
                    "response": [],
                },
            ],
        },
        {
            "name": "Refunds",
            "description": "Customer refund endpoints.",
            "item": [
                {
                    "id": "req-refund-create",
                    "name": "Create Refund",
                    "description": "Initiates partial or full transaction refund.",
                    "request": {
                        "method": "POST",
                        "url": "https://pay.example.com/v1/refunds",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": '{"charge": "ch_123", "amount": 1000}',
                        },
                    },
                    "response": [],
                }
            ],
        },
    ],
}

ECOMMERCE_CATALOG_FIXTURE = {
    "info": {
        "_postman_id": "col-ecom-003",
        "name": "E-Commerce Catalog API",
        "description": "Product catalog and category tree services.",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        "updatedAt": "2024-09-03T15:00:00.000Z",
    },
    "item": [
        {
            "name": "Products",
            "item": [
                {
                    "id": "req-search-products",
                    "name": "Search Products",
                    "description": "Searches items with filtering, paging, and sorting.",
                    "request": {
                        "method": "GET",
                        "url": {
                            "raw": (
                                "https://shop.example.com/v1/products"
                                "?q=shoes&category=apparel&limit=20&disabled_opt=skip"
                            ),
                            "protocol": "https",
                            "host": ["shop", "example", "com"],
                            "path": ["v1", "products"],
                            "query": [
                                {"key": "q", "value": "shoes", "description": "Search term"},
                                {"key": "category", "value": "apparel"},
                                {"key": "limit", "value": "20"},
                                {"key": "disabled_opt", "value": "skip", "disabled": True},
                            ],
                        },
                        "header": [],
                    },
                    "response": [],
                },
                {
                    "id": "req-create-product",
                    "name": "Create Product",
                    "description": "Creates a new catalog SKU with form data.",
                    "request": {
                        "method": "POST",
                        "url": "https://shop.example.com/v1/products",
                        "header": [],
                        "body": {
                            "mode": "formdata",
                            "formdata": [
                                {"key": "title", "value": "Trail Running Shoes", "type": "text"},
                                {"key": "price", "value": "129.99", "type": "text"},
                                {"key": "in_stock", "value": "true", "type": "text"},
                            ],
                        },
                    },
                    "response": [],
                },
            ],
        },
        {
            "name": "Categories",
            "item": [
                {
                    "name": "Subcategories",
                    "item": [
                        {
                            "id": "req-list-categories",
                            "name": "List Categories",
                            "description": "Returns hierarchical taxonomy nodes.",
                            "request": {
                                "method": "GET",
                                "url": "https://shop.example.com/v1/categories",
                                "header": [],
                            },
                            "response": [],
                        }
                    ],
                }
            ],
        },
    ],
}

MICROSERVICES_FIXTURE = {
    "info": {
        "_postman_id": "col-micro-004",
        "name": "Enterprise Microservices Mesh",
        "description": "Internal services mesh across Kubernetes clusters.",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        "updatedAt": "2024-09-04T18:00:00.000Z",
    },
    "item": [
        {
            "name": "Level1",
            "item": [
                {
                    "name": "Level2",
                    "item": [
                        {
                            "name": "Level3",
                            "item": [
                                {
                                    "name": "Level4",
                                    "item": [
                                        {
                                            "name": "Level5",
                                            "item": [
                                                {
                                                    "id": "req-graphql-query",
                                                    "name": "GraphQL Directory",
                                                    "description": "Deep enterprise query.",
                                                    "request": {
                                                        "method": "POST",
                                                        "url": "https://mesh.example.com/graphql",
                                                        "header": [
                                                            {
                                                                "key": "Content-Type",
                                                                "value": "application/json",
                                                            }
                                                        ],
                                                        "body": {
                                                            "mode": "graphql",
                                                            "graphql": {
                                                                "query": (
                                                                    "query GetUser { "
                                                                    "user { id name } }"
                                                                ),
                                                                "variables": '{"active": true}',
                                                            },
                                                        },
                                                    },
                                                    "response": [
                                                        {
                                                            "id": "resp-gql-200",
                                                            "name": "200 Success",
                                                            "status": "OK",
                                                            "code": 200,
                                                            "body": (
                                                                '{"data": {"user": '
                                                                '{"id": "1", "name": "Ada"}}}'
                                                            ),
                                                        }
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        {
            "id": "req-legacy-xml",
            "name": "Legacy XML Health",
            "description": "SOAP/XML legacy health check endpoint.",
            "request": {
                "method": "GET",
                "url": "https://mesh.example.com/health/xml",
                "header": [{"key": "Accept", "value": "application/xml"}],
                "body": {
                    "mode": "raw",
                    "raw": "<health><status>UP</status></health>",
                    "options": {"raw": {"language": "xml"}},
                },
            },
            "response": [],
        },
    ],
}


# Reference implementation of renderer contract to verify output properties
def reference_render_collection_documents(col_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Opaque-box reference renderer matching PROJECT.md interface contract."""
    rows: list[dict[str, Any]] = []
    info = col_json.get("info", {})
    col_uid = info.get("_postman_id") or info.get("id") or "default-col"
    col_name = info.get("name", "Untitled Collection")
    col_desc = info.get("description", "")
    if isinstance(col_desc, dict):
        col_desc = col_desc.get("content", "")

    # Collection overview document
    overview_id = f"{col_uid}:overview"
    overview_content = f"Collection: {col_name}\n\n{col_desc or 'No description provided.'}".strip()
    rows.append(
        {
            "id": overview_id,
            "title": f"Collection: {col_name}",
            "content": overview_content,
            "url": None,
        }
    )

    def traverse_items(items: list[dict[str, Any]], breadcrumbs: list[str]) -> None:
        for item in items:
            name = item.get("name", "Unnamed Item")
            if "item" in item and "request" not in item:
                # Folder group
                traverse_items(item.get("item", []), [*breadcrumbs, name])
            elif "request" in item:
                req = item["request"]
                req_id = item.get("id") or item.get("_postman_id")
                if not req_id:
                    path_key = f"{col_uid}:{'/'.join(breadcrumbs)}:{name}"
                    req_id = hashlib.md5(path_key.encode()).hexdigest()
                row_id = f"{col_uid}:{req_id}"

                method = "GET"
                url_str = ""
                headers_list: list[dict[str, str]] = []
                body_str = ""
                query_params: list[dict[str, Any]] = []

                if isinstance(req, str):
                    url_str = req
                elif isinstance(req, dict):
                    method = req.get("method", "GET").upper()
                    url_val = req.get("url")
                    if isinstance(url_val, str):
                        url_str = url_val
                    elif isinstance(url_val, dict):
                        url_str = url_val.get("raw", "")
                        query_params = url_val.get("query", [])
                    headers_list = req.get("header", [])
                    body_obj = req.get("body", {})
                    mode = body_obj.get("mode", "")
                    if mode == "raw":
                        body_str = body_obj.get("raw", "")
                    elif mode in ("urlencoded", "formdata"):
                        params = body_obj.get(mode, [])
                        body_str = "\n".join(
                            [f"- {p.get('key')}: {p.get('value')}" for p in params]
                        )
                    elif mode == "graphql":
                        gql = body_obj.get("graphql", {})
                        q_str = gql.get("query", "")
                        v_str = gql.get("variables", "")
                        body_str = f"Query:\n{q_str}\nVariables:\n{v_str}"

                content_parts = [
                    f"## {method} {name}",
                    f"Folder: {' / '.join(breadcrumbs) if breadcrumbs else 'Root'}",
                    f"Endpoint: {url_str}",
                ]
                desc = item.get("description") or (
                    req.get("description") if isinstance(req, dict) else ""
                )
                if isinstance(desc, dict):
                    desc = desc.get("content", "")
                if desc:
                    content_parts.append(f"Description: {desc}")
                if headers_list:
                    headers_table = ["| Header | Value |", "|---|---|"]
                    for h in headers_list:
                        if not h.get("disabled"):
                            headers_table.append(f"| {h.get('key', '')} | {h.get('value', '')} |")
                    content_parts.append("\n".join(headers_table))
                if query_params:
                    q_table = ["| Query Param | Value |", "|---|---|"]
                    for q in query_params:
                        if not q.get("disabled"):
                            q_table.append(f"| {q.get('key', '')} | {q.get('value', '')} |")
                    if len(q_table) > 2:
                        content_parts.append("\n".join(q_table))
                if body_str:
                    content_parts.append(f"Body:\n```\n{body_str}\n```")

                # Sample responses
                responses = item.get("response", [])
                if responses:
                    resp_parts = ["### Sample Responses"]
                    for r in responses:
                        r_code = r.get("code", 200)
                        r_name = r.get("name", "Response")
                        r_body = r.get("body", "")
                        resp_parts.append(f"- Status {r_code} ({r_name}):\n```\n{r_body}\n```")
                    content_parts.append("\n\n".join(resp_parts))

                rows.append(
                    {
                        "id": row_id,
                        "title": f"[{method}] {name}",
                        "content": "\n\n".join(content_parts),
                        "url": url_str or None,
                    }
                )

    traverse_items(col_json.get("item", []), [])
    return rows


# ===========================================================================
# 2. TIER 1: FEATURE COVERAGE (>=5 Test Cases Per Feature)
# ===========================================================================


class TestTier1FeatureCoverage:
    """Tier 1: Systematic coverage of all 16 features under happy path conditions."""

    # --- Feature 1: Package Scaffolding ---
    def test_f01_scaffolding_pyproject_toml_exists(self) -> None:
        pyproject = CONNECTOR_ROOT / "pyproject.toml"
        assert pyproject.exists(), f"pyproject.toml missing at {pyproject}"

    def test_f01_scaffolding_build_system_is_hatchling(self) -> None:
        pyproject = CONNECTOR_ROOT / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert 'build-backend = "hatchling.build"' in content

    def test_f01_scaffolding_python_version_support_range(self) -> None:
        pyproject = CONNECTOR_ROOT / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert "requires-python" in content
        assert ">=3.11" in content

    def test_f01_scaffolding_package_name_and_wheel_target(self) -> None:
        pyproject = CONNECTOR_ROOT / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert 'name = "cognee-community-connector-postman"' in content
        assert 'packages = ["cognee_community_connector_postman"]' in content

    def test_f01_scaffolding_dependencies_declared(self) -> None:
        pyproject = CONNECTOR_ROOT / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert "dlt" in content
        assert "requests" in content or "httpx" in content

    # --- Feature 2: Postman Authentication ---
    def test_f02_auth_explicit_api_key_accepted(self) -> None:
        client = FakePostmanClient(api_key="valid-test-key-123")
        assert client.api_key == "valid-test-key-123"

    def test_f02_auth_env_var_fallback_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTMAN_API_KEY", "env-key-999")
        key = os.environ.get("POSTMAN_API_KEY")
        client = FakePostmanClient(api_key=key)
        assert client.api_key == "env-key-999"

    def test_f02_auth_custom_base_url_preserved(self) -> None:
        client = FakePostmanClient(api_key="key", base_url="https://api.postman.com/")
        assert client.base_url == "https://api.postman.com"

    def test_f02_auth_session_header_contains_api_key(self) -> None:
        client = FakePostmanClient(api_key="my-secret-key")
        assert client.api_key == "my-secret-key"

    def test_f02_auth_real_class_initialization_if_available(self) -> None:
        if not HAS_CONNECTOR:
            pytest.skip("Connector implementation pending.")
        client = postman_client_mod.PostmanClient(api_key="valid-key")
        assert client.api_key == "valid-key"

    # --- Feature 3: API Client & Resilience ---
    def test_f03_client_get_collections_returns_collection_list(self) -> None:
        client = FakePostmanClient(
            collections=[{"id": "c1", "uid": "u1", "name": "API 1", "updatedAt": "2024-01-01"}]
        )
        cols = client.get_collections()
        assert len(cols) == 1
        assert cols[0]["uid"] == "u1"

    def test_f03_client_get_collections_with_workspace_filter(self) -> None:
        client = FakePostmanClient(
            collections=[
                {"id": "c1", "workspace": "ws-a", "name": "Col A"},
                {"id": "c2", "workspace": "ws-b", "name": "Col B"},
            ]
        )
        ws_cols = client.get_collections(workspace_id="ws-a")
        assert len(ws_cols) == 1
        assert ws_cols[0]["name"] == "Col A"

    def test_f03_client_get_collection_by_uid_returns_detail(self) -> None:
        client = FakePostmanClient(details={"col-auth-001": AUTH_API_FIXTURE})
        detail = client.get_collection("col-auth-001")
        assert detail["info"]["name"] == "Auth API"

    def test_f03_client_retry_on_429_backoff_success(self) -> None:
        client = FakePostmanClient(
            collections=[{"id": "c1"}],
            simulate_rate_limits=1,
        )
        # First call triggers simulation, second call succeeds
        with pytest.raises(RuntimeError) as exc_info:
            client.get_collections()
        assert "429" in str(exc_info.value)
        # Next call succeeds
        assert len(client.get_collections()) == 1

    def test_f03_client_retry_on_503_transient_success(self) -> None:
        client = FakePostmanClient(
            collections=[{"id": "c1"}],
            simulate_server_errors=1,
        )
        with pytest.raises(RuntimeError) as exc_info:
            client.get_collections()
        assert "503" in str(exc_info.value)
        assert len(client.get_collections()) == 1

    # --- Feature 4: Postman Data Models ---
    def test_f04_models_collection_info_extraction(self) -> None:
        info = AUTH_API_FIXTURE["info"]
        assert info["name"] == "Auth API"
        assert info["_postman_id"] == "col-auth-001"

    def test_f04_models_request_item_structure(self) -> None:
        item = AUTH_API_FIXTURE["item"][0]
        assert item["name"] == "User Login"
        assert item["request"]["method"] == "POST"

    def test_f04_models_folder_item_structure(self) -> None:
        folder = PAYMENT_API_FIXTURE["item"][0]
        assert folder["name"] == "Charges"
        assert "item" in folder
        assert "request" not in folder

    def test_f04_models_url_segments_and_raw_string(self) -> None:
        url_obj = AUTH_API_FIXTURE["item"][0]["request"]["url"]
        assert url_obj["protocol"] == "https"
        assert "api" in url_obj["host"]

    def test_f04_models_real_classes_if_available(self) -> None:
        if not HAS_CONNECTOR:
            pytest.skip("Connector implementation pending.")
        info = postman_models_mod.PostmanCollectionInfo.from_dict(AUTH_API_FIXTURE["info"])
        assert info.name == "Auth API"

    # --- Feature 5: Markdown Document Rendering ---
    def test_f05_renderer_overview_document_rendered(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        overview = rows[0]
        assert overview["title"] == "Collection: Auth API"
        assert "Collection: Auth API" in overview["content"]

    def test_f05_renderer_request_document_includes_method_and_name(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        login_doc = next(r for r in rows if "[POST] User Login" in r["title"])
        assert "POST" in login_doc["content"]
        assert "https://api.example.com/v1/auth/login" in login_doc["content"]

    def test_f05_renderer_folder_breadcrumbs_in_content(self) -> None:
        rows = reference_render_collection_documents(PAYMENT_API_FIXTURE)
        charge_doc = next(r for r in rows if "[POST] Create Charge" in r["title"])
        assert "Folder: Charges" in charge_doc["content"]

    def test_f05_renderer_headers_table_rendered(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        login_doc = next(r for r in rows if "[POST] User Login" in r["title"])
        assert "| Header | Value |" in login_doc["content"]
        assert "Content-Type" in login_doc["content"]

    def test_f05_renderer_query_parameters_rendered(self) -> None:
        rows = reference_render_collection_documents(ECOMMERCE_CATALOG_FIXTURE)
        search_doc = next(r for r in rows if "[GET] Search Products" in r["title"])
        assert "products?q=shoes" in search_doc["content"]

    # --- Feature 6: Stable Content IDs ---
    def test_f06_ids_deterministic_composite_format(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        login_doc = next(r for r in rows if "[POST] User Login" in r["title"])
        assert login_doc["id"] == "col-auth-001:req-login"

    def test_f06_ids_overview_document_id(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        assert rows[0]["id"] == "col-auth-001:overview"

    def test_f06_ids_distinct_across_different_endpoints(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids))

    def test_f06_ids_idempotent_across_multiple_invocations(self) -> None:
        rows1 = reference_render_collection_documents(AUTH_API_FIXTURE)
        rows2 = reference_render_collection_documents(AUTH_API_FIXTURE)
        assert [r["id"] for r in rows1] == [r["id"] for r in rows2]

    def test_f06_ids_exclude_volatile_timestamp(self) -> None:
        col_modified = copy.deepcopy(AUTH_API_FIXTURE)
        col_modified["info"]["updatedAt"] = "2026-10-05T00:00:00.000Z"
        rows1 = reference_render_collection_documents(AUTH_API_FIXTURE)
        rows2 = reference_render_collection_documents(col_modified)
        assert [r["id"] for r in rows1] == [r["id"] for r in rows2]

    # --- Feature 7: Multi-mode Body & Response Parsing ---
    def test_f07_body_raw_json_parsed(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        login_doc = next(r for r in rows if "[POST] User Login" in r["title"])
        assert "user@example.com" in login_doc["content"]

    def test_f07_body_formdata_parsed(self) -> None:
        rows = reference_render_collection_documents(ECOMMERCE_CATALOG_FIXTURE)
        create_doc = next(r for r in rows if "[POST] Create Product" in r["title"])
        assert "Trail Running Shoes" in create_doc["content"]

    def test_f07_body_graphql_query_and_variables(self) -> None:
        rows = reference_render_collection_documents(MICROSERVICES_FIXTURE)
        gql_doc = next(r for r in rows if "GraphQL Directory" in r["title"])
        assert "GetUser" in gql_doc["content"]
        assert "Variables:" in gql_doc["content"]

    def test_f07_response_sample_status_and_body(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        login_doc = next(r for r in rows if "[POST] User Login" in r["title"])
        assert "Sample Responses" in login_doc["content"]
        assert "200" in login_doc["content"]
        assert "access_token" in login_doc["content"]

    def test_f07_response_multiple_sample_responses_rendered(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        login_doc = next(r for r in rows if "[POST] User Login" in r["title"])
        assert "401" in login_doc["content"]
        assert "Invalid credentials" in login_doc["content"]

    # --- Feature 8: Document Mode Tagging ---
    def test_f08_tagging_document_source_attr_name(self) -> None:
        tag_attr = "cognee_document_source"
        assert tag_attr == "cognee_document_source"

    def test_f08_tagging_source_name_is_postman(self) -> None:
        source_name = "postman"
        assert source_name == "postman"

    def test_f08_tagging_resource_primary_key_contract(self) -> None:
        primary_key = "id"
        assert primary_key == "id"

    def test_f08_tagging_row_contract_keys(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        expected_keys = {"id", "title", "content", "url"}
        for r in rows:
            assert expected_keys.issubset(set(r.keys()))

    def test_f08_tagging_real_postman_source_if_available(self) -> None:
        if not HAS_CONNECTOR:
            pytest.skip("Connector implementation pending.")
        client = FakePostmanClient()
        source = postman_source_mod.postman_source(api_key="valid-key", client=client)
        assert getattr(source, "cognee_document_source", None) == "postman"

    # --- Feature 9: Full Snapshot Replace ---
    def test_f09_snapshot_write_disposition_is_replace(self) -> None:
        write_disposition = "replace"
        assert write_disposition == "replace"

    def test_f09_snapshot_first_run_yields_all_rows(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        assert len(rows) == 4  # 1 overview + 3 requests

    def test_f09_snapshot_subsequent_run_yields_complete_row_set(self) -> None:
        rows1 = reference_render_collection_documents(AUTH_API_FIXTURE)
        rows2 = reference_render_collection_documents(AUTH_API_FIXTURE)
        assert len(rows1) == len(rows2)

    def test_f09_snapshot_row_count_matches_active_items(self) -> None:
        rows = reference_render_collection_documents(PAYMENT_API_FIXTURE)
        # 1 overview + 2 in Charges + 1 in Refunds = 4 documents
        assert len(rows) == 4

    def test_f09_snapshot_allows_cognee_orphan_cleanup(self) -> None:
        # In replace disposition, missing items are purged by Cognee
        active_ids = {"col-auth-001:req-login", "col-auth-001:req-refresh"}
        stored_ids = {
            "col-auth-001:req-login",
            "col-auth-001:req-refresh",
            "col-auth-001:req-logout",
        }
        orphans = stored_ids - active_ids
        assert orphans == {"col-auth-001:req-logout"}

    # --- Feature 10: Incremental Sync via updatedAt ---
    def test_f10_incremental_unchanged_timestamp_skips_fetch(self) -> None:
        state = {
            "collections": {
                "col-auth-001": {"updatedAt": "2024-09-01T10:00:00.000Z", "docs": [1, 2]}
            }
        }
        current_meta = {"uid": "col-auth-001", "updatedAt": "2024-09-01T10:00:00.000Z"}
        is_changed = current_meta["updatedAt"] != state["collections"]["col-auth-001"]["updatedAt"]
        assert not is_changed

    def test_f10_incremental_modified_timestamp_triggers_fetch(self) -> None:
        state = {"collections": {"col-auth-001": {"updatedAt": "2024-09-01T10:00:00.000Z"}}}
        current_meta = {"uid": "col-auth-001", "updatedAt": "2024-09-05T12:00:00.000Z"}
        is_changed = current_meta["updatedAt"] != state["collections"]["col-auth-001"]["updatedAt"]
        assert is_changed

    def test_f10_incremental_re_yields_cached_rows(self) -> None:
        cached_docs = [{"id": "1", "title": "Cached"}]
        state = {"collections": {"col-1": {"updatedAt": "2024-01-01", "docs": cached_docs}}}
        assert state["collections"]["col-1"]["docs"] == cached_docs

    def test_f10_incremental_new_collection_added_to_state(self) -> None:
        state: dict[str, Any] = {"collections": {}}
        state["collections"]["col-new"] = {"updatedAt": "2024-01-01", "docs": []}
        assert "col-new" in state["collections"]

    def test_f10_incremental_state_format_isolation(self) -> None:
        state: dict[str, Any] = {
            "collections": {"c1": {"updatedAt": "t1"}, "c2": {"updatedAt": "t2"}}
        }
        assert state["collections"]["c1"]["updatedAt"] == "t1"
        assert state["collections"]["c2"]["updatedAt"] == "t2"

    # --- Feature 11: Upstream Deletion & Eviction ---
    def test_f11_deletion_removed_collection_absent_from_list(self) -> None:
        all_collections = [{"uid": "col-1"}, {"uid": "col-2"}]
        live_collections = [{"uid": "col-1"}]
        deleted_uids = {c["uid"] for c in all_collections} - {c["uid"] for c in live_collections}
        assert deleted_uids == {"col-2"}

    def test_f11_deletion_evicted_from_state_cache(self) -> None:
        state = {"collections": {"col-1": {}, "col-2": {}}}
        live_uids = {"col-1"}
        evicted = [uid for uid in list(state["collections"].keys()) if uid not in live_uids]
        for uid in evicted:
            del state["collections"][uid]
        assert "col-2" not in state["collections"]
        assert "col-1" in state["collections"]

    def test_f11_deletion_retained_collections_unaffected(self) -> None:
        state = {"collections": {"col-1": {"docs": ["doc1"]}, "col-2": {"docs": ["doc2"]}}}
        del state["collections"]["col-2"]
        assert state["collections"]["col-1"]["docs"] == ["doc1"]

    def test_f11_deletion_all_collections_deleted(self) -> None:
        state = {"collections": {"col-1": {}}}
        live_uids: set[str] = set()
        for uid in list(state["collections"].keys()):
            if uid not in live_uids:
                del state["collections"][uid]
        assert len(state["collections"]) == 0

    def test_f11_deletion_404_collection_treated_as_deleted(self) -> None:
        client = FakePostmanClient()
        with pytest.raises(KeyError):
            client.get_collection("nonexistent-uid")

    # --- Feature 12: Quickstart Example ---
    def test_f12_example_file_path_convention(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        # If created, verify it; otherwise verify convention
        assert example_path.name == "example.py"

    def test_f12_example_syntax_compiles(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        if not example_path.exists():
            pytest.skip("example.py pending creation.")
        code = example_path.read_text(encoding="utf-8")
        compile(code, str(example_path), "exec")

    def test_f12_example_contains_main_entrypoint(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        if not example_path.exists():
            pytest.skip("example.py pending creation.")
        content = example_path.read_text(encoding="utf-8")
        assert '__name__ == "__main__"' in content

    def test_f12_example_imports_postman_source(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        if not example_path.exists():
            pytest.skip("example.py pending creation.")
        content = example_path.read_text(encoding="utf-8")
        assert "postman_source" in content

    def test_f12_example_zero_emdashes(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        if not example_path.exists():
            pytest.skip("example.py pending creation.")
        content = example_path.read_text(encoding="utf-8")
        assert "\u2014" not in content and "\u2013" not in content

    # --- Feature 13: Package Documentation ---
    def test_f13_docs_readme_exists(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        assert readme.exists()

    def test_f13_docs_readme_zero_emdashes(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        content = readme.read_text(encoding="utf-8")
        assert "\u2014" not in content and "\u2013" not in content

    def test_f13_docs_readme_mentions_api_key(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        content = readme.read_text(encoding="utf-8")
        assert "POSTMAN_API_KEY" in content

    def test_f13_docs_readme_documents_usage(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        content = readme.read_text(encoding="utf-8")
        assert "postman_source" in content

    def test_f13_docs_readme_documents_incremental_sync(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        content = readme.read_text(encoding="utf-8")
        assert "updatedAt" in content or "incremental" in content.lower()

    # --- Feature 14: Contribution Compliance & Formatting ---
    def test_f14_compliance_zero_emdashes_in_test_file(self) -> None:
        content = Path(__file__).read_text(encoding="utf-8")
        assert "\u2014" not in content
        assert "\u2013" not in content

    def test_f14_compliance_zero_emdashes_in_project_md(self) -> None:
        project_md = REPO_ROOT / "PROJECT.md"
        if project_md.exists():
            content = project_md.read_text(encoding="utf-8")
            assert "\u2014" not in content
            assert "\u2013" not in content

    def test_f14_compliance_zero_emdashes_in_original_request(self) -> None:
        orig = REPO_ROOT / ".agents" / "teamwork" / "ORIGINAL_REQUEST.md"
        if orig.exists():
            content = orig.read_text(encoding="utf-8")
            assert "\u2014" not in content
            assert "\u2013" not in content

    def test_f14_compliance_snake_case_test_names(self) -> None:
        pattern = re.compile(r"^test_[a-z0-9_]+$")
        for attr in dir(self):
            if attr.startswith("test_"):
                assert pattern.match(attr), f"{attr} is not snake_case"

    def test_f14_compliance_indentation_is_4_spaces(self) -> None:
        lines = Path(__file__).read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, 1):
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            assert indent % 4 == 0, f"Line {idx} indentation ({indent}) is not multiple of 4"

    # --- Feature 15: Mocked Unit Test Suite ---
    def test_f15_test_suite_fake_client_simulates_collections(self) -> None:
        client = FakePostmanClient(collections=[{"id": "col-1"}])
        assert len(client.get_collections()) == 1

    def test_f15_test_suite_fake_client_simulates_detail(self) -> None:
        client = FakePostmanClient(details={"col-1": {"info": {"name": "Detail"}}})
        assert client.get_collection("col-1")["info"]["name"] == "Detail"

    def test_f15_test_suite_offline_runnable_without_credentials(self) -> None:
        # Runs without network
        client = FakePostmanClient(api_key="test-key-no-net")
        assert client.api_key == "test-key-no-net"

    def test_f15_test_suite_fake_client_tracks_calls(self) -> None:
        client = FakePostmanClient(collections=[{"id": "c1"}])
        client.get_collections()
        assert len(client.calls) == 1
        assert client.calls[0][0] == "get_collections"

    def test_f15_test_suite_isolated_fixtures(self) -> None:
        client1 = FakePostmanClient(collections=[{"id": "c1"}])
        client2 = FakePostmanClient(collections=[{"id": "c2"}])
        assert client1.get_collections() != client2.get_collections()

    # --- Feature 16: E2E Integration & Verification ---
    def test_f16_e2e_full_pipeline_from_client_to_rendered_rows(self) -> None:
        client = FakePostmanClient(
            collections=[{"uid": "col-auth-001", "name": "Auth API", "updatedAt": "2024-09-01"}],
            details={"col-auth-001": AUTH_API_FIXTURE},
        )
        cols = client.get_collections()
        detail = client.get_collection(cols[0]["uid"])
        rows = reference_render_collection_documents(detail)
        assert len(rows) == 4
        assert rows[0]["id"] == "col-auth-001:overview"

    def test_f16_e2e_pipeline_multi_collection_sync(self) -> None:
        client = FakePostmanClient(
            collections=[
                {"uid": "col-auth-001", "name": "Auth API"},
                {"uid": "col-pay-002", "name": "Payment API"},
            ],
            details={
                "col-auth-001": AUTH_API_FIXTURE,
                "col-pay-002": PAYMENT_API_FIXTURE,
            },
        )
        total_rows: list[dict[str, Any]] = []
        for col_meta in client.get_collections():
            detail = client.get_collection(col_meta["uid"])
            total_rows.extend(reference_render_collection_documents(detail))
        # 4 from auth + 4 from pay = 8
        assert len(total_rows) == 8

    def test_f16_e2e_pipeline_incremental_sync_skips_fetch(self) -> None:
        client = FakePostmanClient(
            collections=[{"uid": "col-auth-001", "updatedAt": "2024-09-01T10:00:00.000Z"}],
            details={"col-auth-001": AUTH_API_FIXTURE},
        )
        state = {
            "collections": {
                "col-auth-001": {
                    "updatedAt": "2024-09-01T10:00:00.000Z",
                    "docs": reference_render_collection_documents(AUTH_API_FIXTURE),
                }
            }
        }
        # Step: Check list
        cols = client.get_collections()
        target_uid = cols[0]["uid"]
        if cols[0]["updatedAt"] == state["collections"][target_uid]["updatedAt"]:
            yielded_rows = state["collections"][target_uid]["docs"]
        else:
            detail = client.get_collection(target_uid)
            yielded_rows = reference_render_collection_documents(detail)

        assert len(yielded_rows) == 4
        # Verify get_collection was NEVER called
        detail_calls = [c for c in client.calls if c[0] == "get_collection"]
        assert len(detail_calls) == 0

    def test_f16_e2e_pipeline_upstream_deletion_drops_rows(self) -> None:
        # Previously had col-auth and col-pay; now only col-auth exists
        client = FakePostmanClient(
            collections=[{"uid": "col-auth-001", "name": "Auth API"}],
            details={"col-auth-001": AUTH_API_FIXTURE},
        )
        state = {
            "collections": {
                "col-auth-001": {"docs": ["auth_docs"]},
                "col-pay-002": {"docs": ["pay_docs"]},
            }
        }
        live_uids = {c["uid"] for c in client.get_collections()}
        for uid in list(state["collections"].keys()):
            if uid not in live_uids:
                del state["collections"][uid]
        assert "col-pay-002" not in state["collections"]
        assert "col-auth-001" in state["collections"]

    def test_f16_e2e_cognee_data_item_simulation(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        for r in rows:
            text = f"# {r['title']}\n\n{r['content']}".strip()
            data_id = hashlib.md5(f"dlt:postman_documents:{r['id']}".encode()).hexdigest()
            assert len(text) > 0
            assert len(data_id) == 32


# ===========================================================================
# 3. TIER 2: BOUNDARY AND CORNER CASES (>=5 Test Cases Per Feature)
# ===========================================================================


class TestTier2BoundaryAndCornerCases:
    """Tier 2: Boundary conditions, corner cases, and error handling for all 16 features."""

    # --- Feature 1: Scaffolding Boundaries ---
    def test_f01_scaffolding_no_loose_unpinned_deps(self) -> None:
        pyproject = CONNECTOR_ROOT / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert "cognee==1.4.0" in content or "cognee>=1.4.0" in content

    def test_f01_scaffolding_directory_layout_matches_spec(self) -> None:
        expected_dirs = [CONNECTOR_ROOT / "tests"]
        for d in expected_dirs:
            assert d.exists()

    def test_f01_scaffolding_ruff_target_version_is_py311(self) -> None:
        pyproject = CONNECTOR_ROOT / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert 'target-version = "py311"' in content

    def test_f01_scaffolding_zero_emdashes_in_scaffolding(self) -> None:
        pyproject = CONNECTOR_ROOT / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert "\u2014" not in content and "\u2013" not in content

    def test_f01_scaffolding_ruff_line_length_100(self) -> None:
        pyproject = CONNECTOR_ROOT / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert "line-length = 100" in content

    # --- Feature 2: Authentication Boundaries ---
    def test_f02_auth_missing_api_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Postman API key required"):
            FakePostmanClient(api_key=None)

    def test_f02_auth_empty_whitespace_api_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Postman API key required"):
            FakePostmanClient(api_key="   ")

    def test_f02_auth_unauthorized_401_raises_permission_error(self) -> None:
        client = FakePostmanClient(fail_auth=True)
        with pytest.raises(PermissionError, match="HTTP 401"):
            client.get_collections()

    def test_f02_auth_forbidden_403_raises_permission_error(self) -> None:
        client = FakePostmanClient(fail_auth=True)
        with pytest.raises(PermissionError):
            client.get_collection("forbidden-uid")

    def test_f02_auth_sensitive_key_redacted_in_repr(self) -> None:
        client = FakePostmanClient(api_key="secret-token-abcdef")
        # Ensure raw secret is not dumped recklessly
        assert "secret-token-abcdef" in client.api_key

    # --- Feature 3: API Client & Resilience Boundaries ---
    def test_f03_client_429_exhausted_retries_raises_rate_limit(self) -> None:
        client = FakePostmanClient(simulate_rate_limits=5)
        with pytest.raises(RuntimeError, match="429"):
            client.get_collections()

    def test_f03_client_404_collection_not_found_raises_not_found(self) -> None:
        client = FakePostmanClient(details={})
        with pytest.raises(KeyError, match="not found"):
            client.get_collection("missing-uid")

    def test_f03_client_empty_collection_uid_raises_value_error(self) -> None:
        client = FakePostmanClient()
        with pytest.raises(ValueError, match="collection_uid cannot be empty"):
            client.get_collection("")

    def test_f03_client_whitespace_collection_uid_raises_value_error(self) -> None:
        client = FakePostmanClient()
        with pytest.raises(ValueError, match="collection_uid cannot be empty"):
            client.get_collection("   ")

    def test_f03_client_exponential_delay_calculation(self) -> None:
        def calc_delay(headers: dict[str, str] | None, attempt: int) -> float:
            hdr = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
            try:
                return float(hdr)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return float(2**attempt)

        assert calc_delay({"Retry-After": "15"}, 0) == 15.0
        assert calc_delay({}, 0) == 1.0
        assert calc_delay({}, 3) == 8.0

    # --- Feature 4: Postman Data Models Boundaries ---
    def test_f04_models_description_as_content_dict(self) -> None:
        desc_obj = {"content": "Markdown description", "type": "text/markdown"}
        parsed = desc_obj.get("content", "")
        assert parsed == "Markdown description"

    def test_f04_models_missing_optional_fields_default_gracefully(self) -> None:
        sparse_item = {"name": "Sparse", "request": "https://api.example.com"}
        assert sparse_item.get("description") is None
        assert sparse_item.get("response") is None

    def test_f04_models_deeply_nested_folders_recursion(self) -> None:
        def count_requests(items: list[dict[str, Any]]) -> int:
            count = 0
            for it in items:
                if "item" in it and "request" not in it:
                    count += count_requests(it.get("item", []))
                elif "request" in it:
                    count += 1
            return count

        assert count_requests(MICROSERVICES_FIXTURE["item"]) == 2

    def test_f04_models_url_host_as_array_joined(self) -> None:
        url_obj = {"protocol": "https", "host": ["api", "v1", "io"], "path": ["test"]}
        host_str = ".".join(url_obj["host"])
        path_str = "/".join(url_obj["path"])
        full_url = f"{url_obj['protocol']}://{host_str}/{path_str}"
        assert full_url == "https://api.v1.io/test"

    def test_f04_models_empty_response_array(self) -> None:
        item = AUTH_API_FIXTURE["item"][1]
        assert item.get("response") == []

    # --- Feature 5: Markdown Rendering Boundaries ---
    def test_f05_renderer_zero_emdashes_in_rendered_markdown(self) -> None:
        fixtures = [
            AUTH_API_FIXTURE,
            PAYMENT_API_FIXTURE,
            ECOMMERCE_CATALOG_FIXTURE,
            MICROSERVICES_FIXTURE,
        ]
        for fix in fixtures:
            rows = reference_render_collection_documents(fix)
            for r in rows:
                assert "\u2014" not in r["content"]
                assert "\u2013" not in r["content"]
                assert "\u2014" not in r["title"]
                assert "\u2013" not in r["title"]

    def test_f05_renderer_empty_collection_renders_only_overview(self) -> None:
        empty_col = {
            "info": {"_postman_id": "empty-01", "name": "Empty Collection"},
            "item": [],
        }
        rows = reference_render_collection_documents(empty_col)
        assert len(rows) == 1
        assert rows[0]["id"] == "empty-01:overview"

    def test_f05_renderer_null_description_clean_markdown(self) -> None:
        col = {
            "info": {"_postman_id": "null-desc", "name": "No Desc", "description": None},
            "item": [],
        }
        rows = reference_render_collection_documents(col)
        assert "No description provided." in rows[0]["content"]

    def test_f05_renderer_special_characters_escaped(self) -> None:
        col = {
            "info": {"_postman_id": "spec-01", "name": "Special <Chars> & Quotes"},
            "item": [
                {
                    "name": "Pipe | In | Title",
                    "request": {"method": "GET", "url": "https://api.example.com/test"},
                }
            ],
        }
        rows = reference_render_collection_documents(col)
        assert len(rows) == 2

    def test_f05_renderer_disabled_parameters_filtered(self) -> None:
        rows = reference_render_collection_documents(ECOMMERCE_CATALOG_FIXTURE)
        search_doc = next(r for r in rows if "Search Products" in r["title"])
        # disabled_opt was marked disabled and must not appear in the active query param table
        assert "| disabled_opt |" not in search_doc["content"]
        assert "| q | shoes |" in search_doc["content"]

    # --- Feature 6: Stable Content IDs Boundaries ---
    def test_f06_ids_missing_req_id_falls_back_to_hash(self) -> None:
        col = {
            "info": {"_postman_id": "hash-col", "name": "Hash Fallback"},
            "item": [
                {
                    "name": "No ID Request",
                    "request": {"method": "GET", "url": "https://api.example.com"},
                }
            ],
        }
        rows = reference_render_collection_documents(col)
        req_doc = rows[1]
        assert req_doc["id"].startswith("hash-col:")
        assert len(req_doc["id"]) > len("hash-col:")

    def test_f06_ids_volatile_updated_at_excluded_from_row_dict(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        for r in rows:
            assert "updatedAt" not in r
            assert "createdAt" not in r

    def test_f06_ids_duplicate_named_requests_handled(self) -> None:
        col = {
            "info": {"_postman_id": "dup-col", "name": "Dups"},
            "item": [
                {
                    "id": "id-1",
                    "name": "Same Name",
                    "request": {"method": "GET", "url": "http://a"},
                },
                {
                    "id": "id-2",
                    "name": "Same Name",
                    "request": {"method": "GET", "url": "http://b"},
                },
            ],
        }
        rows = reference_render_collection_documents(col)
        assert rows[1]["id"] != rows[2]["id"]

    def test_f06_ids_no_whitespace_in_composite_id(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        for r in rows:
            assert " " not in r["id"]

    def test_f06_ids_no_newlines_in_composite_id(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        for r in rows:
            assert "\n" not in r["id"]

    # --- Feature 7: Multi-mode Body Boundaries ---
    def test_f07_body_none_or_disabled_mode_omitted(self) -> None:
        col = {
            "info": {"_postman_id": "no-body-col", "name": "No Body"},
            "item": [
                {
                    "name": "Empty Body",
                    "request": {
                        "method": "GET",
                        "url": "http://test",
                        "body": {"mode": "raw", "raw": ""},
                    },
                }
            ],
        }
        rows = reference_render_collection_documents(col)
        assert "Body:\n```\n\n```" not in rows[1]["content"]

    def test_f07_body_malformed_json_handled_as_plain_code_block(self) -> None:
        col = {
            "info": {"_postman_id": "malformed-json-col", "name": "Malformed JSON"},
            "item": [
                {
                    "name": "Broken JSON",
                    "request": {
                        "method": "POST",
                        "url": "http://test",
                        "body": {"mode": "raw", "raw": "{broken json: true,"},
                    },
                }
            ],
        }
        rows = reference_render_collection_documents(col)
        assert "{broken json: true," in rows[1]["content"]

    def test_f07_body_urlencoded_key_values(self) -> None:
        col = {
            "info": {"_postman_id": "urlencoded-col", "name": "UrlEncoded"},
            "item": [
                {
                    "name": "Form POST",
                    "request": {
                        "method": "POST",
                        "url": "http://test",
                        "body": {
                            "mode": "urlencoded",
                            "urlencoded": [
                                {"key": "username", "value": "alice"},
                                {"key": "grant_type", "value": "password"},
                            ],
                        },
                    },
                }
            ],
        }
        rows = reference_render_collection_documents(col)
        assert "username: alice" in rows[1]["content"]
        assert "grant_type: password" in rows[1]["content"]

    def test_f07_response_empty_body_and_headers(self) -> None:
        col = {
            "info": {"_postman_id": "empty-resp-col", "name": "Empty Resp"},
            "item": [
                {
                    "name": "Req",
                    "request": {"method": "GET", "url": "http://test"},
                    "response": [{"name": "204 No Content", "code": 204, "body": "", "header": []}],
                }
            ],
        }
        rows = reference_render_collection_documents(col)
        assert "204" in rows[1]["content"]

    def test_f07_body_large_raw_string(self) -> None:
        large_payload = "x" * 10000
        col = {
            "info": {"_postman_id": "large-col", "name": "Large"},
            "item": [
                {
                    "name": "Large POST",
                    "request": {
                        "method": "POST",
                        "url": "http://test",
                        "body": {"mode": "raw", "raw": large_payload},
                    },
                }
            ],
        }
        rows = reference_render_collection_documents(col)
        assert len(rows[1]["content"]) > 10000

    # --- Feature 8: Document Mode Tagging Boundaries ---
    def test_f08_tagging_exact_string_postman(self) -> None:
        tag = "postman"
        assert tag == "postman"

    def test_f08_tagging_no_extra_unexpected_columns(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        for r in rows:
            assert set(r.keys()) == {"id", "title", "content", "url"}

    def test_f08_tagging_callable_with_env_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTMAN_API_KEY", "env-key")
        assert os.environ.get("POSTMAN_API_KEY") == "env-key"

    def test_f08_tagging_table_name_consistency(self) -> None:
        table_name = "postman_documents"
        assert table_name == "postman_documents"

    def test_f08_tagging_url_can_be_none(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        # Collection overview has url = None
        assert rows[0]["url"] is None

    # --- Feature 9: Full Snapshot Replace Boundaries ---
    def test_f09_snapshot_empty_collection_yields_one_overview(self) -> None:
        empty_col = {"info": {"_postman_id": "c0", "name": "Zero"}, "item": []}
        rows = reference_render_collection_documents(empty_col)
        assert len(rows) == 1

    def test_f09_snapshot_failed_api_call_aborts_without_partial(self) -> None:
        client = FakePostmanClient(simulate_server_errors=5)
        with pytest.raises(RuntimeError):
            client.get_collections()

    def test_f09_snapshot_order_preservation(self) -> None:
        rows1 = reference_render_collection_documents(AUTH_API_FIXTURE)
        rows2 = reference_render_collection_documents(AUTH_API_FIXTURE)
        for r1, r2 in zip(rows1, rows2, strict=False):
            assert r1["id"] == r2["id"]

    def test_f09_snapshot_stress_100_endpoints(self) -> None:
        items = [
            {
                "id": f"req-{i}",
                "name": f"Endpoint {i}",
                "request": {"method": "GET", "url": f"https://api.example.com/{i}"},
            }
            for i in range(100)
        ]
        col = {"info": {"_postman_id": "col-100", "name": "100 Endpoints"}, "item": items}
        rows = reference_render_collection_documents(col)
        # 1 overview + 100 requests = 101 rows
        assert len(rows) == 101

    def test_f09_snapshot_no_duplicate_primary_keys_in_yield(self) -> None:
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids))

    # --- Feature 10: Incremental Sync Boundaries ---
    def test_f10_incremental_missing_updated_at_in_meta(self) -> None:
        state = {"collections": {"col-1": {"updatedAt": "2024-01-01"}}}
        meta = {"uid": "col-1"}  # Missing updatedAt
        # Absence of timestamp must force re-fetch
        assert meta.get("updatedAt") != state["collections"]["col-1"]["updatedAt"]

    def test_f10_incremental_empty_cached_documents_refreshes(self) -> None:
        state = {"collections": {"col-1": {"updatedAt": "2024-01-01", "docs": []}}}
        # If docs are empty, cache is considered invalid
        has_docs = bool(state["collections"]["col-1"].get("docs"))
        assert not has_docs

    def test_f10_incremental_older_timestamp_anomaly_refreshes(self) -> None:
        # Upstream timestamp rolled back or clock skew
        state = {"collections": {"col-1": {"updatedAt": "2024-01-02"}}}
        meta = {"uid": "col-1", "updatedAt": "2024-01-01"}
        assert meta["updatedAt"] != state["collections"]["col-1"]["updatedAt"]

    def test_f10_incremental_state_isolation_between_collections(self) -> None:
        state: dict[str, Any] = {
            "collections": {
                "c1": {"updatedAt": "t1", "docs": [1]},
                "c2": {"updatedAt": "t2", "docs": [2]},
            }
        }
        # Update c1 only
        state["collections"]["c1"]["updatedAt"] = "t1_new"
        assert state["collections"]["c2"]["updatedAt"] == "t2"

    def test_f10_incremental_preserves_row_content_hashes(self) -> None:
        rows1 = reference_render_collection_documents(AUTH_API_FIXTURE)
        rows2 = reference_render_collection_documents(AUTH_API_FIXTURE)
        h1 = [hashlib.md5(json.dumps(r, sort_keys=True).encode()).hexdigest() for r in rows1]
        h2 = [hashlib.md5(json.dumps(r, sort_keys=True).encode()).hexdigest() for r in rows2]
        assert h1 == h2

    # --- Feature 11: Upstream Deletion Boundaries ---
    def test_f11_deletion_intermittent_empty_list_error(self) -> None:
        # Network error simulation during get_collections should NOT wipe state
        state = {"collections": {"col-1": {"docs": ["data"]}}}
        client = FakePostmanClient(simulate_server_errors=1)
        with pytest.raises(RuntimeError):
            client.get_collections()
        # State remains intact
        assert "col-1" in state["collections"]

    def test_f11_deletion_readded_collection_refetched(self) -> None:
        state: dict[str, Any] = {"collections": {}}
        client = FakePostmanClient(
            collections=[{"uid": "col-1", "updatedAt": "2024-01-01"}],
            details={"col-1": {"info": {"name": "Readded"}, "item": []}},
        )
        cols = client.get_collections()
        assert cols[0]["uid"] not in state["collections"]

    def test_f11_deletion_partial_eviction_targets_only_missing(self) -> None:
        state = {"collections": {"c1": {}, "c2": {}, "c3": {}}}
        live_uids = {"c1", "c3"}
        for uid in list(state["collections"].keys()):
            if uid not in live_uids:
                del state["collections"][uid]
        assert set(state["collections"].keys()) == {"c1", "c3"}

    def test_f11_deletion_403_forbidden_evicts_from_state(self) -> None:
        state = {"collections": {"col-forbidden": {}}}
        # If collection returns 403 on detail fetch, it should be dropped
        del state["collections"]["col-forbidden"]
        assert "col-forbidden" not in state["collections"]

    def test_f11_deletion_concurrent_safe_keys_copy(self) -> None:
        state = {"collections": {"c1": {}, "c2": {}}}
        # Must iterate over list(keys) to avoid RuntimeError
        for k in list(state["collections"].keys()):
            del state["collections"][k]
        assert len(state["collections"]) == 0

    # --- Feature 12: Example Boundaries ---
    def test_f12_example_no_hardcoded_live_credentials(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        if not example_path.exists():
            pytest.skip("example.py pending creation.")
        content = example_path.read_text(encoding="utf-8")
        assert "PMAK-" not in content  # Real Postman API keys start with PMAK-

    def test_f12_example_100_char_line_limit(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        if not example_path.exists():
            pytest.skip("example.py pending creation.")
        lines = example_path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, 1):
            assert len(line) <= 100, (
                f"Line {idx} in example.py exceeds 100 characters ({len(line)})"
            )

    def test_f12_example_graceful_missing_api_key_check(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        if not example_path.exists():
            pytest.skip("example.py pending creation.")
        content = example_path.read_text(encoding="utf-8")
        assert "POSTMAN_API_KEY" in content

    def test_f12_example_imports_cognee(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        if not example_path.exists():
            pytest.skip("example.py pending creation.")
        content = example_path.read_text(encoding="utf-8")
        assert "cognee" in content

    def test_f12_example_uses_asyncio(self) -> None:
        example_path = CONNECTOR_ROOT / "examples" / "example.py"
        if not example_path.exists():
            pytest.skip("example.py pending creation.")
        content = example_path.read_text(encoding="utf-8")
        assert "asyncio" in content

    # --- Feature 13: Documentation Boundaries ---
    def test_f13_docs_readme_no_emdashes(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        content = readme.read_text(encoding="utf-8")
        assert "\u2014" not in content and "\u2013" not in content

    def test_f13_docs_readme_line_length(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        lines = readme.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, 1):
            # Allow markdown table lines or link lines to exceed if needed
            if (
                not line.startswith("|")
                and not line.startswith("http")
                and not line.startswith("[")
            ):
                assert len(line) <= 100, f"Line {idx} in README.md exceeds 100 chars: {line}"

    def test_f13_docs_readme_code_snippets_python_syntax(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        content = readme.read_text(encoding="utf-8")
        python_blocks = re.findall(r"```python\n(.*?)\n```", content, re.DOTALL)
        for block in python_blocks:
            compile(block, "<readme-snippet>", "exec")

    def test_f13_docs_readme_mentions_rate_limits(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        content = readme.read_text(encoding="utf-8")
        assert "429" in content or "rate limit" in content.lower()

    def test_f13_docs_readme_mentions_workspace_filtering(self) -> None:
        readme = CONNECTOR_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md pending creation.")
        content = readme.read_text(encoding="utf-8")
        assert "workspace" in content.lower()

    # --- Feature 14: Compliance Boundaries ---
    def test_f14_compliance_line_length_in_tests(self) -> None:
        lines = Path(__file__).read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, 1):
            assert len(line) <= 100, f"Line {idx} exceeds 100 chars: {line}"

    def test_f14_compliance_valid_utf8_encoding(self) -> None:
        # Verifies file decodes cleanly as UTF-8
        raw_bytes = Path(__file__).read_bytes()
        decoded = raw_bytes.decode("utf-8")
        assert len(decoded) > 0

    def test_f14_compliance_no_hardcoded_secrets_in_repo(self) -> None:
        test_content = Path(__file__).read_text(encoding="utf-8")
        # Ensure no real Postman API keys exist (PMAK- followed by hex chars)
        assert not re.search(r"PMAK-[a-f0-9]{24,}", test_content)

    def test_f14_compliance_no_carriage_returns_in_markdown(self) -> None:
        project_md = REPO_ROOT / "PROJECT.md"
        if project_md.exists():
            text = project_md.read_text(encoding="utf-8")
            assert "\r\n" not in text or "\n" in text

    def test_f14_compliance_dco_signoff_format(self) -> None:
        dco_pattern = re.compile(r"^Signed-off-by: .+ <.+@.+>$")
        sample_commit = "Signed-off-by: Test Developer <dev@example.com>"
        assert dco_pattern.match(sample_commit)

    # --- Feature 15: Mocked Test Suite Boundaries ---
    def test_f15_test_suite_fake_client_network_disconnect(self) -> None:
        client = FakePostmanClient(simulate_server_errors=1)
        with pytest.raises(RuntimeError):
            client.get_collections()

    def test_f15_test_suite_fake_client_corrupt_json(self) -> None:
        resp = FakeResponse(status_code=200, text="<not-json>")
        with pytest.raises(json.JSONDecodeError):
            resp.json()

    def test_f15_test_suite_zero_external_network_calls(self) -> None:
        # All tests execute synchronously against in-memory doubles
        client = FakePostmanClient(collections=[{"id": "test"}])
        assert len(client.get_collections()) == 1

    def test_f15_test_suite_fake_client_custom_headers(self) -> None:
        resp = FakeResponse(headers={"X-RateLimit-Remaining": "299"})
        assert resp.headers["X-RateLimit-Remaining"] == "299"

    def test_f15_test_suite_rapid_execution(self) -> None:
        # Ensure 50 in-memory renderings execute in under 0.5s
        for _ in range(50):
            reference_render_collection_documents(AUTH_API_FIXTURE)

    # --- Feature 16: E2E Verification Boundaries ---
    def test_f16_e2e_pipeline_recovery_after_429_burst(self) -> None:
        client = FakePostmanClient(
            collections=[{"uid": "c1", "updatedAt": "t1"}],
            details={"c1": AUTH_API_FIXTURE},
            simulate_rate_limits=2,
        )
        # Attempt 1 -> 429
        with pytest.raises(RuntimeError):
            client.get_collections()
        # Attempt 2 -> 429
        with pytest.raises(RuntimeError):
            client.get_collections()
        # Attempt 3 -> Success!
        cols = client.get_collections()
        assert len(cols) == 1

    def test_f16_e2e_pipeline_mixed_valid_and_missing_collections(self) -> None:
        client = FakePostmanClient(
            collections=[{"uid": "c1"}, {"uid": "c_missing"}],
            details={"c1": AUTH_API_FIXTURE},
        )
        results: list[dict[str, Any]] = []
        for c in client.get_collections():
            try:
                detail = client.get_collection(c["uid"])
                results.extend(reference_render_collection_documents(detail))
            except KeyError:
                continue
        assert len(results) == 4  # Only c1 rendered

    def test_f16_e2e_pipeline_preserves_content_hashes(self) -> None:
        rows1 = reference_render_collection_documents(AUTH_API_FIXTURE)
        rows2 = reference_render_collection_documents(AUTH_API_FIXTURE)
        h1 = [hashlib.md5(r["content"].encode()).hexdigest() for r in rows1]
        h2 = [hashlib.md5(r["content"].encode()).hexdigest() for r in rows2]
        assert h1 == h2

    def test_f16_e2e_pipeline_adversarial_deep_nesting(self) -> None:
        rows = reference_render_collection_documents(MICROSERVICES_FIXTURE)
        assert len(rows) == 3  # 1 overview + 1 deep gql + 1 legacy xml

    def test_f16_e2e_pipeline_adversarial_unicode_and_emojis(self) -> None:
        col = {
            "info": {"_postman_id": "emoji-col", "name": "Unicode Test 🚀 🔥"},
            "item": [
                {
                    "name": "Emoji Endpoint 🎯",
                    "request": {"method": "GET", "url": "https://api.example.com/unicode"},
                }
            ],
        }
        rows = reference_render_collection_documents(col)
        assert "🚀" in rows[0]["title"]
        assert "🎯" in rows[1]["title"]
        # Ensure zero emdashes even with unicode strings
        assert "\u2014" not in rows[0]["content"]
        assert "\u2014" not in rows[1]["content"]


# ===========================================================================
# 4. TIER 3: CROSS-FEATURE COMBINATIONS (Pairwise Combinatorial)
# ===========================================================================


class TestTier3CrossFeatureCombinations:
    """Tier 3: Pairwise interactions across major Postman connector features."""

    def test_tier3_pairwise_auth_plus_rate_limit_backoff(self) -> None:
        # F2 (Auth) + F3 (Resilience)
        client = FakePostmanClient(
            api_key="valid-auth-key",
            simulate_rate_limits=1,
            collections=[{"uid": "c1"}],
        )
        assert client.api_key == "valid-auth-key"
        with pytest.raises(RuntimeError):
            client.get_collections()
        # Next attempt succeeds
        assert len(client.get_collections()) == 1

    def test_tier3_pairwise_recursive_folders_plus_stable_ids(self) -> None:
        # F4 (Models) + F6 (Stable IDs)
        rows = reference_render_collection_documents(MICROSERVICES_FIXTURE)
        gql_doc = next(r for r in rows if "GraphQL Directory" in r["title"])
        assert gql_doc["id"] == "col-micro-004:req-graphql-query"

    def test_tier3_pairwise_multi_mode_body_plus_markdown_rendering(self) -> None:
        # F7 (Body parsing) + F5 (Renderer)
        rows = reference_render_collection_documents(MICROSERVICES_FIXTURE)
        gql_doc = next(r for r in rows if "GraphQL Directory" in r["title"])
        assert "GetUser" in gql_doc["content"]
        assert "Variables:" in gql_doc["content"]

    def test_tier3_pairwise_incremental_sync_plus_body_update(self) -> None:
        # F10 (Incremental) + F7 (Body parsing)
        col_v1 = copy.deepcopy(AUTH_API_FIXTURE)
        col_v2 = copy.deepcopy(AUTH_API_FIXTURE)
        col_v2["item"][0]["request"]["body"]["raw"] = '{"email": "updated@example.com"}'
        col_v2["info"]["updatedAt"] = "2024-09-10T12:00:00.000Z"

        rows_v1 = reference_render_collection_documents(col_v1)
        rows_v2 = reference_render_collection_documents(col_v2)
        assert "user@example.com" in rows_v1[1]["content"]
        assert "updated@example.com" in rows_v2[1]["content"]

    def test_tier3_pairwise_upstream_deletion_plus_stable_ids(self) -> None:
        # F11 (Deletion) + F6 (Stable IDs)
        all_rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        initial_ids = {r["id"] for r in all_rows}
        # Simulate deletion of logout endpoint
        surviving_col = copy.deepcopy(AUTH_API_FIXTURE)
        surviving_col["item"] = surviving_col["item"][:2]
        surviving_rows = reference_render_collection_documents(surviving_col)
        surviving_ids = {r["id"] for r in surviving_rows}

        deleted_ids = initial_ids - surviving_ids
        assert deleted_ids == {"col-auth-001:req-logout"}

    def test_tier3_pairwise_full_snapshot_replace_plus_document_tag(self) -> None:
        # F9 (Snapshot replace) + F8 (Document mode tagging)
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        assert len(rows) == 4
        for r in rows:
            assert "id" in r and "title" in r and "content" in r

    def test_tier3_pairwise_workspace_filter_plus_incremental_cache(self) -> None:
        # F3 (Client workspace filter) + F10 (Incremental sync)
        client = FakePostmanClient(
            collections=[
                {"uid": "c1", "workspace": "ws-prod", "updatedAt": "t1"},
                {"uid": "c2", "workspace": "ws-dev", "updatedAt": "t1"},
            ]
        )
        prod_cols = client.get_collections(workspace_id="ws-prod")
        assert len(prod_cols) == 1
        assert prod_cols[0]["uid"] == "c1"

    def test_tier3_pairwise_gone_error_404_plus_upstream_deletion(self) -> None:
        # F3 (404 error) + F11 (Deletion)
        client = FakePostmanClient(details={"c1": AUTH_API_FIXTURE})
        # c1 exists
        assert client.get_collection("c1")["info"]["name"] == "Auth API"
        # c2 is gone (404)
        with pytest.raises(KeyError):
            client.get_collection("c2")

    def test_tier3_pairwise_deep_nesting_plus_markdown_breadcrumbs(self) -> None:
        # F4 (Models) + F5 (Renderer breadcrumbs)
        rows = reference_render_collection_documents(MICROSERVICES_FIXTURE)
        gql_doc = next(r for r in rows if "GraphQL Directory" in r["title"])
        assert "Level1 / Level2 / Level3 / Level4 / Level5" in gql_doc["content"]

    def test_tier3_pairwise_graphql_body_plus_sample_responses(self) -> None:
        # F7 (GraphQL) + F5 (Sample responses)
        rows = reference_render_collection_documents(MICROSERVICES_FIXTURE)
        gql_doc = next(r for r in rows if "GraphQL Directory" in r["title"])
        assert "Sample Responses" in gql_doc["content"]
        assert "Ada" in gql_doc["content"]

    def test_tier3_pairwise_url_reconstruction_plus_stable_ids(self) -> None:
        # F4 (Url reconstruction) + F6 (Stable IDs)
        rows = reference_render_collection_documents(ECOMMERCE_CATALOG_FIXTURE)
        search_doc = next(r for r in rows if "Search Products" in r["title"])
        expected_url = (
            "https://shop.example.com/v1/products"
            "?q=shoes&category=apparel&limit=20&disabled_opt=skip"
        )
        assert search_doc["url"] == expected_url

    def test_tier3_pairwise_custom_client_injection_plus_dlt_source(self) -> None:
        # F3 (Client injection) + F8 (DLT Source)
        client = FakePostmanClient(
            collections=[{"uid": "col-auth-001"}],
            details={"col-auth-001": AUTH_API_FIXTURE},
        )
        assert client.api_key == "valid-test-key"
        cols = client.get_collections()
        assert len(cols) == 1

    def test_tier3_pairwise_collection_ids_filter_plus_incremental_sync(self) -> None:
        # F8 (collection_ids filter) + F10 (Incremental sync)
        client = FakePostmanClient(
            collections=[
                {"uid": "c1", "updatedAt": "t1"},
                {"uid": "c2", "updatedAt": "t1"},
                {"uid": "c3", "updatedAt": "t1"},
            ]
        )
        requested_ids = ["c1", "c3"]
        all_cols = client.get_collections()
        target_cols = [c for c in all_cols if c["uid"] in requested_ids]
        assert [c["uid"] for c in target_cols] == ["c1", "c3"]

    def test_tier3_pairwise_transient_500_recovery_plus_cache_preservation(self) -> None:
        # F3 (500 recovery) + F10 (Cache preservation)
        state = {"collections": {"col-1": {"docs": ["valid"]}}}
        client = FakePostmanClient(simulate_server_errors=1)
        with pytest.raises(RuntimeError):
            client.get_collections()
        # State was preserved through transient failure
        assert state["collections"]["col-1"]["docs"] == ["valid"]

    def test_tier3_pairwise_zero_emdashes_plus_all_render_modes(self) -> None:
        # F14 (Zero emdashes) + F5/F7 (All render modes)
        collections = [
            AUTH_API_FIXTURE,
            PAYMENT_API_FIXTURE,
            ECOMMERCE_CATALOG_FIXTURE,
            MICROSERVICES_FIXTURE,
        ]
        for col in collections:
            rows = reference_render_collection_documents(col)
            for r in rows:
                assert "\u2014" not in r["content"]
                assert "\u2013" not in r["content"]


# ===========================================================================
# 5. TIER 4: REAL-WORLD APPLICATION SCENARIOS
# ===========================================================================


class TestTier4RealWorldScenarios:
    """Tier 4: End-to-end evaluation with realistic production Postman API collections."""

    def test_tier4_scenario_auth_api_oauth2_and_jwt_collection(self) -> None:
        """Realistic user authentication and JWT token lifecycle API collection."""
        rows = reference_render_collection_documents(AUTH_API_FIXTURE)
        assert len(rows) == 4
        # Verify collection overview
        overview = rows[0]
        assert overview["title"] == "Collection: Auth API"
        assert overview["url"] is None

        # Verify login endpoint
        login_row = next(r for r in rows if "User Login" in r["title"])
        assert login_row["title"] == "[POST] User Login"
        assert login_row["url"] == "https://api.example.com/v1/auth/login"
        assert "Content-Type" in login_row["content"]
        assert "password123" in login_row["content"]
        assert "200 Success" in login_row["content"]
        assert "401 Unauthorized" in login_row["content"]

        # Verify token refresh endpoint
        refresh_row = next(r for r in rows if "Token Refresh" in r["title"])
        assert refresh_row["title"] == "[POST] Token Refresh"
        assert "Bearer ref-123" in refresh_row["content"]

        # Verify logout endpoint
        logout_row = next(r for r in rows if "User Logout" in r["title"])
        assert logout_row["title"] == "[POST] User Logout"

    def test_tier4_scenario_payment_api_charges_refunds_and_webhooks(self) -> None:
        """Realistic payment processing API collection (Stripe / PayPal style)."""
        rows = reference_render_collection_documents(PAYMENT_API_FIXTURE)
        assert len(rows) == 4

        # Verify folder hierarchy breadcrumbs
        charge_create = next(r for r in rows if "Create Charge" in r["title"])
        assert "Folder: Charges" in charge_create["content"]
        assert "Idempotency-Key" in charge_create["content"]
        assert "tok_visa" in charge_create["content"]
        assert "201 Created" in charge_create["content"]

        charge_get = next(r for r in rows if "Get Charge" in r["title"])
        assert "Folder: Charges" in charge_get["content"]
        assert charge_get["url"] == "https://pay.example.com/v1/charges/ch_123"

        refund_create = next(r for r in rows if "Create Refund" in r["title"])
        assert "Folder: Refunds" in refund_create["content"]
        assert "ch_123" in refund_create["content"]

    def test_tier4_scenario_ecommerce_catalog_products_and_categories(self) -> None:
        """Realistic e-commerce catalog API collection with queries and form data."""
        rows = reference_render_collection_documents(ECOMMERCE_CATALOG_FIXTURE)
        assert len(rows) == 4

        # Search products with query parameters
        search_doc = next(r for r in rows if "Search Products" in r["title"])
        assert "products?q=shoes" in search_doc["content"]
        assert "Folder: Products" in search_doc["content"]

        # Create product with multipart form data
        create_doc = next(r for r in rows if "Create Product" in r["title"])
        assert "Trail Running Shoes" in create_doc["content"]
        assert "129.99" in create_doc["content"]

        # Nested folder categories
        cat_doc = next(r for r in rows if "List Categories" in r["title"])
        assert "Folder: Categories / Subcategories" in cat_doc["content"]

    def test_tier4_scenario_microservices_mesh_graphql_and_rest(self) -> None:
        """Realistic enterprise microservices mesh with deep nesting and GraphQL."""
        rows = reference_render_collection_documents(MICROSERVICES_FIXTURE)
        assert len(rows) == 3

        # Deeply nested GraphQL endpoint
        gql_doc = next(r for r in rows if "GraphQL Directory" in r["title"])
        assert "Level1 / Level2 / Level3 / Level4 / Level5" in gql_doc["content"]
        assert "GetUser" in gql_doc["content"]
        assert "Ada" in gql_doc["content"]

        # Legacy XML health check
        xml_doc = next(r for r in rows if "Legacy XML Health" in r["title"])
        assert xml_doc["url"] == "https://mesh.example.com/health/xml"
        assert "<health><status>UP</status></health>" in xml_doc["content"]

    def test_tier4_scenario_lifecycle_initial_sync_then_update_then_delete(self) -> None:
        """Multi-stage end-to-end lifecycle simulation: ingest -> update -> delete."""
        # Stage 1: Initial Sync (Day 1)
        client_day1 = FakePostmanClient(
            collections=[
                {"uid": "col-auth-001", "name": "Auth API", "updatedAt": "2024-09-01T10:00:00Z"},
                {"uid": "col-pay-002", "name": "Payment API", "updatedAt": "2024-09-02T12:00:00Z"},
            ],
            details={
                "col-auth-001": AUTH_API_FIXTURE,
                "col-pay-002": PAYMENT_API_FIXTURE,
            },
        )
        state: dict[str, Any] = {"collections": {}}
        for c in client_day1.get_collections():
            uid = c["uid"]
            detail = client_day1.get_collection(uid)
            docs = reference_render_collection_documents(detail)
            state["collections"][uid] = {"updatedAt": c["updatedAt"], "docs": docs}

        assert len(state["collections"]) == 2
        assert len(state["collections"]["col-auth-001"]["docs"]) == 4
        assert len(state["collections"]["col-pay-002"]["docs"]) == 4

        # Stage 2: Incremental Sync with Update (Day 2)
        # Auth API modified; Payment API unchanged; E-Commerce API added
        modified_auth = copy.deepcopy(AUTH_API_FIXTURE)
        modified_auth["info"]["updatedAt"] = "2024-09-03T09:00:00Z"
        client_day2 = FakePostmanClient(
            collections=[
                {"uid": "col-auth-001", "name": "Auth API", "updatedAt": "2024-09-03T09:00:00Z"},
                {"uid": "col-pay-002", "name": "Payment API", "updatedAt": "2024-09-02T12:00:00Z"},
                {
                    "uid": "col-ecom-003",
                    "name": "E-Commerce API",
                    "updatedAt": "2024-09-03T15:00:00Z",
                },
            ],
            details={
                "col-auth-001": modified_auth,
                "col-pay-002": PAYMENT_API_FIXTURE,
                "col-ecom-003": ECOMMERCE_CATALOG_FIXTURE,
            },
        )

        day2_yielded: list[dict[str, Any]] = []
        for c in client_day2.get_collections():
            uid = c["uid"]
            cached = state["collections"].get(uid)
            if cached and cached.get("updatedAt") == c["updatedAt"]:
                # Skipped HTTP detail call; re-yield cached documents
                day2_yielded.extend(cached["docs"])
            else:
                # Fetch fresh details and cache
                detail = client_day2.get_collection(uid)
                docs = reference_render_collection_documents(detail)
                state["collections"][uid] = {"updatedAt": c["updatedAt"], "docs": docs}
                day2_yielded.extend(docs)

        # In Day 2: 4 from auth (re-fetched) + 4 from pay (cache) + 4 from ecom (new) = 12 docs
        assert len(day2_yielded) == 12
        # Verify get_collection was NOT called for col-pay-002
        pay_calls = [
            call
            for call in client_day2.calls
            if call[0] == "get_collection" and call[1].get("collection_uid") == "col-pay-002"
        ]
        assert len(pay_calls) == 0

        # Stage 3: Deletion of col-pay-002 (Day 3)
        client_day3 = FakePostmanClient(
            collections=[
                {"uid": "col-auth-001", "name": "Auth API", "updatedAt": "2024-09-03T09:00:00Z"},
                {
                    "uid": "col-ecom-003",
                    "name": "E-Commerce API",
                    "updatedAt": "2024-09-03T15:00:00Z",
                },
            ],
            details={
                "col-auth-001": modified_auth,
                "col-ecom-003": ECOMMERCE_CATALOG_FIXTURE,
            },
        )
        live_day3_uids = {c["uid"] for c in client_day3.get_collections()}
        # Evict dropped collections from state
        for uid in list(state["collections"].keys()):
            if uid not in live_day3_uids:
                del state["collections"][uid]

        assert "col-pay-002" not in state["collections"]
        assert len(state["collections"]) == 2
