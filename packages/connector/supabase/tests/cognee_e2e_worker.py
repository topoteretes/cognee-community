"""Internal subprocess runner: real PostgreSQL, SQLite, Ladybug, LanceDB and CHUNKS search.

Only graph extraction and embedding inference are deterministic model doubles.
No connector, ingestion, reconciliation, persistence or search function is mocked.
"""

import asyncio
import hashlib
import importlib
import json
import os

from sqlalchemy import create_engine, event, text


class TestEmbeddings:
    max_completion_tokens = 8192

    @property
    def tokenizer(self):
        return self

    def count_tokens(self, value):
        return len(value)

    def extract_tokens(self, value):
        return list(value)

    def decode_single_token(self, token):
        return token

    async def embed_text(self, texts):
        return [[(b + 1) / 256 for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts]

    def get_vector_size(self):
        return 8

    def get_batch_size(self):
        return 32

    async def input_limit(self):
        return 8192


async def main():
    import cognee
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.vector import get_vector_engine_async
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.data.methods.get_datasets import get_datasets
    from cognee.modules.users.methods import get_default_user
    from cognee.shared.data_models import KnowledgeGraph, Node
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SYNC_VERSION
    from test_postgres import pg_source

    assert DOCUMENT_SYNC_VERSION >= 1
    embeddings = importlib.import_module(
        "cognee.infrastructure.databases.vector.embeddings.get_embedding_engine"
    )
    embeddings.create_embedding_engine = lambda *args, **kwargs: TestEmbeddings()

    async def extract(content, *args, **kwargs):
        row = json.loads(content.split("\n\n", 1)[-1])
        name = row["row"]["name"]
        return KnowledgeGraph(
            nodes=[Node(id=name, name=name, type="TestRecord", description=name)], edges=[]
        )

    graph_task = importlib.import_module("cognee.tasks.graph.extract_graph_from_data")
    graph_task.extract_content_graph = extract
    writer = create_engine(os.environ["PG_TEST_WRITER"])
    reader = create_engine(os.environ["PG_TEST_READER"])
    with writer.begin() as db:
        for project in ("a", "b"):
            for key in (1, 2):
                db.execute(
                    text(
                        f"INSERT INTO project_{project}.customers VALUES "
                        "(:id, :name, 'private-do-not-export', '2026-01-01')"
                    ),
                    {"id": key, "name": f"{project.upper()}_original_{key}"},
                )

    async def sync(project, dataset="supabase_test"):
        result = await cognee.remember(
            pg_source(reader, project),
            dataset_name=dataset,
            primary_key="id",
            write_disposition="merge",
            max_rows_per_table=0,
            self_improvement=False,
            summary_method="from_extraction",
            chunk_size=2048,
        )
        assert result.status == "completed", result

    async def snapshot(dataset_name, expected, absent=()):
        user = await get_default_user()
        dataset = next(d for d in await get_datasets(user.id) if d.name == dataset_name)
        data = await get_dataset_data(dataset.id)
        assert len(data) == len(expected), (dataset_name, len(data), expected)
        async with set_database_global_context_variables(dataset.id, user.id):
            graph = await get_graph_engine()
            nodes, edges = await graph.get_graph_data()
            graph_text = json.dumps([nodes, edges], default=str)
            vector = await get_vector_engine_async()
            # Inspect every vector collection, so stale entities cannot hide outside chunks.
            vector_payload = []
            connection = await vector.get_connection()
            for collection_name in await connection.table_names():
                collection = await vector.get_collection(collection_name)
                vector_payload.extend(await collection.query().to_list())
            vector_text = json.dumps(vector_payload, default=str)
        search = await cognee.search(
            query_type=cognee.SearchType.CHUNKS,
            query_text="original updated",
            datasets=[dataset_name],
            top_k=100,
        )
        search_text = str(search)
        for name in expected:
            assert name in graph_text, ("graph missing", name)
            assert name in vector_text, ("vector missing", name)
            assert name in search_text, ("search missing", name, search_text)
        for name in absent:
            assert name not in graph_text, ("graph stale", name)
            assert name not in vector_text, ("vector stale", name)
            assert name not in search_text, ("search stale", name)
        for rendered in (graph_text, vector_text, search_text):
            assert "private-do-not-export" not in rendered
        return {str(item.id) for item in data}

    try:
        await sync("a")
        await sync("b")
        expected = ["A_original_1", "A_original_2", "B_original_1", "B_original_2"]
        first = await snapshot("supabase_test", expected)
        await sync("b", "other")
        other = await snapshot("other", ["B_original_1", "B_original_2"])
        await sync("b")
        assert await snapshot("supabase_test", expected) == first

        # A failed key scan must not be interpreted as a successful empty source.
        def fail_scan(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT project_b.customers.id "):
                raise RuntimeError("injected PostgreSQL scan failure")

        event.listen(reader, "before_cursor_execute", fail_scan)
        try:
            try:
                await sync("b")
            except Exception as error:
                assert "injected PostgreSQL scan failure" in str(error)
            else:
                raise AssertionError("Expected scan failure")
        finally:
            event.remove(reader, "before_cursor_execute", fail_scan)
        assert await snapshot("supabase_test", expected) == first
        await sync("b")
        assert await snapshot("supabase_test", expected) == first
        with writer.begin() as db:
            db.execute(
                text(
                    "UPDATE project_b.customers SET name='B_updated_1', "
                    "updated_at='2026-01-02' WHERE id=1"
                )
            )
        await sync("b")
        await snapshot(
            "supabase_test",
            ["A_original_1", "A_original_2", "B_updated_1", "B_original_2"],
            ["B_original_1"],
        )
        with writer.begin() as db:
            db.execute(text("DELETE FROM project_b.customers WHERE id=2"))
        await sync("b")
        await snapshot(
            "supabase_test", ["A_original_1", "A_original_2", "B_updated_1"], ["B_original_2"]
        )
        with writer.begin() as db:
            db.execute(text("DELETE FROM project_b.customers"))
        await sync("b")
        await sync("b")  # successful empty source, no anchor and no fake document
        await snapshot(
            "supabase_test",
            ["A_original_1", "A_original_2"],
            ["B_original_1", "B_original_2", "B_updated_1"],
        )
        assert await snapshot("other", ["B_original_1", "B_original_2"]) == other
        print("COGNEE_STORAGE_E2E_OK: Data, graph, vectors, CHUNKS search; model doubles")
    finally:
        reader.dispose()
        writer.dispose()


if __name__ == "__main__":
    asyncio.run(main())
