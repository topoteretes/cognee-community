"""Stack Overflow connector demo — turn a tag's Q&A into memory.

Pull Stack Overflow questions (with accepted/top answers) for a set of tags
into cognee, with forget-on-delete. ``stack_overflow_source`` returns a
``dlt`` source you hand straight to ``cognee.remember`` — no routing kwargs
needed.

Each run fetches only questions that changed since the last sync (a cheap
listing sweep enumerates the current set for deletion detection first), so a
re-run is inexpensive once the tag has been backfilled once.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[stack-overflow]"    # or: uv sync --extra stack-overflow

2. (Optional but recommended) Register a Stack Apps application at
   https://stackapps.com/apps/oauth/register and copy its API key — keyless
   access is capped at 300 requests/day.
3. Export the key (if you have one) and your LLM key, then run:

       export STACK_OVERFLOW_API_KEY="..."     # optional
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after a question in the tag gets a new answer to see the re-sync.
"""

import asyncio
import os

import cognee

from cognee_community_connector_stack_overflow import stack_overflow_source

# Keep Stack Overflow in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "stack_overflow"
TAGS = ["python", "asyncio"]


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example.")
        return

    # tags is required — Stack Overflow's daily quota is small without it.
    source = stack_overflow_source(tags=TAGS)

    print(f"Syncing Stack Overflow questions tagged {TAGS} into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize the most common issues discussed in these questions.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run after a question gets a new answer, or is deleted, to see the "
        "re-sync and forget-on-delete."
    )


if __name__ == "__main__":
    asyncio.run(main())
