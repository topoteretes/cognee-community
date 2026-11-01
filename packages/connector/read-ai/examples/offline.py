"""Synthetic Read.ai and model responses with real local Cognee storage.

Run: uv run python examples/offline.py
All storage lives in a new temporary directory. No real keys or reports are used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from cognee_community_connector_read_ai.auth import ISSUER, JWKS_URL, USERINFO_URL, TokenBundle

ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
SHARED = "Sharedcorp"
TERMS = (ALPHA, BRAVO, CHARLIE, SHARED)


def meeting(number: int, summary: str) -> dict:
    start = 1735689600000 + number * 3600000
    return {
        "id": f"01{number:024d}",
        "title": f"Synthetic planning {number}",
        "start_time_ms": start,
        "end_time_ms": start + 1800000,
        "platform": "Synthetic",
        "participants": [{"name": "Alice", "email": "private@example.invalid"}],
        "summary": summary,
        "transcript": {
            "turns": [
                {
                    "speaker": {"name": "Alice"},
                    "text": summary,
                    "start_time_ms": start,
                    "end_time_ms": start + 1000,
                }
            ],
        },
        "metrics": {"engagement": 0, "sentiment": 0.9},
    }


class SyntheticReadAI:
    """OAuth proof and mutable provider data; network access is wholly mocked."""

    def __init__(self, meetings: list[dict], subject: str = "synthetic-account"):
        self.meetings = {item["id"]: item for item in meetings}
        self.subject = subject
        self.denied: set[str] = set()
        self.calls: list[str] = []
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk.update(kid="synthetic", use="sig", alg="RS256")
        self.client = httpx.Client(transport=httpx.MockTransport(self.respond))

    def get_tokens(self, *, force_refresh: bool = False) -> TokenBundle:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": self.subject,
            "aud": "synthetic-client",
            "iat": now,
            "exp": now + 3600,
        }
        token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "synthetic"})
        return TokenBundle("synthetic-access-token", "synthetic-client", token)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json={"keys": [self.jwk]})
        if str(request.url) == USERINFO_URL:
            return httpx.Response(200, json={"sub": self.subject})
        if request.url.host != "api.read.ai":
            raise AssertionError(f"Unexpected network request: {request.url}")
        found = re.search(r"/meetings/([^/]+)$", request.url.path)
        if found:
            identity = found[1]
            if identity in self.denied:
                return httpx.Response(403, json={"error": "permission denied"})
            if identity not in self.meetings:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=self.meetings[identity])
        if request.url.path.endswith("/meetings"):
            values = sorted(self.meetings.values(), key=lambda item: item["id"])
            lower = request.url.params.get("start_time_ms.gte")
            upper = request.url.params.get("start_time_ms.lt")
            values = [
                item
                for item in values
                if (lower is None or item["start_time_ms"] >= int(lower))
                and (upper is None or item["start_time_ms"] < int(upper))
            ]
            cursor = request.url.params.get("cursor")
            if cursor:
                values = [item for item in values if item["id"] > cursor]
            limit = int(request.url.params.get("limit", "10"))
            return httpx.Response(
                200,
                json={"object": "list", "data": values[:limit], "has_more": len(values) > limit},
            )
        raise AssertionError(f"Unexpected API route: {request.url}")


class SyntheticModels:
    """Only model boundaries are mocked; indexing and cleanup remain real."""

    def __init__(self):
        self.extractions = 0
        self.embeddings = 0
        self.patches = []

    async def structured(self, text_input=None, system_prompt=None, response_model=str, **kwargs):
        from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

        if response_model is str:
            return "Synthetic answer"
        text = str(text_input or "")
        if response_model == SummarizedContent:
            return SummarizedContent(summary=text, description="Synthetic summary")
        if response_model == KnowledgeGraph:
            self.extractions += 1
            return KnowledgeGraph(
                nodes=[
                    Node(id=term, name=term, type="Company", description=f"{term} company")
                    for term in TERMS
                    if term in text
                ],
                edges=[
                    Edge(
                        source_node_id=term,
                        target_node_id=SHARED,
                        relationship_name="works_with",
                        description=f"{term} works with {SHARED}",
                    )
                    for term in TERMS
                    if term != SHARED and term in text and SHARED in text
                ],
            )
        return response_model()

    def install(self):
        from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
            LiteLLMEmbeddingEngine,
        )
        from cognee.infrastructure.llm import LLMGateway

        models = self

        async def embed(engine, texts):
            models.embeddings += len(texts)
            vectors = []
            for text in texts:
                # Deterministic nonzero vectors support genuine vector queries.
                digest = hashlib.sha256(str(text).encode()).digest()
                size = engine.get_vector_size()
                vectors.append([(digest[index % len(digest)] + 1) / 256 for index in range(size)])
            return vectors

        self.patches = [
            patch.object(LLMGateway, "acreate_structured_output", self.structured),
            patch.object(LiteLLMEmbeddingEngine, "embed_text", embed),
        ]
        for current in self.patches:
            current.start()
        return self

    def close(self):
        for current in reversed(self.patches):
            current.stop()


def configure_storage(root: Path):
    """Configure only an isolated root; never prune a developer's storage."""
    os.environ["READ_AI_RATE_DIR"] = str(root / "rates")
    os.environ["PIPELINES_DIR"] = str(root / "dlt-pipelines")
    os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
    os.environ["COGNEE_LOG_FILE"] = "false"
    os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
    os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "false"
    os.environ["LLM_API_KEY"] = "synthetic-offline-key"
    os.environ["EMBEDDING_API_KEY"] = "synthetic-offline-key"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import cognee
    import cognee.modules.migrations.startup as migration_startup
    from cognee.context_global_variables import graph_db_config, vector_db_config
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination

    # The core guard is per process; this helper deliberately selects a new store.
    migration_startup._startup_migrations_done = False
    get_dlt_destination.cache_clear()
    _create_graph_engine.cache_clear()
    _create_vector_engine.cache_clear()
    create_relational_engine.cache_clear()
    graph_db_config.set(None)
    vector_db_config.set(None)
    cognee.config.system_root_directory(str(root / "system"))
    cognee.config.data_root_directory(str(root / "data"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    cognee.config.set_migration_db_config({"migration_db_provider": "sqlite"})
    cognee.config.set_graph_db_config(
        {
            "graph_database_provider": "ladybug",
            "graph_dataset_database_handler": "ladybug",
            "graph_database_subprocess_enabled": False,
        }
    )
    cognee.config.set_vector_db_config(
        {
            "vector_db_provider": "lancedb",
            "vector_dataset_database_handler": "lancedb",
            "vector_db_subprocess_enabled": False,
        }
    )
    cognee.config.set_vector_db_url(str(root / "system" / "databases" / "cognee.lancedb"))
    cognee.config.set_llm_api_key("synthetic-offline-key")
    cognee.config.set_embedding_api_key("synthetic-offline-key")
    cognee.config.set_embedding_provider("openai")
    cognee.config.set_embedding_model("openai/text-embedding-3-small")
    cognee.config.set_embedding_dimensions(32)


async def main():
    import cognee
    from cognee.modules.search.types import SearchType

    from cognee_community_connector_read_ai import read_ai_source, sync_read_ai

    with tempfile.TemporaryDirectory(prefix="read-ai-offline-") as directory:
        root = Path(directory)
        configure_storage(root)
        models = SyntheticModels().install()
        provider = SyntheticReadAI([meeting(1, f"{ALPHA} and {SHARED} approved the project.")])
        try:
            source = read_ai_source(
                connection_id="offline",
                token_provider=provider,
                client=provider.client,
                state_dir=root / "connector",
            )
            result = await sync_read_ai(source, dataset_name="read_ai_offline")
            hits = await cognee.search(
                ALPHA, query_type=SearchType.CHUNKS, datasets=["read_ai_offline"]
            )
            assert hits and ALPHA in str(hits)
            print(f"Created {result.created} meeting document; retrieved {len(hits)} result(s).")
        finally:
            provider.client.close()
            models.close()


if __name__ == "__main__":
    asyncio.run(main())
