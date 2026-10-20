"""Capture LogRocket's live MCP schemas before implementing synchronization.

Set LOGROCKET_API_KEY and replace the IDs below. This command calls MCP
discovery and tools/list only; it does not query session or issue data.
"""

from cognee_community_connector_logrocket.cli import main

if __name__ == "__main__":
    raise SystemExit(
        main(
            [
                "inspect-capabilities",
                "--organization",
                "YOUR_ORGANIZATION_ID",
                "--project",
                "YOUR_PROJECT_ID",
                "--output",
                "logrocket-capabilities.json",
            ]
        )
    )
