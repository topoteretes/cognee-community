
"""Example: ingest Firestore documents into Cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_firestore import firestore_source

DATASET_NAME = "firestore"


async def main() -> None:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")

    if not project_id:
        raise RuntimeError(
            "Set GOOGLE_CLOUD_PROJECT before running this example."
        )

    source = firestore_source(
        collection=os.getenv("FIRESTORE_COLLECTION", "customers"),
        project_id=project_id,
    )

    print("Syncing Firestore documents into Cognee...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize the Firestore documents.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
