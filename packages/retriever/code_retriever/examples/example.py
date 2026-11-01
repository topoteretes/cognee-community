"""Build a code graph and query it with the community CODE retriever.

Set REPOSITORY_PATH to a small repository whose source may be sent to your
configured Cognee providers. This example intentionally does not clear data.
"""

import asyncio
import os
from pathlib import Path

import cognee
from cognee import SearchType
from cognee_community_pipeline_codify.code_graph_pipeline import run_code_graph_pipeline
from cognee_community_retriever_code import register  # noqa: F401
from cognee_community_retriever_code.code_retriever import CodeSearchType


async def main() -> None:
    raw_path = os.environ.get("REPOSITORY_PATH")
    if not raw_path:
        raise SystemExit("Set REPOSITORY_PATH to a small repository to index.")

    repo_path = Path(raw_path).expanduser().resolve()
    if not repo_path.is_dir():
        raise SystemExit(f"REPOSITORY_PATH is not a directory: {repo_path}")

    async for _status in run_code_graph_pipeline(repo_path=str(repo_path), include_docs=False):
        pass

    results = await cognee.search(
        query_type=SearchType[CodeSearchType.name],
        query_text="Find the function that validates repository paths",
    )
    for result in results:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
