"""Load a repo into Cognee with no LLM key, count the row nodes per table and link the rows.

Uses a throwaway Cognee home in a temp dir, so nothing is left behind. Without arguments it builds
the sample monorepo; pass a repo path (and optionally --ci-logs) to use your own.
"""

import argparse
import asyncio
import tempfile
from collections import Counter
from pathlib import Path

import cognee
from cognee.infrastructure.databases.graph import get_graph_engine

from cognee_community_connector_gitgraph import build_sample_repo, gitgraph_source, link_rows

CI_FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures" / "ci"


async def count_rows() -> Counter[str]:
    """Row nodes per table: cognee links each row to its table node with ``is_row_of``."""
    nodes, edges = await (await get_graph_engine()).get_graph_data()
    tables = {str(node_id): props["name"] for node_id, props in nodes}
    return Counter(tables[str(target)] for _, target, name, _ in edges if name == "is_row_of")


async def main(repo: Path, ci_logs: Path | None) -> None:
    source = gitgraph_source(repo, ci_logs=ci_logs)
    result = await cognee.remember(source, dataset_name="gitgraph", self_improvement=False)
    print(result)
    for table, count in sorted((await count_rows()).items()):
        print(f"{table}: {count}")
    print("links between rows:")
    for relationship, count in sorted((await link_rows(source)).items()):
        print(f"{relationship}: {count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", type=Path, help="git repository (default: the sample)")
    parser.add_argument("--ci-logs", type=Path, help="JUnit XML file or directory")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cognee.config.system_root_directory(str(root / "system"))
        cognee.config.data_root_directory(str(root / "data"))
        if args.repo is None:
            args.repo = build_sample_repo(root / "repo").path
            args.ci_logs = args.ci_logs or CI_FIXTURES
        asyncio.run(main(args.repo, args.ci_logs))
