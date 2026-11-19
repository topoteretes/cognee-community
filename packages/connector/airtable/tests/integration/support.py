"""Offline HTTP/AI fixtures around genuine Cognee SQLite/Ladybug/LanceDB storage.

This module is also usable from a fresh interpreter for the recovery test. It
does not import Cognee's own tests or replace ingestion, retrieval, or cleanup.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

ENVIRONMENT = {
    "TELEMETRY_DISABLED": "1",
    "COGNEE_SKIP_CONNECTION_TEST": "true",
    "COGNEE_SKIP_PREFLIGHT": "1",
    "ENABLE_BACKEND_ACCESS_CONTROL": "true",
    "REQUIRE_AUTHENTICATION": "true",
    "HASH_API_KEY": "false",
    "CACHING": "false",
    "LLM_API_KEY": "offline-fixture",
    "LLM_PROVIDER": "openai",
    "LLM_MODEL": "openai/gpt-4o-mini",
    "EMBEDDING_API_KEY": "offline-fixture",
    "EMBEDDING_PROVIDER": "openai",
    "EMBEDDING_MODEL": "openai/text-embedding-3-small",
    "EMBEDDING_DIMENSIONS": "256",
    "GRAPH_DATASET_DATABASE_HANDLER": "ladybug",
    "VECTOR_DATASET_DATABASE_HANDLER": "lancedb",
    "RUNTIME__LOG_LEVEL": "ERROR",
    "RUNTIME__DLTHUB_TELEMETRY": "false",
}


def pin_environment() -> None:
    for key, value in ENVIRONMENT.items():
        os.environ[key] = value


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.headers = {}

    def json(self):
        return copy.deepcopy(self.payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError

            raise HTTPError(f"Fixture HTTP {self.status_code}", response=self)


class Airtable:
    """Mutable REST fixture with full inventories and independent comment reads."""

    def __init__(self, base="appFixture"):
        self.base = base
        self.tables = {}
        self.records = {}
        self.comments = {}
        self.calls = []
        self.fail_comments = None
        self.add_table("tblPeople", "People")

    def add_table(self, table_id, name):
        self.tables[table_id] = {
            "id": table_id,
            "name": name,
            "primaryFieldId": "fldBody",
            "fields": [
                {"id": "fldBody", "name": "Biography", "type": "multilineText"},
                {
                    "id": "fldModified",
                    "name": "Last modified time",
                    "type": "lastModifiedTime",
                    "options": {
                        "isValid": True,
                        "result": {
                            "type": "dateTime",
                            "options": {
                                "dateFormat": {"name": "iso"},
                                "timeFormat": {"name": "24hour"},
                                "timeZone": "utc",
                            },
                        },
                    },
                },
            ],
        }
        self.records[table_id] = {}

    def put(self, record_id, text, table="tblPeople", modified="2026-01-01T00:00:00Z"):
        self.records[table][record_id] = {
            "id": record_id,
            "createdTime": "2025-01-01T00:00:00Z",
            "fields": {"fldBody": text, "fldModified": modified},
        }

    def comment(self, record_id, text, table="tblPeople"):
        self.comments[(table, record_id)] = [
            {
                "id": "comFixture",
                "text": text,
                "createdTime": "2026-01-01T00:00:00Z",
                "lastUpdatedTime": "2026-01-02T00:00:00Z",
                "author": {"id": "usrFixture", "name": "Reviewer"},
            }
        ]

    def remove(self, record_id, table="tblPeople"):
        self.records[table].pop(record_id)

    def get(self, url, **kwargs):
        path = urlparse(url).path
        self.calls.append((path, copy.deepcopy(kwargs.get("params", {}))))
        if path == f"/v0/meta/bases/{self.base}/tables":
            return Response({"tables": list(self.tables.values())})
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["v0", self.base]:
            table = parts[2]
            if len(parts) == 3:
                return Response({"records": list(self.records[table].values())})
            if len(parts) == 5 and parts[4] == "comments":
                record = parts[3]
                if (table, record) == self.fail_comments:
                    return Response({"error": {"type": "NOT_AUTHORIZED"}}, 403)
                return Response(
                    {"comments": self.comments.get((table, record), []), "offset": None}
                )
        raise AssertionError(f"Unexpected Airtable request: {path}")


def hashed_embedding(text, dimensions):
    vector = [0.0] * dimensions
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.md5(word.encode(), usedforsecurity=False).digest()
        vector[int.from_bytes(digest[:4], "big") % dimensions] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


async def embed(self, text):
    return [hashed_embedding(item, int(self.get_vector_size())) for item in text]


async def structured_output(text_input=None, system_prompt=None, response_model=str, **kwargs):
    from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

    text = text_input or ""
    if response_model is str:
        # A distinctive fact absent from the question can only arrive through
        # the retriever's context, never through a pre-authored answer.
        return re.sub(r"The question is:\s*`.*?`", "", text, flags=re.DOTALL | re.I)
    if response_model is SummarizedContent:
        return SummarizedContent(summary=text, description=text)
    if response_model is KnowledgeGraph:
        # Fixture facts have intentionally distinctive CamelCase names. Shared
        # names map to identical entities across multiple document source refs.
        names = sorted(set(re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", text)))
        nodes = [
            Node(id=name, name=name, type="FixtureFact", description=f"Stored fact: {name}")
            for name in names
        ]
        edges = [
            Edge(
                source_node_id=left,
                target_node_id=right,
                relationship_name="mentioned_with",
            )
            for left, right in zip(names, names[1:], strict=False)
        ]
        return KnowledgeGraph(nodes=nodes, edges=edges)
    return response_model()


def install_ai():
    from contextlib import ExitStack

    from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
        LiteLLMEmbeddingEngine,
    )
    from cognee.infrastructure.llm import LLMGateway

    stack = ExitStack()
    stack.enter_context(patch.object(LLMGateway, "acreate_structured_output", structured_output))
    stack.enter_context(patch.object(LiteLLMEmbeddingEngine, "embed_text", embed))
    return stack


def clear_engines():
    from cognee.context_global_variables import graph_db_config, vector_db_config
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.infrastructure.databases.vector.embeddings.get_embedding_engine import (
        create_embedding_engine,
    )
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination

    graph_db_config.set(None)
    vector_db_config.set(None)
    _create_graph_engine.cache_clear()
    _create_vector_engine.cache_clear()
    create_relational_engine.cache_clear()
    create_embedding_engine.cache_clear()
    get_dlt_destination.cache_clear()


async def configure_storage(root: Path, *, prune=True):
    import cognee
    from cognee.modules.engine.operations.setup import setup

    os.environ["DLT_DATA_DIR"] = str(root / "dlt")
    clear_engines()
    cognee.config.set_graph_db_config(
        {
            "graph_database_provider": "ladybug",
            "graph_dataset_database_handler": "ladybug",
            "graph_database_subprocess_enabled": False,
            "kuzu_num_threads": 2,
            "kuzu_buffer_pool_size": 1 << 26,
            "kuzu_max_db_size": 1 << 28,
        }
    )
    cognee.config.set_vector_db_config(
        {
            "vector_db_provider": "lancedb",
            "vector_dataset_database_handler": "lancedb",
            "vector_db_subprocess_enabled": False,
        }
    )
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    cognee.config.set_migration_db_config({"migration_db_provider": "sqlite"})
    cognee.config.set_llm_api_key("offline-fixture")
    cognee.config.set_llm_model("openai/gpt-4o-mini")
    cognee.config.set_embedding_provider("openai")
    cognee.config.set_embedding_model("openai/text-embedding-3-small")
    cognee.config.set_embedding_api_key("offline-fixture")
    cognee.config.set_embedding_dimensions(256)
    cognee.config.system_root_directory(str(root / "system"))
    cognee.config.data_root_directory(str(root / "data"))
    if prune:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
    await setup()


async def sync(airtable, dataset="airtable_integration", **options):
    import cognee

    from cognee_community_connector_airtable import airtable_source

    result = await cognee.remember(
        airtable_source(
            base_id=airtable.base, token="offline-fixture", session=airtable, **options
        ),
        dataset_name=dataset,
        self_improvement=False,
        dlt_config={"primary_key": "id", "write_disposition": "merge", "max_rows_per_table": 0},
    )
    assert result.status == "completed", result
    return result


async def dataset_by_name(name="airtable_integration"):
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[name]
    )
    assert len(datasets) == 1
    return datasets[0]


async def documents(name="airtable_integration"):
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data

    return await get_dataset_data((await dataset_by_name(name)).id)


async def store_snapshot(name="airtable_integration"):
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.vector import get_vector_engine_async

    dataset = await dataset_by_name(name)
    async with set_database_global_context_variables(dataset.id, dataset.owner_id):
        nodes, edges = await (await get_graph_engine()).get_graph_data()
        vector = await get_vector_engine_async()
        connection = await vector.get_connection()
        texts = []
        ids = {}
        for collection in await connection.table_names():
            rows = await (await connection.open_table(collection)).to_arrow()
            ids[collection] = rows.column("id").to_pylist()
            for payload in rows.column("payload").to_pylist():
                if isinstance(payload, str):
                    payload = json.loads(payload)
                texts.append(json.dumps(payload, sort_keys=True))
    return {
        "graph": json.dumps(nodes, default=str).lower(),
        "edges": edges,
        "vector": "\n".join(texts).lower(),
        "vector_ids": ids,
    }


def content_text(results):
    """Only result content counts; echoed questions and IDs cannot prove recall."""
    if isinstance(results, str):
        return results.lower()
    if isinstance(results, (list, tuple)):
        return "\n".join(content_text(item) for item in results)
    fields = ("text", "completion", "answer", "summary", "search_result")
    if isinstance(results, dict):
        return "\n".join(content_text(results[key]) for key in fields if key in results)
    return "\n".join(
        content_text(value) for key in fields if (value := getattr(results, key, None)) is not None
    )


async def recall(question, dataset="airtable_integration", *, completion=False):
    import cognee
    from cognee.modules.search.types import SearchType

    return content_text(
        await cognee.recall(
            question,
            query_type=SearchType.RAG_COMPLETION if completion else SearchType.CHUNKS,
            datasets=[dataset],
            top_k=100,
            auto_route=False,
        )
    )
