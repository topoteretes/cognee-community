"""Forget-on-delete must remove a comment's cognified graph content, not
just its Data record. Syncs two pull requests, each with one comment
containing a distinctive company name, cognifies (LLM + embeddings mocked,
no live credentials), deletes one comment upstream, re-syncs, and asserts
the deleted comment's extracted entity is gone from the graph while the
surviving comment's entity remains.

Modeled directly on the Google Drive connector's test_google_drive_forget.py
-- the only test in this repo's connectors that proves cognee's
orphan_cleanup actually reaches the graph/vector stores (not just the dlt
staging table); see the regression it guards against in
resolve_dlt_sources.py's _delete_dlt_orphans (graph cleanup must not be
gated on a relational-ledger-only check).

The deletion target here is a COMMENT, not a pull request: Bitbucket pull
requests can never be deleted (see the module docstring in bitbucket.py), so
there is no equivalent "delete the whole PR" scenario to test end to end --
forget-on-delete for a PR itself only ever fires when it drops out of a
narrowed ``pr_states``, which is exercised at the plain-dict-state level in
test_bitbucket.py, not here.
"""

import importlib

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_bitbucket import bitbucket_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "bitbucket_forget_test"
WORKSPACE = "ws"
REPO = "repo"
# Distinctive, unique tokens so each comment maps to exactly one graph entity.
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


def _pr(pr_id, title):
    return {
        "id": pr_id,
        "title": title,
        "state": "OPEN",
        "author": {"display_name": "Jane Doe"},
        "source": {"branch": {"name": "feature"}},
        "destination": {"branch": {"name": "main"}},
        "created_on": "2024-01-01T00:00:00+00:00",
        "updated_on": "2024-01-01T00:00:00+00:00",
        "comment_count": 0,
        "description": "",
        "reviewers": [],
        "links": {
            "html": {"href": f"https://bitbucket.org/{WORKSPACE}/{REPO}/pull-requests/{pr_id}"}
        },
    }


def _comment(comment_id, raw):
    return {
        "id": comment_id,
        "content": {"raw": raw},
        "deleted": False,
        "user": {"display_name": "John Doe"},
        "links": {"html": {"href": f"https://bitbucket.org/comments/{comment_id}"}},
    }


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def json(self):
        return self._payload


class FakeBitbucketWorkspace:
    """Minimal in-memory Bitbucket Cloud double for one repo: a fixed set of
    pull requests and per-PR comments that can be mutated between syncs
    (comments added/deleted) to exercise real forget-on-delete end to end.
    """

    def __init__(self):
        self.prs: list[dict] = []
        self.comments_by_pr: dict[int, list[dict]] = {}

    def add_pr(self, pr_id, title):
        self.prs.append(_pr(pr_id, title))
        self.comments_by_pr[pr_id] = []

    def add_comment(self, pr_id, comment_id, raw):
        self.comments_by_pr[pr_id].append(_comment(comment_id, raw))
        self._sync_comment_count(pr_id)

    def delete_comment(self, pr_id, comment_id):
        self.comments_by_pr[pr_id] = [
            c for c in self.comments_by_pr[pr_id] if c["id"] != comment_id
        ]
        self._sync_comment_count(pr_id)

    def _sync_comment_count(self, pr_id):
        count = len(self.comments_by_pr[pr_id])
        for pr in self.prs:
            if pr["id"] == pr_id:
                pr["comment_count"] = count

    def session(self):
        return _FakeSession(self)


class _FakeSession:
    """No pagination/state-based routing needed: this fixture is small
    enough that every listing fits on one page, and ``repo_slugs=[REPO]`` is
    always passed explicitly, so there is no repository listing call either.
    """

    def __init__(self, workspace: FakeBitbucketWorkspace):
        self._workspace = workspace

    def get(self, url, params=None):
        if url.endswith("/pullrequests"):
            return _Resp({"values": list(self._workspace.prs), "next": None})
        for pr_id, comments in self._workspace.comments_by_pr.items():
            if url.endswith(f"/pullrequests/{pr_id}/comments"):
                return _Resp({"values": list(comments), "next": None})
        return _Resp({"values": [], "next": None})


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((t for t in (ALPHA, BRAVO) if text_input and t in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="Company", description=f"{name} entity")]
            if name
            else []
        )
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    for _nid, props in nodes:
        if any(token in str(v).lower() for v in (props or {}).values()):
            return True
    return False


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _sync(workspace: FakeBitbucketWorkspace):
    await cognee.add(
        bitbucket_source(
            workspace=WORKSPACE,
            repo_slugs=[REPO],
            access_token="tok",
            session=workspace.session(),
        ),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_comment_forgets_its_graph_content(clean_environment):
    workspace = FakeBitbucketWorkspace()
    workspace.add_pr(1, "PR Alpha")
    workspace.add_pr(2, "PR Bravo")
    workspace.add_comment(1, 101, f"{ALPHA} is a company in the logistics sector.")
    workspace.add_comment(2, 201, f"{BRAVO} is an unrelated company in the finance sector.")

    await _sync(workspace)
    assert await _graph_has(ALPHA), "comment 101's entity should be in the graph after ingest"
    assert await _graph_has(BRAVO), "comment 201's entity should be in the graph after ingest"

    # Delete comment 101 upstream; the re-sync must forget its content.
    workspace.delete_comment(1, 101)
    await _sync(workspace)

    assert await _graph_has(BRAVO), "surviving comment 201's entity must remain after deletion"
    assert not await _graph_has(ALPHA), (
        "deleted comment 101's entity must be removed from the graph (forget-on-delete), "
        "not just its Data record"
    )
