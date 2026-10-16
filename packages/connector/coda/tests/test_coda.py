"""Unit and dlt-pipeline tests for the Coda connector. No live Coda account needed.

* Client tests run ``CodaClient`` against ``httpx.MockTransport``, including the
  asynchronous page export (start, poll, download).
* Rendering tests cover page and table documents.
* Sync tests drive ``_iter_rows`` with a fake API and a plain dict as state, and
  run ``coda_source`` through a dlt pipeline into a temp sqlite destination to
  prove the incremental sync and forget-on-delete.
"""

import json
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from conftest import DOC, FakeCoda, columns, row

from cognee_community_connector_coda.coda import (
    CodaAPIError,
    CodaAuthError,
    CodaClient,
    _CodaConfig,
    _iter_rows,
    coda_source,
    render_page,
    render_table,
)

TASKS = columns("Task", "Owner", "Status", "Tags")


def _config(**overrides):
    values = {
        "doc_ids": (DOC,),
        "workspace_id": None,
        "folder_id": None,
        "include_tables": True,
        "include_hidden_pages": False,
        "include_sync_pages": False,
    }
    values.update(overrides)
    return _CodaConfig(**values)


def _stats():
    return {"emitted": 0, "unchanged": 0, "exported": 0, "deleted": 0, "gone": 0}


def _run(coda, state, stats=None, **overrides):
    stats = stats if stats is not None else _stats()
    return list(_iter_rows(coda, _config(**overrides), state, stats))


def _ids(rows):
    return sorted(r["id"] for r in rows)


def _row_reads(coda):
    return [path for path, _ in coda.calls if path.endswith("/rows")]


@pytest.fixture
def doc(coda):
    coda.add_page("canvas-1", "Spec", "# Goals\n\nShip export.")
    coda.add_table(
        "grid-1",
        "Tasks",
        TASKS,
        [row("i-1", 1, Task="Fix export", Owner="Priya", Status="Blocked", Tags=["api", "v2"])],
    )
    return coda


# ---------------------------------------------------------------------------
# CodaClient over httpx.MockTransport
# ---------------------------------------------------------------------------
def _client(handler, clock=lambda: 0.0):
    requests, sleeps = [], []

    def record(request):
        requests.append(request)
        return handler(request)

    client = CodaClient(
        "tok-1",
        http=httpx.Client(transport=httpx.MockTransport(record)),
        sleep=sleeps.append,
        clock=clock,
    )
    return client, requests, sleeps


def test_export_polls_until_complete_and_downloads_without_the_token():
    statuses = iter(["inProgress", "inProgress", "complete"])

    def handler(request):
        if request.method == "POST":
            assert json.loads(request.content) == {"outputFormat": "markdown"}
            return httpx.Response(202, json={"id": "req-1", "status": "inProgress"})
        if request.url.host == "storage.test":
            return httpx.Response(200, text="# Spec")
        return httpx.Response(
            200,
            json={"status": next(statuses), "downloadLink": "https://storage.test/x.md"},
        )

    client, requests, sleeps = _client(handler)
    assert client.export_markdown(DOC, "canvas-1") == "# Spec"
    assert requests[0].url == f"https://coda.io/apis/v1/docs/{DOC}/pages/canvas-1/export"
    assert requests[0].headers["Authorization"] == "Bearer tok-1"
    assert requests[1].url.path.endswith("/export/req-1")
    assert "Authorization" not in requests[-1].headers
    assert sleeps == [1.0, 2.0]


def test_failed_or_stuck_exports_raise():
    def failed(request):
        if request.method == "POST":
            return httpx.Response(202, json={"id": "req-1"})
        return httpx.Response(200, json={"status": "failed", "error": "boom"})

    client, _, _ = _client(failed)
    with pytest.raises(CodaAPIError, match="failed"):
        client.export_markdown(DOC, "canvas-1")

    def stuck(request):
        if request.method == "POST":
            return httpx.Response(202, json={"id": "req-1"})
        return httpx.Response(200, json={"status": "inProgress"})

    now = iter(range(0, 1000, 50))
    client, _, _ = _client(stuck, clock=lambda: float(next(now)))
    with pytest.raises(CodaAPIError, match="timed out"):
        client.export_markdown(DOC, "canvas-1")


def test_client_retries_429_and_raises_status_only_errors():
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "4"}),
            httpx.Response(200, json={"items": []}),
        ]
    )
    client, _, sleeps = _client(lambda request: next(responses))
    assert client.get("/docs") == {"items": []}
    assert sleeps == [4.0]

    client, _, _ = _client(lambda request: httpx.Response(401))
    with pytest.raises(CodaAuthError):
        client.get("/docs")
    client, _, _ = _client(lambda request: httpx.Response(404, json={"message": f"doc {DOC}"}))
    with pytest.raises(CodaAPIError) as info:
        client.get(f"/docs/{DOC}")
    assert info.value.status == 404 and DOC not in str(info.value)


def test_client_rejects_bad_tokens_without_echoing_them():
    with pytest.raises(ValueError) as info:
        CodaClient("s3cr3t token")
    assert "s3cr3t" not in str(info.value)
    assert "s3cr3t" not in repr(CodaClient("s3cr3t"))
    assert CodaClient("tok", "https://docs.superhuman.com/apis/v1/").base_url == (
        "https://docs.superhuman.com/apis/v1"
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_table_writes_rows_as_labeled_blocks(doc):
    rows = [
        row("i-2", 2, Task="Write docs", Owner="Sam", Status="", Tags=[]),
        row("i-1", 1, Task="Fix export", Owner="Priya", Status="Blocked", Tags=["api", "v2"]),
    ]
    table = doc.tables[DOC][0]
    result = render_table(doc.docs[DOC], table, TASKS, rows)
    assert result["id"] == f"table:{DOC}:grid-1"
    assert result["title"] == "Tasks"
    assert result["content"] == (
        "Doc: Launch plan\nTable: Tasks\n\n"
        "## Row: Fix export\n- Owner: Priya\n- Status: Blocked\n- Tags: api, v2\n\n"
        "## Row: Write docs\n- Owner: Sam"
    )


def test_render_page_adds_the_doc_name(doc):
    page = doc.pages[DOC][0]
    result = render_page(doc.docs[DOC], page, "# Goals\n\nShip export.\n")
    assert result == {
        "id": f"page:{DOC}:canvas-1",
        "title": "Spec",
        "content": "Doc: Launch plan\n\n# Goals\n\nShip export.",
        "url": page["browserLink"],
        "_deleted": False,
    }


def test_row_becomes_a_coda_document(doc):
    result = render_page(doc.docs[DOC], doc.pages[DOC][0], "Ship export.")
    dlt_row = SimpleNamespace(
        table_name="coda_documents",
        primary_key_value=result["id"],
        row_data=result,
        content_hash="h",
    )
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, result["id"]), "coda")
    assert item.data.startswith("# Spec")
    assert item.system_metadata["source"] == "coda"


# ---------------------------------------------------------------------------
# Sync state machine (_iter_rows with a dict as state)
# ---------------------------------------------------------------------------
def test_first_sync_emits_pages_and_tables_but_not_views_embeds_or_hidden(doc):
    doc.add_page("canvas-2", "Video", "", contentType="embed")
    doc.add_page("canvas-3", "Mirror", "mirrored", contentType="syncPage")
    doc.add_page("canvas-4", "Drafts", "secret drafts", isEffectivelyHidden=True)
    doc.add_table("grid-2", "Blocked tasks", TASKS, [], table_type="view")
    assert _ids(_run(doc, {})) == [f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"]
    assert doc.exports == ["canvas-1"]


def test_hidden_and_sync_pages_are_opt_in(doc):
    doc.add_page("canvas-3", "Mirror", "mirrored", contentType="syncPage")
    doc.add_page("canvas-4", "Drafts", "drafts", isEffectivelyHidden=True)
    rows = _run(doc, {}, include_hidden_pages=True, include_sync_pages=True, include_tables=False)
    assert _ids(rows) == [f"page:{DOC}:canvas-{n}" for n in (1, 3, 4)]


def test_no_change_sync_exports_and_reads_nothing(doc):
    state = {}
    _run(doc, state)
    doc.exports.clear()
    doc.calls.clear()

    assert _run(doc, state) == []
    assert doc.exports == [] and _row_reads(doc) == []


def test_edited_page_is_the_only_one_exported_again(doc):
    other = doc.add_page("canvas-2", "Notes", "v1")
    state = {}
    _run(doc, state)
    doc.exports.clear()

    doc.markdown["canvas-2"] = "v2"
    doc.touch(other)
    rows = _run(doc, state)
    assert _ids(rows) == [f"page:{DOC}:canvas-2"]
    assert doc.exports == ["canvas-2"] and "v2" in rows[0]["content"]


def test_edited_table_row_reprocesses_only_the_table(doc):
    state = {}
    _run(doc, state)
    doc.exports.clear()

    doc.rows["grid-1"][0]["values"]["c-status"] = "Done"
    doc.touch(doc.tables[DOC][0])
    rows = _run(doc, state)
    assert _ids(rows) == [f"table:{DOC}:grid-1"]
    assert "- Status: Done" in rows[0]["content"] and doc.exports == []


def test_touched_but_unchanged_content_is_not_emitted(doc):
    state = {}
    _run(doc, state)
    doc.touch(doc.pages[DOC][0])
    stats = _stats()
    assert _run(doc, state, stats=stats) == []
    assert stats["exported"] == 1


def test_renamed_doc_reprocesses_everything(doc):
    state = {}
    _run(doc, state)
    doc.docs[DOC]["name"] = "Launch plan Q4"
    rows = _run(doc, state)
    assert _ids(rows) == [f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"]
    assert all(r["content"].startswith("Doc: Launch plan Q4") for r in rows)


def test_deleted_page_and_table_are_forgotten(doc):
    state = {}
    _run(doc, state)
    doc.pages[DOC].clear()
    doc.tables[DOC].clear()
    assert _run(doc, state) == [
        {"id": f"page:{DOC}:canvas-1", "_deleted": True},
        {"id": f"table:{DOC}:grid-1", "_deleted": True},
    ]


def test_page_deleted_during_the_sync_forgets_only_that_page(doc):
    gone = doc.add_page("canvas-2", "Notes", "v1")
    state = {}
    _run(doc, state)

    doc.touch(gone)
    del doc.markdown["canvas-2"]  # listed, but the export answers 404
    doc.touch(doc.pages[DOC][0])
    doc.markdown["canvas-1"] = "# Goals\n\nShip export by Friday."
    rows = _run(doc, state)
    assert _ids(rows) == [f"page:{DOC}:canvas-1", f"page:{DOC}:canvas-2"]
    assert {"id": f"page:{DOC}:canvas-2", "_deleted": True} in rows
    assert set(state["docs"][DOC]["tables"]) == {"grid-1"}


@pytest.mark.parametrize("status", [404, 403, 410])
def test_deleted_or_unshared_doc_is_forgotten(doc, status):
    state = {}
    _run(doc, state)
    doc.errors[f"/docs/{DOC}"] = status
    stats = _stats()
    assert _ids(_run(doc, state, stats=stats)) == [f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"]
    assert state["docs"] == {}


def test_doc_deleted_during_the_sync_is_forgotten(doc):
    state = {}
    _run(doc, state)
    doc.errors[f"/docs/{DOC}/pages"] = 404  # found, then gone before its pages are read
    stats = _stats()
    rows = _run(doc, state, stats=stats)
    assert _ids(rows) == [f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"]
    assert stats["gone"] == 1 and state["docs"] == {}


def test_doc_left_out_of_the_selection_is_forgotten(doc):
    doc.add_doc("XyZ12345", "Other")
    state = {}
    _run(doc, state)
    rows = _run(doc, state, doc_ids=("XyZ12345",))
    assert all(r["_deleted"] for r in rows) and len(rows) == 2


def test_docs_listed_by_workspace_and_folder(coda):
    coda.add_doc("D2", "Ops", workspace="ws-2", folder="fl-9")
    coda.add_page("p-ops", "Runbook", "steps", doc_id="D2")
    rows = _run(coda, {}, doc_ids=(), workspace_id="ws-2", folder_id="fl-9")
    assert _ids(rows) == ["page:D2:p-ops"]


def test_lists_follow_next_page_tokens():
    coda = FakeCoda(page_size=2)
    for index in range(5):
        coda.add_page(f"p-{index}", f"Page {index}", "text")
    assert len(_run(coda, {}, include_tables=False)) == 5


def test_bad_token_raises_and_forgets_nothing(doc):
    state = {}
    _run(doc, state)
    doc.bad_token = True
    with pytest.raises(CodaAuthError):
        _run(doc, state)
    assert state["docs"][DOC]["pages"]


def test_other_errors_raise_instead_of_forgetting(doc):
    state = {}
    _run(doc, state)
    doc.errors[f"/docs/{DOC}/tables"] = 500
    with pytest.raises(CodaAPIError):
        _run(doc, state)


# ---------------------------------------------------------------------------
# coda_source factory
# ---------------------------------------------------------------------------
def test_source_declares_document_path_and_scope(coda):
    source = coda_source([DOC], client=coda, resource_name="coda_team_a")
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "coda"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "coda_team_a"
    assert source.name == "coda_team_a"


def test_missing_token_names_the_env_var(monkeypatch):
    monkeypatch.delenv("CODA_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="CODA_API_TOKEN"):
        coda_source([DOC])


# ---------------------------------------------------------------------------
# dlt pipeline: incremental sync + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "coda.db").as_posix()
    return dlt.pipeline(
        pipeline_name="coda_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="coda_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM coda_documents") as cursor,
    ):
        return {r[0] for r in cursor.fetchall()}


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, doc):
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(coda_source([DOC], client=doc))
    assert _staged(pipeline) == {f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"}

    # The state lives in dlt, so a fresh source object resumes from it.
    doc.exports.clear()
    source = coda_source([DOC], client=doc)
    pipeline.run(source)
    assert doc.exports == [] and source.cognee_sync_stats["emitted"] == 0

    doc.tables[DOC].clear()
    pipeline.run(coda_source([DOC], client=doc))
    assert _staged(pipeline) == {f"page:{DOC}:canvas-1"}


def test_failed_run_keeps_staging_and_state(dlt_mod, tmp_path, doc):
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(coda_source([DOC], client=doc))

    doc.tables[DOC].clear()
    doc.errors[f"/docs/{DOC}/pages"] = 500
    with pytest.raises(Exception, match="500"):
        pipeline.run(coda_source([DOC], client=doc))
    assert _staged(pipeline) == {f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"}

    doc.errors.clear()
    pipeline.run(coda_source([DOC], client=doc))
    assert _staged(pipeline) == {f"page:{DOC}:canvas-1"}
