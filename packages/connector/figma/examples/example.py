"""Figma connector demo — turn your design files and their discussions into memory.

Pull Figma files and comment threads into cognee, with forget-on-delete.
``figma_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Each file (pages, frame names, text layers) and each
comment thread is ingested as a normal document, so it goes through the full
cognify entity-extraction pipeline.

The first run fetches every selected file. Later runs list the selection and
fetch only files whose last-modified stamp moved; unchanged files are served
from a cache in dlt's state and are not re-cognified. Files and comments
deleted in Figma are forgotten on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the text layers and comments of the Figma files you select. It is
strictly opt-in — nothing is fetched until you run this script, and only the
files you select are read. Use a dedicated dataset so you can wipe it with a
single ``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
One-time setup (see README.md for the click-by-click version)
────────────────────────────────────────────────────────────────────────────
1. Get a token. Either an OAuth2 access token from your Figma app (see
   README.md → "Setup: Figma OAuth app"), or, for a quick local try, a
   personal access token from Figma → Settings → Security with the scopes
   ``file_content:read``, ``file_comments:read`` and ``folders:read``.
2. Pick what to sync: a folder id (the number after ``/project/`` or
   ``/folder/`` in the folder's URL), a team id (after ``/team/``), or file
   keys (after ``/design/`` or ``/board/`` in a file URL).
3. Export them with your LLM key, then run:

       export FIGMA_ACCESS_TOKEN="<oauth access token>"   # or FIGMA_PERSONAL_ACCESS_TOKEN
       export FIGMA_FOLDER_IDS="<folder id>,<folder id>"  # or FIGMA_TEAM_IDS / FIGMA_FILE_KEYS
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after editing a file, commenting, or deleting a file or comment to see
the incremental re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_figma import figma_source

# Keep Figma in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "figma"


def _ids(name: str) -> list[str] | None:
    """Comma-separated ids from an environment variable, or None when unset."""
    ids = [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]
    return ids or None


async def main() -> None:
    if not (os.environ.get("FIGMA_ACCESS_TOKEN") or os.environ.get("FIGMA_PERSONAL_ACCESS_TOKEN")):
        print("Set FIGMA_ACCESS_TOKEN or FIGMA_PERSONAL_ACCESS_TOKEN (see README.md).")
        return
    selection = {
        "team_ids": _ids("FIGMA_TEAM_IDS"),
        "folder_ids": _ids("FIGMA_FOLDER_IDS"),
        "file_keys": _ids("FIGMA_FILE_KEYS"),
    }
    if not any(selection.values()):
        print("Set FIGMA_FOLDER_IDS, FIGMA_TEAM_IDS or FIGMA_FILE_KEYS to choose what to sync.")
        return

    print("Syncing Figma files and comment threads into cognee ...")
    # The token comes from the environment. The default write_disposition
    # ("replace") is what this source expects: each run is a full snapshot.
    await cognee.remember(figma_source(**selection), dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What design decisions were discussed in the comments?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit a file, add or resolve a comment, or delete something in Figma, then "
        "re-run: changed files re-sync and deleted ones are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
