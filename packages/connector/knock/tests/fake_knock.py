"""In-memory mock client for Knock connector testing."""

from __future__ import annotations

import copy
from typing import Any

from cognee_community_connector_knock.knock import (
    KnockAuthenticationError,
    KnockNotFoundError,
)


def make_sample_knock_data() -> dict[str, list[dict[str, Any]]]:
    return {
        "workflows": [
            {
                "key": "user-welcome",
                "name": "Welcome New User",
                "description": "Triggered when a new user signs up",
                "active": True,
                "steps": [
                    {"type": "channel", "channel_id": "sendgrid-primary"},
                    {"type": "delay", "settings": {"delay_for": "1d"}},
                    {"type": "channel", "channel_id": "slack-community"},
                ],
                "created_at": "2026-01-01T12:00:00Z",
                "updated_at": "2026-01-02T12:00:00Z",
            },
            {
                "key": "password-reset",
                "name": "Password Reset Alert",
                "description": "High-priority security notification",
                "active": True,
                "steps": [
                    {"type": "channel", "channel_id": "sendgrid-primary"},
                ],
                "created_at": "2026-01-03T12:00:00Z",
                "updated_at": "2026-01-03T12:00:00Z",
            },
        ],
        "channels": [
            {
                "id": "sendgrid-primary",
                "name": "Transactional SendGrid",
                "type": "email",
                "provider": "sendgrid",
                "settings": {"api_key": "secret_sendgrid_token_xyz"},
                "created_at": "2026-01-01T10:00:00Z",
                "updated_at": "2026-01-01T10:00:00Z",
            },
            {
                "id": "slack-community",
                "name": "Community Slack Alerts",
                "type": "chat",
                "provider": "slack",
                "settings": {"bot_token": "xoxb-secret-slack-token"},
                "created_at": "2026-01-01T10:00:00Z",
                "updated_at": "2026-01-01T10:00:00Z",
            },
        ],
        "schedules": [
            {
                "id": "sch_weekly_digest",
                "workflow": "weekly-digest",
                "recipient": "dev_lead_user@company.com",
                "repeats": "weekly",
                "next_occurrence_at": "2026-10-30T09:00:00Z",
                "ending_at": "2027-01-01T00:00:00Z",
            }
        ],
        "messages": [
            {
                "id": "msg_001",
                "workflow": "password-reset",
                "status": "delivered",
                "channel_id": "sendgrid-primary",
                "recipient": "security_admin@domain.com",
                "created_at": "2026-10-20T10:00:00Z",
                "updated_at": "2026-10-20T10:01:00Z",
            }
        ],
    }


class FakeKnockClient:
    """In-memory mock for KnockClient."""

    def __init__(
        self,
        api_key: str = "mock_secret_key_123",
        sample_data: dict[str, list[dict[str, Any]]] | None = None,
        should_auth_fail: bool = False,
    ) -> None:
        self.api_key = api_key
        self.data = copy.deepcopy(sample_data or make_sample_knock_data())
        self.should_auth_fail = should_auth_fail
        self.request_log: list[str] = []

    def _check_auth(self) -> None:
        if self.should_auth_fail:
            raise KnockAuthenticationError("Mock authentication failure (401)")

    def list_workflows(self, page_size: int = 50, after: str | None = None) -> dict[str, Any]:
        self._check_auth()
        self.request_log.append("list_workflows")
        items = self.data.get("workflows", [])
        return {"entries": items, "page_info": {"after": None, "page_size": page_size}}

    def get_workflow(self, key: str) -> dict[str, Any]:
        self._check_auth()
        for wf in self.data.get("workflows", []):
            if wf["key"] == key:
                return wf
        raise KnockNotFoundError(f"Workflow {key} not found")

    def list_channels(self, page_size: int = 50, after: str | None = None) -> dict[str, Any]:
        self._check_auth()
        self.request_log.append("list_channels")
        items = self.data.get("channels", [])
        return {"entries": items, "page_info": {"after": None, "page_size": page_size}}

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        self._check_auth()
        for ch in self.data.get("channels", []):
            if ch["id"] == channel_id:
                return ch
        raise KnockNotFoundError(f"Channel {channel_id} not found")

    def list_schedules(self, page_size: int = 50, after: str | None = None) -> dict[str, Any]:
        self._check_auth()
        self.request_log.append("list_schedules")
        items = self.data.get("schedules", [])
        return {"entries": items, "page_info": {"after": None, "page_size": page_size}}

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        self._check_auth()
        for sch in self.data.get("schedules", []):
            if sch["id"] == schedule_id:
                return sch
        raise KnockNotFoundError(f"Schedule {schedule_id} not found")

    def list_messages(self, page_size: int = 50, after: str | None = None) -> dict[str, Any]:
        self._check_auth()
        self.request_log.append("list_messages")
        items = self.data.get("messages", [])
        return {"entries": items, "page_info": {"after": None, "page_size": page_size}}

    def get_message(self, message_id: str) -> dict[str, Any]:
        self._check_auth()
        for msg in self.data.get("messages", []):
            if msg["id"] == message_id:
                return msg
        raise KnockNotFoundError(f"Message {message_id} not found")
