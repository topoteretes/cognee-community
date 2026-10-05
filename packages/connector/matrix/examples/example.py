"""Matrix connector demo — "ask my team chat".

Sync messages from Matrix rooms into cognee memory, incrementally, with
forget-on-redaction.

One-time setup
--------------
1. Install:  cd packages/connector/matrix && uv sync --all-extras
2. Get an access token for the (bot) account whose joined rooms you want:
   Element → Settings → Help & About → Access token, or
   ``curl -XPOST $MATRIX_HOMESERVER/_matrix/client/v3/login -d '{"type":"m.login.password",...}'``
3. Export:
       export MATRIX_HOMESERVER="https://matrix.org"
       export MATRIX_ACCESS_TOKEN="syt_..."
       # optional: export MATRIX_ROOM_IDS="!abc:matrix.org,!def:matrix.org"
4. Set ``LLM_API_KEY`` in ``.env`` like any other cognee example.

Run:  uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_matrix import matrix_source

DATASET_NAME = "matrix_chat"

# merge + id PK = incremental upserts; max_rows_per_table=0 lets orphan cleanup
# see the whole synced corpus, not cognee's default 50-row window.
MATRIX_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main():
    homeserver = os.environ.get("MATRIX_HOMESERVER")
    token = os.environ.get("MATRIX_ACCESS_TOKEN")
    room_ids = os.environ.get("MATRIX_ROOM_IDS")
    if not (homeserver and token):
        print("Set MATRIX_HOMESERVER and MATRIX_ACCESS_TOKEN (see this file's docstring).")
        return

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    def build_source():
        return matrix_source(
            homeserver=homeserver,
            access_token=token,
            room_ids=room_ids.split(",") if room_ids else None,
        )

    print("\n=== Matrix sync #1 (backfill) ===")
    print(
        await cognee.remember(build_source(), dataset_name=DATASET_NAME, **MATRIX_REMEMBER_KWARGS)
    )

    answer = await cognee.search(
        query_text="What decisions were made in these rooms, and who made them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Answer:", answer)

    # Post, edit or redact a message in Element, then sync again: only the delta
    # is fetched and redacted messages are forgotten.
    input("\nChange something in the room, then press Enter to re-sync...")
    print("\n=== Matrix sync #2 (incremental) ===")
    print(
        await cognee.remember(build_source(), dataset_name=DATASET_NAME, **MATRIX_REMEMBER_KWARGS)
    )


if __name__ == "__main__":
    asyncio.run(main())
