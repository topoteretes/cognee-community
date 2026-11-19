"""In-memory mock client for Resend connector testing."""

from __future__ import annotations

import copy
from typing import Any

from cognee_community_connector_resend.resend import (
    ResendAuthenticationError,
    ResendNotFoundError,
)


def make_sample_resend_data() -> dict[str, list[dict[str, Any]]]:
    return {
        "domains": [
            {
                "id": "dom_prod_001",
                "name": "notifications.company.com",
                "status": "verified",
                "region": "us-east-1",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "dom_staging_002",
                "name": "staging.company.com",
                "status": "pending",
                "region": "eu-west-1",
                "created_at": "2026-01-05T00:00:00Z",
            },
        ],
        "emails": [
            {
                "id": "em_sent_101",
                "from": "alerts@notifications.company.com",
                "to": ["developer_oncall@domain.com"],
                "subject": "Critical Incident Alert: Service Down",
                "last_event": "delivered",
                "created_at": "2026-10-21T10:00:00Z",
            }
        ],
        "audiences": [
            {
                "id": "aud_saas_users",
                "name": "Product Beta Testers",
                "created_at": "2026-02-01T00:00:00Z",
            }
        ],
        "api_keys": [
            {
                "id": "key_service_worker",
                "name": "Backend Microservice Production Key",
                "created_at": "2026-01-10T00:00:00Z",
            }
        ],
    }


class FakeResendClient:
    """In-memory mock for ResendClient."""

    def __init__(
        self,
        api_key: str = "re_mock_test_key_123",
        sample_data: dict[str, list[dict[str, Any]]] | None = None,
        should_auth_fail: bool = False,
    ) -> None:
        self.api_key = api_key
        self.data = copy.deepcopy(sample_data or make_sample_resend_data())
        self.should_auth_fail = should_auth_fail
        self.request_log: list[str] = []

    def _check_auth(self) -> None:
        if self.should_auth_fail:
            raise ResendAuthenticationError("Mock authentication failure (401)")

    def list_domains(self) -> dict[str, Any]:
        self._check_auth()
        self.request_log.append("list_domains")
        return {"data": self.data.get("domains", [])}

    def get_domain(self, domain_id: str) -> dict[str, Any]:
        self._check_auth()
        for d in self.data.get("domains", []):
            if d["id"] == domain_id:
                return d
        raise ResendNotFoundError(f"Domain {domain_id} not found")

    def list_emails(self, limit: int = 50) -> dict[str, Any]:
        self._check_auth()
        self.request_log.append("list_emails")
        return {"data": self.data.get("emails", [])}

    def get_email(self, email_id: str) -> dict[str, Any]:
        self._check_auth()
        for e in self.data.get("emails", []):
            if e["id"] == email_id:
                return e
        raise ResendNotFoundError(f"Email {email_id} not found")

    def list_audiences(self) -> dict[str, Any]:
        self._check_auth()
        self.request_log.append("list_audiences")
        return {"data": self.data.get("audiences", [])}

    def get_audience(self, audience_id: str) -> dict[str, Any]:
        self._check_auth()
        for a in self.data.get("audiences", []):
            if a["id"] == audience_id:
                return a
        raise ResendNotFoundError(f"Audience {audience_id} not found")

    def list_api_keys(self) -> dict[str, Any]:
        self._check_auth()
        self.request_log.append("list_api_keys")
        return {"data": self.data.get("api_keys", [])}
