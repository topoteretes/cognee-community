"""In-memory stand-in for the Rippling API, shared by test modules."""

import pytest
from cognee_community_connector_rippling.rippling import (
    RipplingAPIError,
    RipplingAuthError,
    RipplingQuotaError,
    RipplingRateLimitedError,
)


class FakeRippling:
    """Implements ``get`` for Rippling endpoints: /employees, /departments, /policies."""

    def __init__(self):
        self.employees: dict[str, dict] = {}
        self.departments: dict[str, dict] = {}
        self.policies: dict[str, dict] = {}
        self.calls: list[str] = []
        self.bad_token = False
        self.rate_limited = False
        self.quota_exhausted = False
        self.server_error = False

    def add_employee(self, e_id: str, name: str, description: str = "", title: str = "Software Engineer", department: str = "Engineering"):
        emp = {
            "id": e_id,
            "name": name,
            "title": title,
            "department": department,
            "manager": "Engineering VP",
            "work_email": f"{e_id}@example.com",
            "employment_status": "Active",
            "start_date": "2024-01-15",
            "description": description,
            "updated_at": "2026-10-25T10:00:00.000Z",
        }
        self.employees[e_id] = emp
        return emp

    def add_department(self, d_id: str, name: str, description: str = "", parent: str = "Product"):
        dept = {
            "id": d_id,
            "name": name,
            "parent_department": parent,
            "head": "Director of Engineering",
            "description": description,
            "updated_at": "2026-10-25T10:00:00.000Z",
        }
        self.departments[d_id] = dept
        return dept

    def add_policy(self, p_id: str, title: str, description: str = "", category: str = "Remote Work"):
        policy = {
            "id": p_id,
            "title": title,
            "category": category,
            "effective_date": "2026-01-01",
            "description": description,
            "updated_at": "2026-10-25T10:00:00.000Z",
        }
        self.policies[p_id] = policy
        return policy

    def get(self, path: str, params=None):
        self.calls.append(path)
        if self.bad_token:
            raise RipplingAuthError("Rippling rejected the API token (HTTP 401)")
        if self.rate_limited:
            raise RipplingRateLimitedError("Rippling rate limit exceeded (HTTP 429)")
        if self.quota_exhausted:
            raise RipplingQuotaError("Rippling quota exhausted")
        if self.server_error:
            raise RipplingAPIError("Rippling server error: HTTP 500", status=500)

        clean = path.strip("/")
        parts = clean.split("/")

        if len(parts) == 1:
            collection = parts[0]
            if collection == "employees":
                return list(self.employees.values())
            if collection == "departments":
                return list(self.departments.values())
            if collection == "policies":
                return list(self.policies.values())
            raise RipplingAPIError(f"Endpoint not found: {path}", status=404)

        if len(parts) == 2:
            collection, item_id = parts
            if collection == "employees" and item_id in self.employees:
                return dict(self.employees[item_id])
            if collection == "departments" and item_id in self.departments:
                return dict(self.departments[item_id])
            if collection == "policies" and item_id in self.policies:
                return dict(self.policies[item_id])
            raise RipplingAPIError(f"Item not found: {path}", status=404)

        raise RipplingAPIError(f"Unknown path: {path}", status=404)


@pytest.fixture
def rippling():
    return FakeRippling()
