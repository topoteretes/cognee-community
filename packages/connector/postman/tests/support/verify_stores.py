"""Inspect the demo's actual local stores; no provider calls or Cognee test helpers."""

import argparse
import asyncio
import json
from pathlib import Path

from local_runtime import configure_runtime


async def inspect_stores(dataset_name: str) -> tuple[dict, dict]:
    import cognee
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.vector import get_vector_engine_async
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    datasets = await cognee.datasets.list_datasets(user)
    dataset = next((item for item in datasets if item.name == dataset_name), None)
    if dataset is None:
        raise RuntimeError("Dataset not found; absence is not proof of successful cleanup.")
    data = await cognee.datasets.list_data(dataset.id, user)
    data_text = []
    for row in data:
        location = row.raw_data_location
        path = Path(location.removeprefix("file://"))
        if not path.is_file():
            raise RuntimeError("Cannot inspect stored document; verification incomplete.")
        data_text.append(path.read_text())
    async with set_database_global_context_variables(dataset.id, user.id):
        graph = await get_graph_engine()
        nodes, edges = await graph.get_graph_data()
        vector = await get_vector_engine_async()
        connection = await vector.get_connection()
        counts, vector_text = {}, []
        for name in await connection.table_names():
            table = await connection.open_table(name)
            counts[name] = int(await table.count_rows())
            arrow = await table.to_arrow()
            vector_text.append(json.dumps(arrow.to_pylist(), default=str))
    stats = {
        "data_rows": len(data),
        "graph_nodes": len(nodes),
        "graph_edges": len(edges),
        "vector_rows": counts,
    }
    texts = {
        "Data": "\n".join(data_text),
        "graph": json.dumps([nodes, edges], default=str),
        "vectors": "\n".join(vector_text),
    }
    return stats, texts


def assert_stores(
    stats: dict, texts: dict, *, expected_rows=None, expect_empty=False, forbidden=(), expected=()
) -> None:
    if expected_rows is not None and stats["data_rows"] != expected_rows:
        raise RuntimeError("Unexpected Data row count.")
    if expect_empty and (
        stats["data_rows"]
        or stats["graph_nodes"]
        or stats["graph_edges"]
        or any(stats["vector_rows"].values())
    ):
        raise RuntimeError("Expected empty dataset, but stale records remain.")
    for token in forbidden:
        for store, text in texts.items():
            if token.casefold() in text.casefold():
                raise RuntimeError(f"Obsolete text remains in {store}.")
    for token in expected:
        for store in ("Data", "vectors"):
            if token.casefold() not in texts[store].casefold():
                raise RuntimeError(f"Expected text absent from {store}.")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="postman_4711_demo")
    parser.add_argument("--expect-data-rows", type=int)
    parser.add_argument("--expect-empty", action="store_true")
    parser.add_argument("--forbid-text", action="append", default=[])
    parser.add_argument("--expect-text", action="append", default=[])
    args = parser.parse_args()
    configure_runtime()
    stats, texts = await inspect_stores(args.dataset)
    assert_stores(
        stats,
        texts,
        expected_rows=args.expect_data_rows,
        expect_empty=args.expect_empty,
        forbidden=args.forbid_text,
        expected=args.expect_text,
    )
    print(json.dumps(stats, indent=2))
    print("Store verification passed.")


if __name__ == "__main__":
    asyncio.run(main())
