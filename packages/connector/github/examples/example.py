"""GitHub connector demo — turn your repos into memory.

Pull GitHub repos/issues/PRs/commits/releases into cognee, with forget-on-delete.
``github_source`` returns a ``dlt`` source you hand straight to ``cognee.remember`` —
no routing kwargs needed. Documents are ingested as normal documents (so they go through
the full cognify entity-extraction pipeline, unlike the relational dlt connectors).

Each run is a full snapshot: unchanged rows keep a stable id and are not re-cognified,
and items that vanish from GitHub (deleted, made private, or you lose access) drop out of
the snapshot, so cognee's orphan cleanup forgets them from memory on the next sync.
"""

import asyncio
import os

import cognee

from cognee_community_connector_github import github_source

# Keep GitHub in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "github"


async def main() -> None:
    if not os.environ.get("GITHUB_TOKEN"):
        print(
            "Set GITHUB_TOKEN (GitHub Settings -> Developer settings -> Personal access "
            "tokens, repo scope) to run this example."
        )
        return

    # Scope with repos=[...] or orgs=[...]; omit both to ingest every repo the
    # authenticated user can see.
    source = github_source()

    print("Syncing GitHub into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize what these GitHub repos are about.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit an issue/PR/commit/release in GitHub, then re-run: edits re-sync and "
        "removed items are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())