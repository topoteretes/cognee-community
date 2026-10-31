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

from cognee_community_connector_coda import coda as coda_module
from cognee_community_connector_coda.coda import (
    CodaAPIError,
    CodaAuthError,
    CodaClient,
    CodaRateLimitedError,
    CodaSourceError,
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


def _run(coda, state, stats=None, check_active=None, **overrides):
    stats = stats if stats is not None else {}
    return list(_iter_rows(coda, _config(**overrides), state, stats, check_active))


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
def _client(handler, clock=lambda: 0.0, **kwargs):
    requests, sleeps = [], []

    def record(request):
        requests.append(request)
        return handler(request)

    client = CodaClient(
        "tok-1",
        http=httpx.Client(transport=httpx.MockTransport(record)),
        sleep=sleeps.append,
        clock=clock,
        **kwargs,
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


@pytest.mark.parametrize("retry_after", ["3600", "1"])
def test_client_stops_on_a_long_or_endless_429(retry_after):
    client, requests, sleeps = _client(
        lambda request: httpx.Response(429, headers={"Retry-After": retry_after}),
        max_sleep=10_000.0,
    )
    with pytest.raises(CodaRateLimitedError):
        client.get("/docs")
    assert sleeps == ([] if retry_after == "3600" else [1.0] * 4)
    assert len(requests) == (1 if retry_after == "3600" else 5)


def test_client_retry_waits_stop_at_the_budget_until_reset():
    busy, ok = (
        httpx.Response(503, headers={"Retry-After": "40"}),
        httpx.Response(200, json={"items": []}),
    )
    responses = iter([busy, ok, busy, ok])
    client, _, sleeps = _client(lambda request: next(responses), max_sleep=60.0)
    client.get("/docs")
    with pytest.raises(CodaRateLimitedError):
        client.get("/docs")
    client.reset_sleep_budget()
    assert client.get("/docs") == {"items": []}
    assert sleeps == [40.0]


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


def test_table_changes_come_from_the_table_not_the_listing(doc):
    # A live table listing carries no updatedAt, so a run reads each table's own
    # endpoint, and only the tables that changed have their rows read again.
    state = {}
    _run(doc, state)
    doc.calls.clear()
    assert _run(doc, state) == []
    assert (f"/docs/{DOC}/tables/grid-1", {}) in doc.calls and _row_reads(doc) == []


def test_display_column_is_not_repeated_in_the_table_text(doc):
    rows = _run(doc, {})
    table = next(r for r in rows if r["id"] == f"table:{DOC}:grid-1")
    assert "## Row: Fix export" in table["content"]
    assert "- Task:" not in table["content"]


@pytest.mark.parametrize("status", [404, 410])
def test_table_deleted_after_the_listing_is_forgotten(doc, status):
    state = {}
    _run(doc, state)
    doc.errors[f"/docs/{DOC}/tables/grid-1"] = status
    rows = _run(doc, state)
    assert [r["id"] for r in rows if r["_deleted"]] == [f"table:{DOC}:grid-1"]


def test_table_that_answers_403_is_kept(doc):
    state = {}
    _run(doc, state)
    doc.errors[f"/docs/{DOC}/tables/grid-1"] = 403
    stats = {}
    assert _run(doc, state, stats=stats) == []
    assert "grid-1" in state["docs"][DOC]["tables"] and stats["unreadable"] == 1


def test_new_table_that_answers_403_is_skipped(doc):
    doc.errors[f"/docs/{DOC}/tables/grid-1"] = 403
    rows = _run(doc, {})
    assert f"table:{DOC}:grid-1" not in [r["id"] for r in rows]


def test_touched_but_unchanged_content_is_not_emitted(doc):
    state = {}
    _run(doc, state)
    doc.touch(doc.pages[DOC][0])
    stats = {}
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
    doc.add_page("canvas-2", "Notes", "kept")
    state = {}
    _run(doc, state)
    doc.pages[DOC] = [p for p in doc.pages[DOC] if p["id"] != "canvas-1"]
    doc.tables[DOC].clear()
    # The page goes at once; an empty table listing is confirmed by the next run.
    assert _run(doc, state) == [{"id": f"page:{DOC}:canvas-1", "_deleted": True}]
    assert state["docs"][DOC]["tables"]
    assert _run(doc, state) == [{"id": f"table:{DOC}:grid-1", "_deleted": True}]


def test_table_listing_that_comes_back_is_not_forgotten(doc):
    state = {}
    _run(doc, state)
    saved = list(doc.tables[DOC])
    doc.tables[DOC].clear()
    assert _run(doc, state) == []
    doc.tables[DOC][:] = saved
    assert _run(doc, state) == []
    doc.tables[DOC].clear()
    assert _run(doc, state) == []  # a fresh empty answer starts a new confirmation


def test_deselected_tables_are_forgotten_at_once(doc):
    state = {}
    _run(doc, state)
    rows = _run(doc, state, include_tables=False)
    assert rows == [{"id": f"table:{DOC}:grid-1", "_deleted": True}]


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


@pytest.mark.parametrize("status", [404, 410])
def test_deleted_doc_is_forgotten(doc, status):
    state = {}
    _run(doc, state)
    doc.errors[f"/docs/{DOC}"] = status
    assert _ids(_run(doc, state)) == [f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"]
    assert state["docs"] == {}


def test_doc_whose_access_was_removed_is_left_unchanged(doc):
    state = {}
    _run(doc, state)
    doc.errors[f"/docs/{DOC}"] = 403
    stats = {}
    assert _run(doc, state, stats=stats) == []
    assert stats["unreadable"] == 1 and state["docs"][DOC]["pages"]

    # Access removed between the doc read and its page listing: same answer.
    doc.errors = {f"/docs/{DOC}/pages": 403}
    stats = {}
    assert _run(doc, state, stats=stats) == []
    assert stats["unreadable"] == 1 and state["docs"][DOC]["pages"]


def test_page_whose_access_was_removed_is_kept(doc):
    state = {}
    _run(doc, state)
    doc.touch(doc.pages[DOC][0])
    doc.export_errors["canvas-1"] = 403
    stats = {}
    assert _run(doc, state, stats=stats) == []
    assert "canvas-1" in state["docs"][DOC]["pages"] and stats["unreadable"] == 1


def test_selected_doc_that_was_never_readable_raises(coda):
    with pytest.raises(CodaSourceError, match="Nope1234"):
        _run(coda, {}, doc_ids=("Nope1234",))


def test_answer_without_items_raises_instead_of_forgetting(doc):
    state = {}
    _run(doc, state)
    doc.drop_items = True
    with pytest.raises(CodaAPIError, match="items list"):
        _run(doc, state)
    assert state["docs"][DOC]["pages"]


def test_empty_doc_listing_waits_for_confirmation(doc):
    state = {}
    _run(doc, state, doc_ids=())
    del doc.docs[DOC]
    assert _run(doc, state, doc_ids=()) == []
    assert DOC in state["docs"]
    assert _ids(_run(doc, state, doc_ids=())) == [f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"]
    assert "empty_listing" not in state


def test_empty_page_listing_keeps_the_pages(doc):
    state = {}
    _run(doc, state)
    doc.pages[DOC].clear()
    assert _run(doc, state) == []
    assert "canvas-1" in state["docs"][DOC]["pages"]


def test_rate_limit_stops_cleanly_keeping_finished_docs(doc):
    doc.add_doc("ZzOther1", "Other")
    doc.add_page("p-other", "Notes", "text", doc_id="ZzOther1")
    state = {}
    doc.rate_limited.add("/docs/ZzOther1/pages")
    stats = {}
    rows = _run(doc, state, stats=stats, doc_ids=(DOC, "ZzOther1"))
    assert _ids(rows) == [f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"]
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1
    assert DOC in state["docs"] and "ZzOther1" not in state["docs"]

    doc.rate_limited.clear()
    rows = _run(doc, state, doc_ids=(DOC, "ZzOther1"))
    assert _ids(rows) == ["page:ZzOther1:p-other"]


def test_budget_pauses_and_resumes(doc):
    doc.add_doc("ZzOther1", "Other")
    doc.add_page("p-other", "Notes", "text", doc_id="ZzOther1")
    state = {}
    stats = {}
    rows = _run(doc, state, stats=stats, doc_ids=(DOC, "ZzOther1"), max_requests=1)
    assert _ids(rows) == [f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"]
    # A cut run is incomplete, so failed is set with the budget as the reason.
    assert stats["failed_budget"] == 1 and stats["failed"] == 1
    rows = _run(doc, state, doc_ids=(DOC, "ZzOther1"), max_requests=1)
    assert _ids(rows) == ["page:ZzOther1:p-other"]


def test_budget_cut_runs_still_forget_removed_docs(doc):
    doc.add_doc("ZzOther1", "Other")
    doc.add_page("p-other", "Notes", "text", doc_id="ZzOther1")
    state = {}
    _run(doc, state, doc_ids=())
    del doc.docs["ZzOther1"]
    doc.touch(doc.pages[DOC][0])
    doc.markdown["canvas-1"] = "changed"
    rows = _run(doc, state, doc_ids=(), max_requests=1)
    assert {"id": "page:ZzOther1:p-other", "_deleted": True} in rows


def test_each_run_starts_a_new_wait_budget(doc):
    resets = []
    doc.reset_sleep_budget = lambda: resets.append(1)
    _run(doc, {})
    _run(doc, {})
    assert resets == [1, 1]


def test_check_active_runs_before_every_request(doc):
    calls = []

    def check_active():
        calls.append(len(doc.calls))
        if len(calls) == 3:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        _run(doc, {}, check_active=check_active)
    assert calls == [0, 1, 2] and len(doc.calls) == 2


def test_doc_deleted_during_the_sync_is_forgotten(doc):
    state = {}
    _run(doc, state)
    real_get = doc.get
    doc_calls = []

    def gone_after_it_was_found(path, params=None):
        # Found by the doc lookup, then gone: its pages and the doc both answer 404.
        if path == f"/docs/{DOC}":
            doc_calls.append(path)
            if len(doc_calls) > 1:
                raise CodaAPIError("Coda request failed: HTTP 404", status=404)
        if path == f"/docs/{DOC}/pages":
            raise CodaAPIError("Coda request failed: HTTP 404", status=404)
        return real_get(path, params)

    doc.get = gone_after_it_was_found
    stats = {}
    rows = _run(doc, state, stats=stats)
    assert _ids(rows) == [f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"]
    assert all(row["_deleted"] for row in rows)
    assert stats["gone"] == 1 and state["docs"] == {}


def test_cut_inside_a_doc_keeps_its_finished_pages(doc):
    doc.add_page("canvas-2", "Notes", "second")
    real_export = doc.export_markdown
    limited = {"canvas-2"}

    def export(doc_id, page_id):
        if page_id in limited:
            raise CodaRateLimitedError("Coda rate limit exceeded (HTTP 429)")
        return real_export(doc_id, page_id)

    doc.export_markdown = export
    state, stats = {}, {}
    rows = _run(doc, state, stats=stats)
    # The first page is kept with its row; the doc is read again from the rest.
    assert _ids(rows) == [f"page:{DOC}:canvas-1"]
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1
    assert set(state["docs"][DOC]["pages"]) == {"canvas-1"}
    assert "name" not in state["docs"][DOC]

    limited.clear()
    doc.exports.clear()
    rows = _run(doc, state)
    assert doc.exports == ["canvas-1", "canvas-2"]  # the doc name is new to the state
    assert f"page:{DOC}:canvas-2" in _ids(rows) and f"table:{DOC}:grid-1" in _ids(rows)
    assert state["docs"][DOC]["name"] == "Launch plan"


def test_child_404_of_a_readable_doc_forgets_nothing(doc):
    state = {}
    _run(doc, state)
    doc.errors[f"/docs/{DOC}/pages"] = 404  # the doc itself still answers
    stats = {}
    assert _run(doc, state, stats=stats) == []
    assert stats["gone"] == 0 and stats["unreadable"] == 1
    assert set(state["docs"][DOC]["pages"]) == {"canvas-1"}
    assert set(state["docs"][DOC]["tables"]) == {"grid-1"}


def test_doc_left_out_of_the_selection_is_forgotten(doc):
    doc.add_doc("XyZ12345", "Other")
    doc.add_page("p-x", "Notes", "text", doc_id="XyZ12345")
    state = {}
    _run(doc, state)
    rows = [r for r in _run(doc, state, doc_ids=("XyZ12345",)) if r.get("_deleted")]
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
    source = coda_source([DOC], service=coda, resource_name="coda_team_a")
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "coda"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "coda_team_a"
    assert source.name == "coda_team_a"


def test_default_resource_name_follows_the_scope(coda):
    assert coda_source(["abc123"], client=coda).name == "coda_abc123"
    upper, lower = (
        coda_source(["AbC123"], client=coda).name,
        coda_source(["abc123"], client=coda).name,
    )
    assert upper.startswith("coda_abc123_") and upper != lower
    assert coda_source(service=coda, workspace_id="ws-1").name.startswith("coda_ws_1_")
    # Two accounts syncing all their docs into one dataset would share a constant name.
    for doc_ids in (None, [], [DOC, "other"]):
        with pytest.raises(ValueError, match="Pass a resource_name"):
            coda_source(doc_ids, service=coda)
    assert coda_source(service=coda, resource_name="coda_mine").name == "coda_mine"


@pytest.mark.parametrize("name", ["", "Coda-Team", "coda team", "c" * 65, 7])
def test_invalid_resource_name_is_rejected(coda, name):
    with pytest.raises(ValueError, match="resource_name"):
        coda_source([DOC], service=coda, resource_name=name)


def test_doc_ids_as_a_bare_string_raises(coda):
    with pytest.raises(TypeError, match="not a string"):
        coda_source(DOC, service=coda)


@pytest.mark.parametrize("value", ["日本", "---", "x" * 80])
def test_derived_resource_name_stays_valid(coda, value):
    name = coda_source([value], service=coda).name
    assert name.startswith("coda_") and "__" not in name and len(name) <= 64


def test_client_checks_access_around_every_wait():
    checks, requests = [], []

    def revoke_after_the_first_check():
        checks.append(True)
        if len(checks) > 1:
            raise PermissionError("access revoked")

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    client = CodaClient(
        "tok-1",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda seconds: None,
        check_active=revoke_after_the_first_check,
    )
    with pytest.raises(PermissionError):
        client.get("/docs")
    # Checked before the wait and again after it, so the retry never goes out.
    assert len(requests) == 1 and len(checks) == 2


def test_client_closes_only_the_http_client_it_made():
    def transport():
        return httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    injected = httpx.Client(transport=transport())
    CodaClient("tok-1", http=injected).close()
    assert not injected.is_closed

    owned = CodaClient("tok-1")
    owned._http = made = httpx.Client(transport=transport())
    owned.close()
    assert made.is_closed and owned._http is None


def test_missing_token_raises_without_reading_the_environment(monkeypatch):
    monkeypatch.setenv("CODA_API_TOKEN", "from-env")
    with pytest.raises(ValueError, match="token"):
        coda_source([DOC])


def test_client_is_built_with_keyword_arguments(monkeypatch):
    seen = {}
    monkeypatch.setattr(coda_module, "CodaClient", lambda **kwargs: seen.update(kwargs) or "c")
    coda_source([DOC], token="tok", base_url="https://docs.superhuman.com/apis/v1")
    assert seen == {
        "token": "tok",
        "base_url": "https://docs.superhuman.com/apis/v1",
        "check_active": None,
    }


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


def _staged(pipeline, table="coda_documents"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id FROM {table}") as cursor,
    ):
        return {r[0] for r in cursor.fetchall()}


def _source(coda, name="coda_documents"):
    return coda_source([DOC], service=coda, resource_name=name)


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, doc):
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(doc))
    assert _staged(pipeline) == {f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"}

    # The state lives in dlt, so a fresh source object resumes from it.
    doc.exports.clear()
    source = _source(doc)
    pipeline.run(source)
    assert doc.exports == [] and source.cognee_sync_stats["emitted"] == 0

    # An empty table listing is confirmed by the next run before tables are forgotten.
    doc.tables[DOC].clear()
    pipeline.run(_source(doc))
    assert _staged(pipeline) == {f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"}
    pipeline.run(_source(doc))
    assert _staged(pipeline) == {f"page:{DOC}:canvas-1"}


def test_owned_client_is_closed_and_stats_carry_every_key(dlt_mod, tmp_path, doc, monkeypatch):
    closed = []

    class Owned:
        def __init__(self, **kwargs):
            pass

        def get(self, path, params=None):
            return doc.get(path, params)

        def export_markdown(self, doc_id, page_id):
            return doc.export_markdown(doc_id, page_id)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(coda_module, "CodaClient", Owned)
    source = coda_source([DOC], token="tok")
    _pipeline(dlt_mod, tmp_path).run(source)
    assert closed == [True]
    assert source.cognee_sync_stats["failed"] == 0
    assert {"failed_rate_limit", "failed_budget"} <= set(source.cognee_sync_stats)


def test_two_sources_in_one_dataset_leave_each_other_alone(dlt_mod, tmp_path, doc):
    other = FakeCoda()
    other.add_page("p-b", "Plan B", "text")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(doc, "coda_a"))
    pipeline.run(_source(other, "coda_b"))

    doc.tables[DOC].clear()
    for _ in range(2):  # the second run confirms the empty table listing
        pipeline.run(_source(doc, "coda_a"))
        pipeline.run(_source(other, "coda_b"))
    assert _staged(pipeline, "coda_a") == {f"page:{DOC}:canvas-1"}
    assert _staged(pipeline, "coda_b") == {f"page:{DOC}:p-b"}


def test_failed_run_keeps_staging_and_state(dlt_mod, tmp_path, doc):
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(doc))

    doc.tables[DOC].clear()
    doc.errors[f"/docs/{DOC}/pages"] = 500
    with pytest.raises(Exception, match="500"):
        pipeline.run(_source(doc))
    assert _staged(pipeline) == {f"page:{DOC}:canvas-1", f"table:{DOC}:grid-1"}

    doc.errors.clear()
    pipeline.run(_source(doc))
    pipeline.run(_source(doc))  # confirms the empty table listing
    assert _staged(pipeline) == {f"page:{DOC}:canvas-1"}
