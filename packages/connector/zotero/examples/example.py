"""Ingest a public Zotero group and search it with cognee.

Set LLM_API_KEY; optionally ZOTERO_GROUP_ID. Default: VSG public library,
https://www.zotero.org/groups/479046/vsg_public/items/ (no Zotero key required).
Run with: python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_zotero import ZoteroLibraryUnchanged, zotero_source


async def remember_library(library_id: str) -> None:
    """Unwrap the no-op signal when dlt/cognee wrap extraction exceptions."""
    try:
        await cognee.remember(zotero_source(library_id=library_id), dataset_name="zotero")
    except Exception as exc:
        cause = exc
        seen = set()
        while cause is not None and id(cause) not in seen:
            if isinstance(cause, ZoteroLibraryUnchanged):
                raise cause from None
            seen.add(id(cause))
            cause = cause.__cause__ or cause.__context__
        raise


async def main() -> None:
    library_id = os.environ.get("ZOTERO_GROUP_ID", "479046")
    try:
        await remember_library(library_id)
    except ZoteroLibraryUnchanged as exc:
        print(f"Library unchanged since version {exc.version}; nothing to ingest.")
        return
    answer = await cognee.search(
        query_text="Summarize the research in this library.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["zotero"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
