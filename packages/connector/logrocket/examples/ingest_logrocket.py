"""Ingest a bounded LogRocket window into a dedicated cognee dataset.

Run from packages/connector/logrocket:

    uv run --env-file .env python examples/ingest_logrocket.py

The MCP search surface has no exhaustive data pagination or deletion feed.
This example explicitly acknowledges bounded results. If a response reaches
the configured cap, extraction fails. Each run requests the full window again.
"""

import asyncio
import os

import cognee

from cognee_community_connector_logrocket import logrocket_source, remember_logrocket

DATASET_NAME = "logrocket"


async def main() -> None:
    organization_id = os.environ.get("LOGROCKET_ORGANIZATION")
    project_id = os.environ.get("LOGROCKET_PROJECT")
    if not organization_id or not project_id:
        print(
            "Set LOGROCKET_ORGANIZATION and LOGROCKET_PROJECT or run uv with --env-file .env. "
            "Provide the API key through LOGROCKET_API_KEY or DLT secrets."
        )
        return

    source = logrocket_source(
        organization_id=organization_id,
        project_id=project_id,
        resources=("sessions", "issues"),
        session_lookback_days=7,
        issue_lookback_days=30,
        limit=100,
        # Required acknowledgement: capped MCP searches cannot prove a complete
        # project snapshot and this source never infers upstream deletion.
        acknowledge_bounded_results=True,
    )
    await remember_logrocket(source, dataset_name=DATASET_NAME)
    result = await cognee.search(
        query_text="What LogRocket errors and affected sessions were observed?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
