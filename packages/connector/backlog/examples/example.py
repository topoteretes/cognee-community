"""Minimal runnable example for cognee-community-connector-backlog."""

import os
from cognee_community_connector_backlog import backlog_source


def demo() -> None:
    space_url = os.environ.get("BACKLOG_SPACE_URL", "https://demo.backlog.com")
    api_key = os.environ.get("BACKLOG_API_KEY", "DEMO_API_KEY")

    def mock_transport(url: str, params: dict) -> list:
        if url.endswith("/api/v2/projects"):
            return [{"id": 1, "projectKey": "ENG"}]
        if url.endswith("/api/v2/issues"):
            return [
                {
                    "id": 10,
                    "issueKey": "ENG-10",
                    "summary": "Switch payment retry to 3 attempts",
                    "description": "Reduce retry storms during upstream maintenance.",
                    "status": {"name": "Resolved"},
                    "priority": {"name": "High"},
                    "assignee": {"name": "Aiko Tanaka"},
                    "updated": "2026-10-30T12:00:00Z",
                }
            ]
        return []

    rows = list(backlog_source(space_url=space_url, api_key=api_key, fetch_json_fn=mock_transport))
    for r in rows:
        print(r["id"], "->", r["summary"])


if __name__ == "__main__":
    demo()
