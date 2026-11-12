"""Real memory acceptance in an isolated disposable process.

The opt-in selects execution only; it cannot bypass the worker's database path,
provider, fresh-process and network checks. Windows stays skipped under WDAC.
No global prune is used. Importing this module does not import Cognee.
"""

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def test_two_pass_remember_in_disposable_process(request, tmp_path):
    """Run only on an explicitly selected, approved non-Windows test runner."""
    if not request.config.getoption("--run-jenkins-memory"):
        pytest.skip("Real memory acceptance requires --run-jenkins-memory on an approved runner")
    if sys.platform == "win32":
        pytest.skip("Windows native execution remains blocked by the documented WDAC policy")
    from remember_worker import sandbox_environment

    worker = Path(__file__).with_name("remember_worker.py")
    root = tmp_path / "memory_runtime"
    # Provision public tokenizer assets before the offline worker starts.
    # tiktoken validates their published hashes; this process never imports
    # Cognee or receives model/database credentials.
    provision_env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    provision_env["TIKTOKEN_CACHE_DIR"] = str(tmp_path / "tokenizer_assets")
    provision = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import tiktoken; tiktoken.get_encoding('cl100k_base'); "
            "tiktoken.get_encoding('o200k_base')",
        ],
        env=provision_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert provision.returncode == 0, provision.stdout + provision.stderr
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(worker), str(root)],
        cwd=tmp_path,
        env=sandbox_environment(root),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads((root / "acceptance_result.json").read_text(encoding="utf-8"))
    assert proof["status"] == "passed"
    assert proof["checks"] == [
        "documents",
        "vectors",
        "graph",
        "source_isolation",
        "dataset_isolation",
        "retained_ids",
        "repeat_sync",
        "document_content",
        "searchability",
        "no_reingestion",
        "failure_memory_retention",
        "failure_cursor_rollback",
        "overlapping_source_ids",
    ]
    assert proof["network_attempts"] == 0
    assert proof["prune_calls"] == 0
    print("JENKINS_MEMORY_ACCEPTANCE=" + json.dumps(proof, sort_keys=True))


def _stub_paid_models(monkeypatch):
    """Stub model calls only; remember, extraction and all stores remain real."""
    from cognee.infrastructure.llm.LLMGateway import LLMGateway
    from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

    calls = {"llm": 0, "embedding": 0}

    async def structured(text_input, system_prompt, response_model, **kwargs):
        calls["llm"] += 1
        if isinstance(response_model, type) and issubclass(response_model, KnowledgeGraph):
            # Marker entities come only from supplied document content.
            names = [
                name
                for name in (
                    "Rotatedmarker",
                    "Deletedmarker",
                    "Newmarker",
                    "Othermarker",
                    "Stablemarker",
                )
                if name in text_input
            ]
            nodes = [
                Node(
                    id=name.lower(),
                    name=name,
                    type="Fixture",
                    description=name + " Jenkins fixture",
                )
                for name in names
            ]
            root = Node(
                id="jenkinsfixture",
                name="Jenkinsfixture",
                type="Fixture",
                description="Jenkins fixture root",
            )
            edges = [
                Edge(
                    source_node_id=root.id,
                    target_node_id=node.id,
                    relationship_name="contains",
                )
                for node in nodes
            ]
            return response_model(nodes=[root, *nodes], edges=edges)
        if isinstance(response_model, type) and issubclass(response_model, SummarizedContent):
            return response_model(summary=text_input[:200], description="Jenkins fixture")
        if response_model is str:
            return text_input
        raise AssertionError("Unstubbed model schema: acceptance must remain offline")

    async def embed(self, text):
        calls["embedding"] += 1
        from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
            EmbeddingResult,
        )

        size = int(self.get_vector_size())
        vectors = []
        for value in text:
            digest = hashlib.sha256(value.encode()).digest()
            vectors.append([digest[i % len(digest)] / 255 for i in range(size)])
        return EmbeddingResult(vectors=vectors, tokens=0)

    monkeypatch.setattr(LLMGateway, "acreate_structured_output", staticmethod(structured))
    for name in ("LiteLLMEmbeddingEngine", "OpenAICompatibleEmbeddingEngine"):
        module = importlib.import_module(
            "cognee.infrastructure.databases.vector.embeddings." + name
        )
        monkeypatch.setattr(getattr(module, name), "embed_text", embed)
    return calls


async def exercise_memory(runtime):
    """Harness contract: cognee/user plus dataset-scoped real store readers.

    documents(dataset) returns {external_id: UUID} for all Jenkins documents;
    vector_texts(dataset) and graph_names(dataset) dump real stored payloads,
    not search results or mocked counts. Backend settings and network denial
    must be checked by the isolated fixture before yielding this runtime.
    """
    from test_jenkins import BASE_URL, FakeJenkins, _build, _Resp

    from cognee_community_connector_jenkins import jenkins_source

    cognee = runtime.cognee
    rotated = _build(1, cause="Rotatedmarker")
    stable = _build(2, cause="Stablemarker")
    fake = FakeJenkins(
        {
            "api": {"builds": [rotated, stable]},
            "web": {"description": "Deletedmarker", "builds": []},
        }
    )
    other = FakeJenkins({"other": {"description": "Othermarker", "builds": []}})

    async def remember(dataset, client, scope, **source_kwargs):
        result = await cognee.remember(
            jenkins_source(
                base_url=BASE_URL,
                service=client,
                resource_name=scope,
                **source_kwargs,
            ),
            dataset_name=dataset,
            self_improvement=False,
            primary_key="id",
            write_disposition="merge",
            max_rows_per_table=0,
        )
        assert result.status == "completed", result

    await remember("jenkins_a", fake, "primary")
    first = await runtime.documents("jenkins_a")
    assert set(first) == {"job:api", "build:api#1", "build:api#2", "job:web"}
    first_contents = await runtime.document_contents("jenkins_a")
    assert "Rotatedmarker" in first_contents["build:api#1"]
    assert "Stablemarker" in first_contents["build:api#2"]
    assert "Deletedmarker" in first_contents["job:web"]
    model_calls = dict(runtime.model_calls)
    fake.calls.clear()
    await remember("jenkins_a", fake, "primary")
    assert runtime.model_calls == model_calls, "Unchanged sync must not re-ingest old content"
    assert await runtime.documents("jenkins_a") == first
    assert not fake.urls("/1/api/json") and not fake.urls("/2/api/json")
    for reader in (runtime.vector_texts, runtime.graph_names):
        contents = "\n".join(await reader("jenkins_a")).lower()
        assert "rotatedmarker" in contents and "deletedmarker" in contents
    await remember("jenkins_b", fake, "primary")
    await remember("jenkins_a", other, "secondary")
    isolated_b = await runtime.documents("jenkins_b")
    with_other = await runtime.documents("jenkins_a")
    assert set(with_other) == {*first, "job:other"}
    assert set(isolated_b) == set(first)
    assert set(isolated_b.values()).isdisjoint(first.values())

    async def snapshot():
        return {
            dataset: (
                await runtime.documents(dataset),
                await runtime.document_contents(dataset),
                sorted(await runtime.vector_texts(dataset)),
                sorted(await runtime.graph_names(dataset)),
            )
            for dataset in ("jenkins_a", "jenkins_b")
        }

    before_failure = await snapshot()
    before_state = runtime.source_states()

    class Broken(FakeJenkins):
        mode = "access"

        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if self.mode == "access" and url.endswith("/job/api/api/json"):
                return _Resp(status=403)
            if self.mode == "partial" and url == BASE_URL + "/api/json":
                response._payload = {"jobs": None}
            if self.mode == "build_partial" and url.endswith("/job/api/api/json"):
                response._payload["partial"] = True
            return response

    broken = Broken(fake.jobs)
    for mode in ("access", "partial", "build_partial"):
        broken.mode = mode
        with pytest.raises(Exception, match=r"Jenkins|discovery"):
            await remember("jenkins_a", broken, "primary")
        assert await snapshot() == before_failure
        assert runtime.source_states() == before_state

    fake.jobs["api"]["builds"] = [rotated, stable, _build(3, cause="Newmarker")]
    fake.calls.clear()

    def interrupt_after_fetch():
        if fake.urls("/3/api/json"):
            raise RuntimeError("fixture interrupted extraction")

    with pytest.raises(Exception, match="fixture interrupted extraction"):
        await remember("jenkins_a", fake, "primary", check_active=interrupt_after_fetch)
    assert await snapshot() == before_failure
    assert runtime.source_states() == before_state
    with (
        patch("dlt.current.resource_state", side_effect=RuntimeError("fixture DLT state failure")),
        pytest.raises(Exception, match="fixture DLT state failure"),
    ):
        await remember("jenkins_a", fake, "primary")
    assert await snapshot() == before_failure
    assert runtime.source_states() == before_state

    fake.calls.clear()
    fake.jobs["api"]["builds"] = [stable, _build(3, cause="Newmarker")]
    del fake.jobs["web"]
    fake.folders = {"": ["api"]}
    await remember("jenkins_a", fake, "primary")
    second = await runtime.documents("jenkins_a")
    assert set(second) == {"job:api", "build:api#2", "build:api#3", "job:other"}
    assert second["job:api"] == first["job:api"]
    assert second["build:api#2"] == first["build:api#2"]
    assert first["build:api#1"] not in second.values()
    assert first["job:web"] not in second.values()
    assert fake.urls("/3/api/json"), "Retry must fetch the build after failed extraction"
    assert "Newmarker" in (await runtime.document_contents("jenkins_a"))["build:api#3"]
    await runtime.search_marker("jenkins_a", "Newmarker")
    for reader in (runtime.vector_texts, runtime.graph_names):
        contents = "\n".join(await reader("jenkins_a")).lower()
        assert "newmarker" in contents and "othermarker" in contents
        assert "rotatedmarker" not in contents and "deletedmarker" not in contents
    assert await runtime.documents("jenkins_b") == isolated_b
    for reader in (runtime.vector_texts, runtime.graph_names):
        contents = "\n".join(await reader("jenkins_b")).lower()
        assert "rotatedmarker" in contents and "deletedmarker" in contents
        assert "newmarker" not in contents and "othermarker" not in contents
    # Revisit both scopes: state/cursor collisions must not reintroduce old rows.
    await remember("jenkins_a", other, "secondary")
    await remember("jenkins_b", fake, "primary")
    assert await runtime.documents("jenkins_a") == second
    third = await runtime.documents("jenkins_b")
    assert set(third) == {"job:api", "build:api#2", "build:api#3"}
    assert set(third.values()).isdisjoint(second.values())

    # Two installations can have identical Jenkins job/build names. Their
    # resource scopes must still prevent document collisions and cross-delete.
    collision_primary = FakeJenkins(
        {"api": {"builds": [rotated, stable]}, "web": {"description": "Deletedmarker"}}
    )
    collision_secondary = FakeJenkins(
        {"api": {"description": "Othermarker", "builds": [_build(1, cause="Othermarker")]}}
    )
    await remember("jenkins_collision", collision_primary, "collision_primary")
    primary_ids = await runtime.documents("jenkins_collision", "collision_primary")
    assert set(primary_ids) == {"job:api", "build:api#1", "build:api#2", "job:web"}
    await remember("jenkins_collision", collision_secondary, "collision_secondary")
    secondary_ids = await runtime.documents("jenkins_collision", "collision_secondary")
    assert set(secondary_ids) == {"job:api", "build:api#1"}
    assert set(primary_ids.values()).isdisjoint(secondary_ids.values())
    assert await runtime.documents("jenkins_collision", "collision_primary") == primary_ids
    del collision_primary.jobs["web"]
    collision_primary.folders = {"": ["api"]}
    collision_primary.jobs["api"]["builds"] = [stable, _build(3, cause="Newmarker")]
    await remember("jenkins_collision", collision_primary, "collision_primary")
    assert await runtime.documents("jenkins_collision", "collision_secondary") == secondary_ids
    remaining = await runtime.documents("jenkins_collision", "collision_primary")
    assert set(remaining) == {"job:api", "build:api#2", "build:api#3"}
    for reader in (runtime.vector_texts, runtime.graph_names):
        contents = "\n".join(await reader("jenkins_collision")).lower()
        assert "othermarker" in contents and "newmarker" in contents
        assert "rotatedmarker" not in contents and "deletedmarker" not in contents

    # A complete empty inventory removes the final job/builds in this scope,
    # while an overlapping source and another dataset retain their own memory.
    collision_primary.folders = {"": []}
    await remember("jenkins_collision", collision_primary, "collision_primary")
    assert await runtime.documents("jenkins_collision", "collision_primary") == {}
    assert await runtime.documents("jenkins_collision", "collision_secondary") == secondary_ids
    assert await runtime.documents("jenkins_a") == second
    assert await runtime.documents("jenkins_b") == third
    for reader in (runtime.vector_texts, runtime.graph_names):
        contents = "\n".join(await reader("jenkins_collision")).lower()
        assert "othermarker" in contents
        assert "newmarker" not in contents and "stablemarker" not in contents
