"""Customer.io connector demo — "ask my messaging".

Sync the messages in your Customer.io campaigns and newsletters into cognee memory,
then ask questions about them.

``customerio_source`` returns a dlt source you hand straight to ``cognee.remember``.
Every run is a full snapshot of the workspace's messages: edited messages are updated,
deleted ones are forgotten, and messages whose text didn't change are not re-cognified.

One-time setup
--------------
1. In Customer.io, go to Account settings → API Credentials → App API Keys and create
   a key. It must be an App API key; Track API keys don't work with the App API.
2. Export it, plus your workspace's region if it's in the EU:

       CUSTOMERIO_APP_API_KEY=...
       CUSTOMERIO_REGION=eu        # only for EU workspaces; the default is "us"

3. Configure cognee's LLM + embeddings in ``.env`` like any other cognee run.

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_customerio import customerio_source

DATASET_NAME = "customerio"


async def main() -> None:
    if not os.environ.get("CUSTOMERIO_APP_API_KEY"):
        print("Set CUSTOMERIO_APP_API_KEY (see this file's docstring).")
        return

    print("=== Sync #1 ===")
    # No write_disposition needed: cognee's default ("replace") is what a full
    # snapshot wants.
    result = await cognee.remember(customerio_source(), dataset_name=DATASET_NAME)
    print(result)

    answer = await cognee.search(
        query_text="Which campaigns send emails, and what are their subject lines?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Answer:", answer)

    # Run it again after editing or deleting a message in Customer.io: the edit is
    # picked up and the deleted message is forgotten. Unchanged messages are skipped.
    print("\n=== Sync #2 ===")
    result = await cognee.remember(customerio_source(), dataset_name=DATASET_NAME)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
