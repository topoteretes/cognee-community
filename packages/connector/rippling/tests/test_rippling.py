"""Unit and integration tests for the Rippling data-source connector."""

import httpx
import pytest
from cognee_community_connector_rippling.rippling import (
    RipplingAPIError,
    RipplingAuthError,
    RipplingClient,
    RipplingRateLimitedError,
    _iter_rows,
    _render_department_markdown,
    _render_employee_markdown,
    _render_policy_markdown,
    rippling_source,
)


def test_client_empty_token_raises():
    with pytest.raises(RipplingAuthError):
        RipplingClient(api_token="   ")


def test_client_sends_bearer_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer rippling_test_token"
        return httpx.Response(200, json=[{"id": "emp_1", "name": "Alice"}])

    transport = httpx.MockTransport(handler)
    client = RipplingClient(api_token="rippling_test_token")
    client._client = httpx.Client(transport=transport, base_url="https://api.rippling.com/platform/api")
    client._owns_client = True

    data = client.get("/employees")
    assert len(data) == 1
    assert data[0]["id"] == "emp_1"
    client.close()


def test_client_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    client = RipplingClient(api_token="invalid_token")
    client._client = httpx.Client(transport=transport, base_url="https://api.rippling.com/platform/api")
    client._owns_client = True

    with pytest.raises(RipplingAuthError):
        client.get("/employees")
    client.close()


def test_client_429_long_retry_after_raises_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"error": "Rate limit exceeded"})

    transport = httpx.MockTransport(handler)
    client = RipplingClient(api_token="test_token")
    client._client = httpx.Client(transport=transport, base_url="https://api.rippling.com/platform/api")
    client._owns_client = True

    with pytest.raises(RipplingRateLimitedError):
        client.get("/employees")
    client.close()


def test_client_check_active_cancelled():
    def _cancel():
        raise RipplingAPIError("Run cancelled by caller")

    client = RipplingClient(api_token="test_token", check_active=_cancel)
    with pytest.raises(RipplingAPIError, match="Run cancelled"):
        client.get("/employees")


def test_client_budget_exceeded():
    client = RipplingClient(api_token="test_token", max_requests=1)
    client.calls.append("/employees")
    with pytest.raises(RipplingAPIError, match="budget exceeded"):
        client.get("/employees")


def test_render_employee_markdown():
    emp = {
        "id": "emp_100",
        "name": "Jane Doe",
        "title": "Principal Architect",
        "department": "Platform Engineering",
        "manager": "CTO",
        "work_email": "jane@example.com",
        "employment_status": "Active",
        "start_date": "2023-03-01",
        "description": "Leads platform reliability and cloud distributed systems.",
        "updated_at": "2026-10-25T12:00:00Z",
    }
    doc_id, title, content = _render_employee_markdown(emp)
    assert doc_id == "rippling:employee:emp_100"
    assert "Jane Doe" in title
    assert "Principal Architect" in content
    assert "Platform Engineering" in content
    assert "jane@example.com" in content
    assert "Leads platform reliability" in content


def test_render_department_markdown():
    dept = {
        "id": "dept_eng_200",
        "name": "Platform Engineering",
        "parent_department": "Engineering Operations",
        "head": "VP of Architecture",
        "description": "Responsible for core APIs and cloud infrastructure.",
        "updated_at": "2026-10-25T12:00:00Z",
    }
    doc_id, title, content = _render_department_markdown(dept)
    assert doc_id == "rippling:department:dept_eng_200"
    assert "Platform Engineering" in title
    assert "Engineering Operations" in content
    assert "VP of Architecture" in content


def test_render_policy_markdown():
    policy = {
        "id": "pol_wfh_300",
        "title": "Global Remote Work Policy",
        "category": "Workplace Guidelines",
        "effective_date": "2026-01-01",
        "description": "Guidelines on home office stipends, equipment, and core collaboration hours.",
        "updated_at": "2026-10-25T12:00:00Z",
    }
    doc_id, title, content = _render_policy_markdown(policy)
    assert doc_id == "rippling:policy:pol_wfh_300"
    assert "Global Remote Work Policy" in title
    assert "Workplace Guidelines" in content
    assert "2026-01-01" in content


def test_source_yields_all_selected_items(rippling):
    rippling.add_employee("emp_1", "Employee One")
    rippling.add_department("dept_1", "Department One")
    rippling.add_policy("pol_1", "Policy One")

    state = {}
    stats = {}
    items = list(_iter_rows(rippling, ("employees", "departments", "policies"), state, stats))
    assert len(items) == 3
    assert stats["emitted"] == 3
    assert stats["unchanged"] == 0
    assert stats["deleted"] == 0


def test_incremental_sync_skips_unchanged_content(rippling):
    rippling.add_employee("emp_1", "Employee One", "Bio text")
    rippling.add_department("dept_1", "Department One")

    state = {}
    stats = {}
    list(_iter_rows(rippling, ("employees", "departments"), state, stats))
    assert stats["emitted"] == 2

    # Second sync: same content, zero rows emitted
    stats.clear()
    items_second = list(_iter_rows(rippling, ("employees", "departments"), state, stats))
    assert len(items_second) == 0
    assert stats["emitted"] == 0
    assert stats["unchanged"] == 2


def test_updated_item_reemits_new_version(rippling):
    rippling.add_employee("emp_1", "Employee Initial", "Old bio")
    state = {}
    stats = {}
    list(_iter_rows(rippling, ("employees",), state, stats))

    # Update description
    rippling.employees["emp_1"]["description"] = "Brand new updated role bio"
    stats.clear()
    updated = list(_iter_rows(rippling, ("employees",), state, stats))
    assert len(updated) == 1
    assert stats["emitted"] == 1
    assert "Brand new updated role bio" in updated[0]["content"]


def test_deleted_item_emits_hard_delete_tombstone(rippling):
    rippling.add_employee("emp_1", "Employee 1")
    rippling.add_employee("emp_2", "Employee 2")
    state = {}
    stats = {}
    list(_iter_rows(rippling, ("employees",), state, stats))

    # Delete emp_2
    del rippling.employees["emp_2"]
    stats.clear()
    deleted = list(_iter_rows(rippling, ("employees",), state, stats))
    assert len(deleted) == 1
    assert deleted[0]["id"] == "rippling:employee:emp_2"
    assert deleted[0]["_deleted"] is True
    assert stats["deleted"] == 1
