"""Offline Graph fixtures with actual DLT extraction and SQLite merge."""

import json
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import dlt
import httpx
import pytest
from dlt.extract.exceptions import InvalidResourceDataTypeMultiplePipes
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_onenote import list_notebooks, onenote_source
from cognee_community_connector_onenote.onenote import OneNoteError


class Graph:
    def __init__(self):
        self.notebooks = {"a": "Project Alpha", "b": "Excluded"}
        self.sections = {"a": "Planning", "b": "Private"}
        self.pages = {"a": [self.page("p1")], "b": [self.page("secret")]}
        self.html = {
            "p1": "<html><body><h1>Plan</h1><p>Original body</p>"
            '<img alt="Architecture diagram" src="https://graph.microsoft.com/v1.0/'
            'me/onenote/resources/image/content"></body></html>',
            "secret": "<p>Excluded content</p>",
        }
        self.errors = {}
        self.requests = []
        self.gone_code = "20113"

    @staticmethod
    def page(identity, timestamp="2026-10-01T00:00:00Z"):
        return {
            "id": identity,
            "title": identity,
            "createdDateTime": "2026-01-01T00:00:00Z",
            "lastModifiedDateTime": timestamp,
            "links": {"oneNoteWebUrl": {"href": f"https://onenote.com/{identity}"}},
        }

    def handle(self, request):
        assert request.headers["Authorization"] == "Bearer offline-token"
        path = request.url.path.removeprefix("/v1.0/me/onenote/")
        self.requests.append((path, dict(request.url.params)))
        if path in self.errors:
            status, code = self.errors[path]
            return httpx.Response(
                status, json={"error": {"code": code, "message": "Fixture error"}}
            )
        parts = path.split("/")
        if path == "notebooks":
            values = [
                {"id": identity, "displayName": title} for identity, title in self.notebooks.items()
            ]
        elif parts[0] == "notebooks" and len(parts) == 2:
            return httpx.Response(
                200, json={"id": parts[1], "displayName": self.notebooks[parts[1]]}
            )
        elif parts[0] == "notebooks" and parts[-1] == "sections":
            values = [{"id": f"s-{parts[1]}", "displayName": self.sections[parts[1]]}]
        elif parts[-1] == "sectionGroups":
            values = []
        elif parts[0] == "sections" and parts[-1] == "pages":
            values = self.pages[parts[1].removeprefix("s-")]
        elif parts[0] == "pages" and parts[-1] == "content":
            return httpx.Response(
                200,
                text=f"<html><body>{self.html[parts[1]]}</body></html>",
                headers={"content-type": "text/html"},
            )
        elif parts[0] == "pages":
            for notebook, pages in self.pages.items():
                for page in pages:
                    if page["id"] == parts[1]:
                        metadata = deepcopy(page)
                        metadata["parentNotebook"] = {
                            "id": notebook,
                            "displayName": self.notebooks[notebook],
                        }
                        metadata["parentSection"] = {
                            "id": f"s-{notebook}",
                            "displayName": self.sections[notebook],
                        }
                        return httpx.Response(200, json=metadata)
            return httpx.Response(
                404, json={"error": {"code": self.gone_code, "message": "Page gone"}}
            )
        else:
            raise AssertionError(f"Unexpected Graph request: {request.url}")
        assert request.url.params.get("$orderby") == "createdDateTime asc"
        values = sorted(values, key=lambda row: (row.get("createdDateTime", ""), row["id"]))
        skip = int(request.url.params.get("$skip", "0"))
        top = int(request.url.params.get("$top", "100"))
        return httpx.Response(200, json={"value": values[skip : skip + top]})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def content_requests(self):
        return Counter(path for path, _ in self.requests if path.endswith("/content"))


@pytest.fixture
def pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="onenote_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path}/data.db"),
        dataset_name="notes",
        pipelines_dir=str(tmp_path / "state"),
    )


def sync(pipeline, graph, selection=("a",), account="tenant:user", **kwargs):
    tables = []
    for notebook in selection:
        with graph.client() as client:
            resource = onenote_source(
                "offline-token",
                notebook_id=notebook,
                account_id=account,
                http_client=client,
                **kwargs,
            )
            tables.append(resource.name)
            pipeline.run(resource, write_disposition="merge")
            graph.last_stats = resource.cognee_sync_stats
    return tables


def rows(pipeline, table):
    # Table names are generated by the connector, never supplied by a provider.
    assert table.replace("_", "").isalnum()
    with pipeline.sql_client() as client:
        with client.execute_query(f'SELECT id, title, content FROM "{table}"') as cursor:
            return list(cursor.fetchall())


def test_initial_selection_html_and_unchanged_replay(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    first = rows(pipeline, table)
    assert len(first) == 1
    assert "Original body" in first[0][2]
    assert "Project Alpha" in first[0][2]
    assert "Planning" in first[0][2]
    assert "Architecture diagram" in first[0][2]
    assert "resources/image/content" in first[0][2]
    calls = graph.content_requests()
    sync(pipeline, graph)
    assert rows(pipeline, table) == first
    assert graph.content_requests() == calls
    assert not any("secret" in path for path, _ in graph.requests)


def test_rename_rebuilds_breadcrumbs_without_html_fetch(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    calls = graph.content_requests()
    graph.notebooks["a"] = "Project Beta"
    graph.sections["a"] = "Execution"
    sync(pipeline, graph)
    content = rows(pipeline, table)[0][2]
    assert "Project Beta" in content and "Execution" in content
    assert "Project Alpha" not in content and "Planning" not in content
    assert graph.content_requests() == calls


def test_edits_timestamp_only_and_older_new_page(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    first = rows(pipeline, table)
    graph.pages["a"][0]["lastModifiedDateTime"] = "2026-10-02T00:00:00Z"
    sync(pipeline, graph)
    assert rows(pipeline, table) == first
    graph.pages["a"][0]["lastModifiedDateTime"] = "2026-10-03T00:00:00Z"
    graph.html["p1"] = "<p>Updated body</p>"
    graph.pages["a"].append(Graph.page("older", "2020-01-01T00:00:00Z"))
    graph.html["older"] = "<p>Older new page</p>"
    sync(pipeline, graph)
    content = "\n".join(row[2] for row in rows(pipeline, table))
    assert "Updated body" in content and "Older new page" in content
    assert "Original body" not in content


@pytest.mark.parametrize("code", ["20113", "20102"])
def test_confirmed_final_page_deletion_physically_empties_table(pipeline, code):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    graph.pages["a"] = []
    graph.gone_code = code
    sync(pipeline, graph)
    assert rows(pipeline, table) == []


@pytest.mark.parametrize(
    "status,code",
    [(403, "40003"), (404, "20112"), (404, "unknown"), (401, "20113"), (403, "20102")],
)
def test_missing_page_access_or_ambiguous_failure_preserves_table(pipeline, status, code):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.pages["a"] = []
    graph.errors["pages/p1"] = (status, code)
    sync(pipeline, graph)
    assert graph.last_stats["failed"] > 0
    assert graph.last_stats["complete"] == 0
    assert rows(pipeline, table) == before


def test_failed_notebook_preserves_its_snapshot(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.errors["notebooks/a/sections"] = (403, "40003")
    sync(pipeline, graph)
    assert graph.last_stats["failed"] > 0
    assert rows(pipeline, table) == before
    del graph.errors["notebooks/a/sections"]
    sync(pipeline, graph)
    assert graph.last_stats["complete"] == 1


def test_deselection_preserves_table_and_reselection_cache(pipeline):
    graph = Graph()
    tables = sync(pipeline, graph, ("a", "b"))
    before = {table: rows(pipeline, table) for table in tables}
    calls = graph.content_requests()
    sync(pipeline, graph, ("a",))
    assert {table: rows(pipeline, table) for table in tables} == before
    sync(pipeline, graph, ("a", "b"))
    assert graph.content_requests() == calls


def test_account_scope_produces_separate_resources(pipeline):
    graph = Graph()
    first = sync(pipeline, graph, account="tenant:first")
    second = sync(pipeline, graph, account="tenant:second")
    assert set(first).isdisjoint(second)
    assert all(len(rows(pipeline, table)) == 1 for table in first + second)
    assert graph.content_requests()["pages/p1/content"] == 2


@pytest.mark.parametrize("count", [0, 3, 100, 200])
def test_exact_pagination_boundaries(pipeline, count):
    graph = Graph()
    graph.pages["a"] = [Graph.page(f"p{i}") for i in range(count)]
    graph.html.update({f"p{i}": f"<p>Body {i}</p>" for i in range(count)})
    (table,) = sync(pipeline, graph, max_requests=1000)
    assert len(rows(pipeline, table)) == count


def test_request_budget_preserves_previous_snapshot(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    sync(pipeline, graph, max_requests=1)
    assert graph.last_stats["failed_budget"] == 1
    assert graph.last_stats["requests"] == 1
    assert graph.last_stats["complete"] == 0
    assert rows(pipeline, table) == before


def test_notebook_listing_uses_delegated_token_provider():
    graph = Graph()
    with graph.client() as client:
        result = list_notebooks(lambda: "offline-token", http_client=client)
    assert {item["id"] for item in result} == {"a", "b"}


def test_empty_selection_is_rejected():
    with pytest.raises(ValueError):
        onenote_source("offline-token", notebook_id="", account_id="account")


def test_confirmed_move_outside_selection_deletes_copy_without_fetch(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    calls = graph.content_requests()
    graph.pages["b"].append(graph.pages["a"].pop())
    graph.html["p1"] = "<p>Excluded update</p>"
    sync(pipeline, graph)
    assert before
    assert rows(pipeline, table) == []
    assert graph.content_requests() == calls


def test_move_between_independently_synced_notebooks(pipeline):
    graph = Graph()
    tables = sync(pipeline, graph, ("a", "b"))
    calls = graph.content_requests()
    graph.pages["b"].append(graph.pages["a"].pop())
    sync(pipeline, graph, ("a", "b"))
    all_rows = [row for table in tables for row in rows(pipeline, table)]
    assert len(all_rows) == 2
    moved = next(row for row in all_rows if "Original body" in row[2])
    assert "Excluded > Private" in moved[2]
    assert graph.content_requests()["pages/p1/content"] == calls["pages/p1/content"] + 1


def test_renderer_version_invalidates_unchanged_cache(pipeline, monkeypatch):
    from cognee_community_connector_onenote import onenote

    graph = Graph()
    sync(pipeline, graph)
    monkeypatch.setattr(onenote, "_RENDERER_VERSION", onenote._RENDERER_VERSION + 1)
    sync(pipeline, graph)
    assert graph.content_requests()["pages/p1/content"] == 2


def test_pending_load_recovery_then_fresh_extraction(pipeline, monkeypatch):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    graph.pages["a"][0]["lastModifiedDateTime"] = "2026-10-03T00:00:00Z"
    graph.html["p1"] = "<p>Recover me</p>"
    original_load = pipeline.load

    def failed_load(*args, **kwargs):
        raise RuntimeError("Injected load failure")

    monkeypatch.setattr(pipeline, "load", failed_load)
    with pytest.raises(RuntimeError, match="Injected load failure"):
        sync(pipeline, graph)
    calls = graph.content_requests()
    monkeypatch.setattr(pipeline, "load", original_load)
    with graph.client() as client:
        source = onenote_source(
            "offline-token", notebook_id="a", account_id="tenant:user", http_client=client
        )
        pipeline.run(source)
        assert source.cognee_sync_stats == {}
    assert "Recover me" in rows(pipeline, table)[0][2]
    sync(pipeline, graph)
    assert graph.content_requests() == calls
    assert "Recover me" in rows(pipeline, table)[0][2]


def test_declared_document_and_dataset_scope():
    from cognee.tasks.ingestion.dlt_utils import (
        DOCUMENT_SOURCE_ATTR,
        PIPELINE_SCOPE_ATTR,
        pipeline_name_for_source,
    )

    source = onenote_source("offline-token", notebook_id="a", account_id="tenant:user")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "onenote"
    assert getattr(source, PIPELINE_SCOPE_ATTR)
    assert pipeline_name_for_source(source, "first") != pipeline_name_for_source(source, "second")


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_and_five_total_attempts(monkeypatch, status):
    from cognee_community_connector_onenote import onenote

    sleeps = []
    monkeypatch.setattr(onenote.time, "sleep", sleeps.append)
    requests = []

    def throttled(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"Retry-After": "2"}, json={"error": {"code": "throttled"}}
        )

    with httpx.Client(transport=httpx.MockTransport(throttled)) as client:
        with pytest.raises(OneNoteError):
            list_notebooks("offline-token", http_client=client)
    assert len(requests) == 5
    assert sleeps == [2] * 4


def test_retry_recovers_and_token_is_not_in_error(monkeypatch):
    from cognee_community_connector_onenote import onenote

    monkeypatch.setattr(onenote.time, "sleep", lambda _: None)
    requests = []

    def response(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"value": []})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert list_notebooks("offline-token", http_client=client) == []
    assert len(requests) == 2
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401))) as client:
        with pytest.raises(OneNoteError, match="reauthenticate") as failure:
            list_notebooks("secret-token", http_client=client)
    assert "secret-token" not in str(failure.value)


def test_transport_failure_has_bounded_backoff_and_redacted_diagnostics(monkeypatch):
    from cognee_community_connector_onenote import onenote

    requests, sleeps = [], []
    monkeypatch.setattr(onenote.time, "sleep", sleeps.append)

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("secret-token", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(OneNoteError) as failure:
            list_notebooks("secret-token", http_client=client)
    assert len(requests) == 5
    assert sleeps == [1, 2, 4, 8]
    assert "secret-token" not in str(failure.value)


@pytest.mark.parametrize("retry_after", ["bad", "-1", "inf", "nan"])
def test_invalid_retry_after_uses_bounded_fallback(monkeypatch, retry_after):
    from cognee_community_connector_onenote import onenote

    sleeps = []
    monkeypatch.setattr(onenote.time, "sleep", sleeps.append)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": retry_after})
        )
    ) as client:
        with pytest.raises(OneNoteError):
            list_notebooks("offline-token", http_client=client)
    assert sleeps == [1, 2, 4, 8]


@pytest.mark.parametrize(
    "next_link",
    [
        "http://graph.microsoft.com/v1.0/me/onenote/notebooks",
        "https://evil.example/v1.0/me/onenote/notebooks",
        "https://graph.microsoft.com.evil.example/v1.0/me/onenote/notebooks",
        "https://user:password@graph.microsoft.com/v1.0/me/onenote/notebooks",
    ],
)
def test_unsafe_pagination_link_never_receives_token(next_link):
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, json={"value": [{"id": "a"}], "@odata.nextLink": next_link})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(OneNoteError, match="Unsafe"):
            list_notebooks("offline-token", http_client=client)
    assert len(requests) == 1


def test_injected_redirect_enabled_client_still_does_not_follow_redirects():
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example"})

    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        with pytest.raises(OneNoteError):
            list_notebooks("offline-token", http_client=client)
    assert len(requests) == 1


@pytest.mark.parametrize("duplicate", [False, True])
def test_next_link_cycle_and_duplicate_page_fail(duplicate):
    requests = []

    def response(request):
        requests.append(request)
        item_id = "a" if duplicate else str(len(requests))
        next_link = (
            "https://graph.microsoft.com/v1.0/me/onenote/notebooks?$skiptoken=next"
            if duplicate
            else str(request.url)
        )
        return httpx.Response(200, json={"value": [{"id": item_id}], "@odata.nextLink": next_link})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(OneNoteError):
            list_notebooks("offline-token", http_client=client)
    assert len(requests) == (2 if duplicate else 1)


def test_successful_next_link_is_followed():
    requests = []

    def response(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "a"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/onenote/"
                    "notebooks?$skiptoken=next",
                },
            )
        assert request.url.params["$skiptoken"] == "next"
        return httpx.Response(200, json={"value": [{"id": "b"}]})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert [row["id"] for row in list_notebooks("offline-token", http_client=client)] == [
            "a",
            "b",
        ]


def test_confirmed_parent_deletion_reconciles_descendant_and_empty_table(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    del graph.notebooks["a"]
    graph.pages["a"] = []
    graph.errors["notebooks/a"] = (404, "20113")
    sync(pipeline, graph)
    assert rows(pipeline, table) == []
    assert any(path == "pages/p1" for path, _ in graph.requests)


@pytest.mark.parametrize("status,code", [(403, "40003"), (404, "20102")])
def test_inaccessible_parent_is_not_treated_as_deletion(pipeline, status, code):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    del graph.notebooks["a"]
    graph.errors["notebooks/a"] = (status, code)
    sync(pipeline, graph)
    assert graph.last_stats["failed"] > 0
    assert graph.last_stats["complete"] == 0
    assert rows(pipeline, table) == before


def test_malformed_collection_preserves_loaded_table(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    original = graph.handle

    def malformed(request):
        if request.url.path.endswith("/sections/s-a/pages"):
            return httpx.Response(200, json={"value": None})
        return original(request)

    graph.handle = malformed
    sync(pipeline, graph)
    assert graph.last_stats["failed"] > 0
    assert graph.last_stats["complete"] == 0
    assert rows(pipeline, table) == before


def test_later_pagination_failure_preserves_snapshot_and_cache(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    calls = graph.content_requests()
    original = graph.handle
    graph.pages["a"] = [Graph.page(f"p{i}") for i in range(101)]

    def failed_batch(request):
        if (
            request.url.path.endswith("/sections/s-a/pages")
            and request.url.params.get("$skip") == "100"
        ):
            return httpx.Response(403, json={"error": {"code": "40003"}})
        return original(request)

    graph.handle = failed_batch
    sync(pipeline, graph)
    assert graph.last_stats["failed"] > 0
    assert graph.last_stats["complete"] == 0
    assert rows(pipeline, table) == before
    assert graph.content_requests() == calls
    graph.handle = original
    graph.pages["a"] = [Graph.page("p1")]
    sync(pipeline, graph)
    assert rows(pipeline, table) == before
    assert graph.content_requests() == calls


@pytest.fixture
def nested_graph():
    graph = Graph()
    original = graph.handle

    def nested(request):
        path = request.url.path.removeprefix("/v1.0/me/onenote/")
        if path == "notebooks/a/sections":
            return httpx.Response(200, json={"value": []})
        if path == "notebooks/a/sectionGroups":
            return httpx.Response(200, json={"value": [{"id": "group", "displayName": "Research"}]})
        if path == "sectionGroups/group/sections" and path not in graph.errors:
            return httpx.Response(200, json={"value": [{"id": "s-a", "displayName": "Planning"}]})
        return original(request)

    graph.handle = nested
    return graph


def test_section_group_recursion_renders_complete_breadcrumb(pipeline, nested_graph):
    graph = nested_graph
    (table,) = sync(pipeline, graph)
    assert "Project Alpha > Research > Planning" in rows(pipeline, table)[0][2]


@pytest.mark.parametrize("container_path", ["sections/s-a/pages", "sectionGroups/group/sections"])
def test_confirmed_section_or_group_deletion_reconciles_pages(
    pipeline, nested_graph, container_path
):
    graph = nested_graph
    (table,) = sync(pipeline, graph)
    graph.pages["a"] = []
    graph.errors[container_path] = (404, "20113")
    sync(pipeline, graph)
    assert rows(pipeline, table) == []
    assert any(path == "pages/p1" for path, _ in graph.requests)


@pytest.mark.parametrize("container_path", ["sections/s-a/pages", "sectionGroups/group/sections"])
def test_section_or_group_access_loss_preserves_snapshot(pipeline, nested_graph, container_path):
    graph = nested_graph
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.errors[container_path] = (403, "40003")
    sync(pipeline, graph)
    assert graph.last_stats["failed"] > 0
    assert graph.last_stats["complete"] == 0
    assert rows(pipeline, table) == before


def test_long_retry_after_stops_without_retrying_early(monkeypatch):
    import cognee_community_connector_onenote.onenote as onenote

    delays = []
    monkeypatch.setattr(onenote.time, "sleep", delays.append)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "301"}))
    )
    with client, pytest.raises(OneNoteError):
        list_notebooks("offline-token", http_client=client)
    assert delays == []


def test_body_checkpoint_survives_interrupted_run_and_pipeline_reload(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.pages["a"] += [Graph.page("p2"), Graph.page("p3")]
    graph.html.update(p2="<p>Checkpoint body</p>", p3="<p>Recovered body</p>")
    graph.errors["pages/p3/content"] = (403, "40003")
    sync(pipeline, graph)
    assert graph.last_stats["complete"] == 0
    assert graph.last_stats["failed"] > 0
    assert rows(pipeline, table) == before
    calls = graph.content_requests()
    assert calls["pages/p2/content"] == 1
    # Read the durable DLT state in a fresh interpreter, not the extraction context.
    persisted = subprocess.run(
        [
            sys.executable,
            "-c",
            "import dlt,json,sys; "
            "p=dlt.attach(sys.argv[1], pipelines_dir=sys.argv[2]); "
            "print(json.dumps(p.state, default=str))",
            pipeline.pipeline_name,
            pipeline.pipelines_dir,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Checkpoint body" in json.dumps(json.loads(persisted.stdout))
    del graph.errors["pages/p3/content"]
    restored = dlt.attach(pipeline.pipeline_name, pipelines_dir=pipeline.pipelines_dir)
    restored.destination = pipeline.destination
    sync(restored, graph)
    assert graph.last_stats["complete"] == 1
    assert len(rows(restored, table)) == 3
    assert graph.content_requests()["pages/p2/content"] == 1
    assert graph.content_requests()["pages/p3/content"] == calls["pages/p3/content"] + 1


def test_check_active_exception_propagates_without_requests(pipeline):
    graph = Graph()

    def cancelled():
        raise RuntimeError("connection revoked")

    with pytest.raises(PipelineStepFailed, match="connection revoked"):
        sync(pipeline, graph, check_active=cancelled)
    assert graph.requests == []


def test_replace_override_rejected_before_requests_and_preserves_rows(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    requests = len(graph.requests)
    with graph.client() as client:
        resource = onenote_source(
            "offline-token", notebook_id="a", account_id="tenant:user", http_client=client
        )
        with pytest.raises((PipelineStepFailed, ValueError), match="merge"):
            pipeline.run(resource, write_disposition="replace")
    assert len(graph.requests) == requests
    assert rows(pipeline, table) == before


@pytest.mark.parametrize("page_code", ["20102", "20113"])
def test_deleted_parent_accepts_confirmed_missing_page(pipeline, page_code):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    graph.pages["a"] = []
    del graph.notebooks["a"]
    graph.errors["notebooks/a"] = (404, "20113")
    graph.gone_code = page_code
    sync(pipeline, graph)
    assert rows(pipeline, table) == []
    assert graph.last_stats["deleted"] == 1
    assert any(path == "notebooks/a" for path, _ in graph.requests)
    assert any(path == "pages/p1" for path, _ in graph.requests)


def test_connection_resource_name_cannot_be_reused_for_other_notebook(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph, resource_name="stable_connection")
    before = rows(pipeline, table)
    with pytest.raises(PipelineStepFailed, match="(?i)(identity|connection|notebook|scope)"):
        sync(pipeline, graph, selection=("b",), resource_name="stable_connection")
    assert rows(pipeline, table) == before


def test_explicit_ordering_survives_fallback_pagination(pipeline):
    graph = Graph()
    graph.pages["a"] = [Graph.page(f"p{i}") for i in range(100)]
    graph.html.update({f"p{i}": "<p>Body</p>" for i in range(100)})
    sync(pipeline, graph)
    page_calls = [params for path, params in graph.requests if path == "sections/s-a/pages"]
    assert len(page_calls) == 2
    assert page_calls == [
        {"$orderby": "createdDateTime asc", "$top": "100", "$skip": "0"},
        {"$orderby": "createdDateTime asc", "$top": "100", "$skip": "100"},
    ]


def test_cumulative_retry_wait_budget_never_retries_early(monkeypatch):
    from cognee_community_connector_onenote import onenote

    delays, requests = [], []
    monkeypatch.setattr(onenote.time, "sleep", delays.append)

    def response(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "40"})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(OneNoteError):
            list_notebooks("offline-token", http_client=client)
    assert delays == [40]
    assert len(requests) == 2


def test_valid_nesting_beyond_thirty_groups(pipeline):
    graph = Graph()
    original = graph.handle

    def nested(request):
        path = request.url.path.removeprefix("/v1.0/me/onenote/")
        if path == "notebooks/a/sections":
            return httpx.Response(200, json={"value": []})
        if path == "notebooks/a/sectionGroups":
            return httpx.Response(200, json={"value": [{"id": "g0", "displayName": "Level 0"}]})
        if path.startswith("sectionGroups/g"):
            group, collection = path.split("/")[1:]
            level = int(group[1:])
            values = []
            if collection == "sectionGroups" and level < 35:
                values = [{"id": f"g{level + 1}", "displayName": f"Level {level + 1}"}]
            if collection == "sections" and level == 35:
                values = [{"id": "s-a", "displayName": "Deep planning"}]
            return httpx.Response(200, json={"value": values})
        return original(request)

    graph.handle = nested
    (table,) = sync(pipeline, graph)
    assert graph.last_stats["complete"] == 1
    assert "Level 35 > Deep planning" in rows(pipeline, table)[0][2]


def test_duplicate_after_collection_changes_aborts_without_partial_rows(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.pages["a"] = [Graph.page(f"p{i}") for i in range(100)]
    original = graph.handle

    def changed_batch(request):
        if (
            request.url.path.endswith("/sections/s-a/pages")
            and request.url.params.get("$skip") == "100"
        ):
            return httpx.Response(200, json={"value": [Graph.page("p0")]})
        return original(request)

    graph.handle = changed_batch
    sync(pipeline, graph)
    assert graph.last_stats["failed"] > 0
    assert graph.last_stats["complete"] == 0
    assert rows(pipeline, table) == before


def test_account_notebook_identity_tuple_is_unambiguous():
    first = onenote_source("offline-token", account_id="a:b", notebook_id="c")
    second = onenote_source("offline-token", account_id="a", notebook_id="b:c")
    assert first.name != second.name


def test_check_active_provider_shaped_error_still_propagates(pipeline):
    graph = Graph()

    def cancelled():
        if graph.requests:
            raise OneNoteError("connection revoked", status=404, code="20113")

    with pytest.raises(PipelineStepFailed, match="connection revoked"):
        sync(pipeline, graph, check_active=cancelled)
    assert len(graph.requests) == 1


def resource_cache(pipeline, table):
    for source in pipeline.state["sources"].values():
        resource = source.get("resources", {}).get(table)
        if resource is not None:
            return resource["onenote_cache"]
    raise AssertionError(f"Missing persisted resource state for {table}")


def deletion_graph(count=12):
    graph = Graph()
    graph.pages["a"] = [Graph.page(f"deleted-{index:02d}") for index in range(count)]
    graph.html.update({page["id"]: f"<p>Body {page['id']}</p>" for page in graph.pages["a"]})
    return graph


def test_bulk_deletion_confirmation_resumes_across_fresh_process(pipeline, tmp_path):
    graph = deletion_graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.pages["a"] = []
    graph.requests.clear()
    sync(pipeline, graph, max_requests=8)
    assert graph.last_stats["complete"] == 0
    assert graph.last_stats["failed_budget"] == 1
    assert rows(pipeline, table) == before
    first_confirmed = resource_cache(pipeline, table)["confirmed_missing"]
    assert len(first_confirmed) == 4
    assert set(first_confirmed.values()) == {True}
    first_checks = [path for path, _ in graph.requests if path.startswith("pages/")]

    # Execute the next extraction in a genuinely fresh interpreter and Graph fixture.
    script = """
import dlt, json, sys
from runpy import run_path
ns = run_path(sys.argv[1])
pipeline = dlt.attach(sys.argv[2], pipelines_dir=sys.argv[3])
pipeline.destination = dlt.destinations.sqlalchemy(sys.argv[4])
graph = ns['Graph']()
graph.pages['a'] = []
ns['sync'](pipeline, graph, max_requests=8)
result = {'stats': graph.last_stats, 'cache': ns['resource_cache'](pipeline, sys.argv[5]),
          'requests': graph.requests}
print('RESUMED=' + json.dumps(result, default=str))
"""
    resumed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(Path(__file__).resolve()),
            pipeline.pipeline_name,
            pipeline.pipelines_dir,
            f"sqlite:///{tmp_path}/data.db",
            table,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(
        next(
            line.removeprefix("RESUMED=")
            for line in resumed.stdout.splitlines()
            if line.startswith("RESUMED=")
        )
    )
    assert result["stats"]["complete"] == 0
    assert result["stats"]["failed_budget"] == 1
    assert len(result["cache"]["confirmed_missing"]) == 8
    assert set(first_confirmed).issubset(result["cache"]["confirmed_missing"])
    second_checks = [path for path, _ in result["requests"] if path.startswith("pages/")]

    restored = dlt.attach(pipeline.pipeline_name, pipelines_dir=pipeline.pipelines_dir)
    restored.destination = pipeline.destination
    fresh_graph = Graph()
    fresh_graph.pages["a"] = []
    sync(restored, fresh_graph, max_requests=8)
    assert fresh_graph.last_stats["complete"] == 1
    assert fresh_graph.last_stats["deleted"] == 12
    assert fresh_graph.last_stats["requests"] == 8
    assert rows(restored, table) == []
    assert resource_cache(restored, table)["page_ids"] == []
    assert not resource_cache(restored, table).get("confirmed_missing")
    all_checks = (
        first_checks
        + second_checks
        + [path for path, _ in fresh_graph.requests if path.startswith("pages/")]
    )
    assert len(all_checks) == len(set(all_checks)) == 12


@pytest.mark.parametrize("status,code", [(403, "40003"), (404, "unknown")])
def test_uncertain_deletion_retains_prior_confirmations_and_documents(pipeline, status, code):
    graph = deletion_graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.pages["a"] = []
    sync(pipeline, graph, max_requests=8)
    confirmed = deepcopy(resource_cache(pipeline, table)["confirmed_missing"])
    unresolved = sorted(set(page_id for page_id, _, _ in before) - set(confirmed))
    assert len(confirmed) == 4
    graph.errors[f"pages/{unresolved[0]}"] = (status, code)
    sync(pipeline, graph, max_requests=8)
    assert graph.last_stats["complete"] == 0
    assert graph.last_stats["failed"] == 1
    assert rows(pipeline, table) == before
    assert resource_cache(pipeline, table)["confirmed_missing"] == confirmed
    del graph.errors[f"pages/{unresolved[0]}"]
    for _ in range(2):
        sync(pipeline, graph, max_requests=8)
    assert graph.last_stats["complete"] == 1
    assert rows(pipeline, table) == []


def test_reappearance_invalidates_prior_confirmation_before_later_deletion(pipeline):
    graph = deletion_graph()
    original_pages = deepcopy(graph.pages["a"])
    (table,) = sync(pipeline, graph)
    graph.pages["a"] = []
    sync(pipeline, graph, max_requests=8)
    confirmed = resource_cache(pipeline, table)["confirmed_missing"]
    restored_id = sorted(confirmed)[0]
    graph.pages["a"] = [next(page for page in original_pages if page["id"] == restored_id)]
    sync(pipeline, graph, max_requests=8)
    assert graph.last_stats["complete"] == 0
    assert restored_id not in resource_cache(pipeline, table)["confirmed_missing"]
    assert len(rows(pipeline, table)) == 12
    graph.pages["a"] = []
    graph.errors[f"pages/{restored_id}"] = (403, "40003")
    sync(pipeline, graph, max_requests=8)
    assert graph.last_stats["complete"] == 0
    assert restored_id not in resource_cache(pipeline, table)["confirmed_missing"]
    assert len(rows(pipeline, table)) == 12
    del graph.errors[f"pages/{restored_id}"]
    sync(pipeline, graph, max_requests=8)
    assert graph.last_stats["complete"] == 0
    assert len(rows(pipeline, table)) == 12
    sync(pipeline, graph, max_requests=8)
    assert graph.last_stats["complete"] == 1
    assert rows(pipeline, table) == []
    assert sum(path == f"pages/{restored_id}" for path, _ in graph.requests) >= 3


def test_creation_order_pagination_is_stable_when_an_earlier_page_is_edited(pipeline):
    graph = Graph()
    graph.pages["a"] = [Graph.page(f"p{index:03d}") for index in range(101)]
    for index, page in enumerate(graph.pages["a"]):
        page["createdDateTime"] = f"2026-01-{index // 24 + 1:02d}T{index % 24:02d}:00:00Z"
        graph.html[page["id"]] = f"<p>Body {index}</p>"
    original = graph.handle

    def edit_between_batches(request):
        if (
            request.url.path.endswith("/sections/s-a/pages")
            and request.url.params.get("$skip") == "100"
        ):
            graph.pages["a"][40]["lastModifiedDateTime"] = "2030-01-01T00:00:00Z"
        return original(request)

    graph.handle = edit_between_batches
    (table,) = sync(pipeline, graph)
    assert graph.last_stats["complete"] == 1
    assert {row[0] for row in rows(pipeline, table)} == {page["id"] for page in graph.pages["a"]}
    page_calls = [params for path, params in graph.requests if path == "sections/s-a/pages"]
    assert [params["$skip"] for params in page_calls] == ["0", "100"]
    assert all(params["$orderby"] == "createdDateTime asc" for params in page_calls)


def test_persisted_cache_stores_unique_body_once_after_success_and_interruption(pipeline):
    graph = Graph()
    graph.html["p1"] = "<p>UniqueBodyOne_Alpha</p>"
    (table,) = sync(pipeline, graph)
    cache = resource_cache(pipeline, table)
    assert cache["page_ids"] == ["p1"]
    assert "rows" not in cache
    assert json.dumps(cache).count("UniqueBodyOne_Alpha") == 1
    graph.pages["a"] += [Graph.page("p2"), Graph.page("p3")]
    graph.html.update(p2="<p>UniqueBodyTwo_Beta</p>", p3="<p>Eventually succeeds</p>")
    graph.errors["pages/p3/content"] = (403, "40003")
    sync(pipeline, graph)
    cache = resource_cache(pipeline, table)
    assert graph.last_stats["complete"] == 0
    assert cache["page_ids"] == ["p1"]
    assert json.dumps(cache).count("UniqueBodyOne_Alpha") == 1
    assert json.dumps(cache).count("UniqueBodyTwo_Beta") == 1
    assert len(rows(pipeline, table)) == 1


def test_legacy_rendered_row_cache_converts_without_refetching(pipeline, monkeypatch):
    graph = Graph()
    graph.html["p1"] = "<p>UniqueLegacyBody</p>"
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    calls = graph.content_requests()
    original_state = dlt.current.resource_state

    def legacy_state(*args, **kwargs):
        state = original_state(*args, **kwargs)
        cache = state["onenote_cache"]
        cache.pop("page_ids", None)
        cache["rows"] = {
            row[0]: {
                "id": row[0],
                "title": row[1],
                "content": row[2],
                "url": "https://onenote.com/p1",
            }
            for row in before
        }
        return state

    monkeypatch.setattr(dlt.current, "resource_state", legacy_state)
    sync(pipeline, graph)
    assert graph.last_stats["complete"] == 1
    assert rows(pipeline, table) == before
    assert graph.content_requests() == calls
    cache = resource_cache(pipeline, table)
    assert cache["page_ids"] == ["p1"]
    assert "rows" not in cache
    assert json.dumps(cache).count("UniqueLegacyBody") == 1


def test_malformed_page_links_checkpoint_bodies_and_recover_after_attach(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.pages["a"][0]["lastModifiedDateTime"] = "2026-10-03T00:00:00Z"
    graph.html["p1"] = "<p>Healthy edit held until full recovery</p>"
    graph.pages["a"].append(Graph.page("p2"))
    graph.pages["a"][1]["links"] = []
    graph.html["p2"] = "<p>Malformed metadata body checkpoint</p>"
    sync(pipeline, graph)
    assert graph.last_stats["complete"] == 0
    assert rows(pipeline, table) == before
    calls = graph.content_requests()
    assert calls["pages/p2/content"] == 1
    assert (
        resource_cache(pipeline, table)["bodies"]["p2"]["text"]
        == "Malformed metadata body checkpoint"
    )
    restored = dlt.attach(pipeline.pipeline_name, pipelines_dir=pipeline.pipelines_dir)
    restored.destination = pipeline.destination
    graph.pages["a"][1]["links"] = {"oneNoteWebUrl": {"href": "https://onenote.com/p2"}}
    sync(restored, graph)
    assert graph.last_stats["complete"] == 1
    assert graph.content_requests() == calls
    final_content = "\n".join(row[2] for row in rows(restored, table))
    assert "Healthy edit held until full recovery" in final_content
    assert "Malformed metadata body checkpoint" in final_content


@pytest.mark.parametrize("name", ["", "   ", 0, False, [], {}])
def test_explicit_invalid_connection_names_rejected_before_requests(name):
    graph = Graph()
    with graph.client() as client:
        with pytest.raises((TypeError, ValueError), match="(?i)(name|connection)"):
            onenote_source(
                "offline-token",
                notebook_id="a",
                account_id="tenant:user",
                resource_name=name,
                http_client=client,
            )
    assert graph.requests == []


def test_normalized_connection_names_collide_before_http():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR

    graph = Graph()
    with graph.client() as client:
        first = onenote_source(
            "offline-token",
            notebook_id="a",
            account_id="tenant:user",
            resource_name="Onenote_A",
            http_client=client,
        )
        second = onenote_source(
            "offline-token",
            notebook_id="b",
            account_id="tenant:user",
            resource_name="onenote_a",
            http_client=client,
        )
        assert first.name == second.name == "onenote_a"
        assert (
            getattr(first, PIPELINE_SCOPE_ATTR)
            == getattr(second, PIPELINE_SCOPE_ATTR)
            == "onenote_a"
        )

        @dlt.source(name="duplicate_connections")
        def duplicate_connections():
            return [first, second]

        with pytest.raises(InvalidResourceDataTypeMultiplePipes):
            duplicate_connections()
    assert graph.requests == []


def test_normalized_connection_alias_cannot_rebind_state(pipeline):
    graph = Graph()
    (table,) = sync(pipeline, graph, resource_name="Onenote_A")
    assert table == "onenote_a"
    before = rows(pipeline, table)
    calls = len(graph.requests)
    with pytest.raises(PipelineStepFailed, match="(?i)(identity|connection|notebook|scope)"):
        sync(pipeline, graph, selection=("b",), resource_name="onenote_a")
    assert rows(pipeline, table) == before
    assert len(graph.requests) == calls


@pytest.mark.parametrize("version", [None, 0, 1, 2, 3])
def test_document_sync_version_guard_accepts_only_supported_versions(monkeypatch, version):
    from cognee.tasks.ingestion import dlt_utils

    if version is None:
        monkeypatch.delattr(dlt_utils, "DOCUMENT_SYNC_VERSION", raising=False)
    else:
        monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", version)
    if version is None or version == 0:
        with pytest.raises(RuntimeError, match="(?i)(document|sync|Cognee)"):
            onenote_source("offline-token", notebook_id="a", account_id="tenant:user")
    else:
        assert onenote_source("offline-token", notebook_id="a", account_id="tenant:user").name


@pytest.mark.parametrize("stage", ["request", "retry", "publication"])
@pytest.mark.parametrize("provider_shaped", [False, True])
def test_check_active_failure_wrapped_outside_provider_errors(
    pipeline, monkeypatch, stage, provider_shaped
):
    from cognee_community_connector_onenote import onenote

    graph = Graph()
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.requests.clear()
    graph.pages["a"] = []
    failure = (
        OneNoteError("connection revoked", status=404, code="20113")
        if provider_shaped
        else ValueError("connection revoked")
    )
    attempts = []
    callback_calls = []
    original = graph.handle
    monkeypatch.setattr(onenote.time, "sleep", lambda _: None)

    def handler(request):
        attempts.append(request)
        if stage == "retry":
            return httpx.Response(429, headers={"Retry-After": "0"})
        return original(request)

    graph.handle = handler

    def check_active():
        callback_calls.append(None)
        if (
            stage == "request"
            or (stage == "retry" and attempts)
            or (stage == "publication" and any(path == "pages/p1" for path, _ in graph.requests))
        ):
            raise failure

    with graph.client() as client:
        resource = onenote_source(
            "offline-token",
            notebook_id="a",
            account_id="tenant:user",
            http_client=client,
            check_active=check_active,
        )
        with pytest.raises(PipelineStepFailed) as captured:
            pipeline.run(resource, write_disposition="merge")
    wrapped = captured.value
    while wrapped.__cause__ is not failure:
        wrapped = wrapped.__cause__
        assert wrapped is not None, "Callback exception lost its original cause"
    assert isinstance(wrapped, RuntimeError)
    assert not isinstance(wrapped, OneNoteError)
    assert "connection revoked" in str(wrapped)
    assert rows(pipeline, table) == before
    assert len(attempts) == {"request": 0, "retry": 1, "publication": 5}[stage]
    assert callback_calls


@pytest.mark.parametrize("provider_shaped", [False, True])
def test_token_provider_failure_is_wrapped_and_preserves_cause(pipeline, provider_shaped):
    graph = Graph()
    failure = (
        OneNoteError("token refresh revoked", status=404, code="20113")
        if provider_shaped
        else ValueError("token refresh revoked")
    )

    def token():
        raise failure

    with graph.client() as client:
        resource = onenote_source(
            token, notebook_id="a", account_id="tenant:user", http_client=client
        )
        with pytest.raises(PipelineStepFailed) as captured:
            pipeline.run(resource, write_disposition="merge")
    wrapped = captured.value
    while wrapped.__cause__ is not failure:
        wrapped = wrapped.__cause__
        assert wrapped is not None, "Token-provider exception lost its original cause"
    assert isinstance(wrapped, RuntimeError)
    assert not isinstance(wrapped, OneNoteError)
    assert graph.requests == []


@pytest.mark.parametrize("failure_kind", ["malformed_links", "pagination"])
def test_observed_page_invalidates_removal_confirmation_during_incomplete_scan(
    pipeline, failure_kind
):
    graph = deletion_graph()
    original_pages = deepcopy(graph.pages["a"])
    (table,) = sync(pipeline, graph)
    before = rows(pipeline, table)
    graph.pages["a"] = []
    sync(pipeline, graph, max_requests=8)
    confirmed = deepcopy(resource_cache(pipeline, table)["confirmed_missing"])
    assert len(confirmed) == 4
    restored_id = sorted(confirmed)[0]
    restored_page = next(page for page in original_pages if page["id"] == restored_id)
    original_handle = graph.handle
    if failure_kind == "malformed_links":
        malformed_page = Graph.page("zz-malformed")
        malformed_page["links"] = []
        graph.html["zz-malformed"] = "<p>Incomplete scan body</p>"
        graph.pages["a"] = [restored_page, malformed_page]
    else:
        graph.pages["a"] = [restored_page] + [
            Graph.page(f"zz-page-{index:03d}") for index in range(99)
        ]

        def failed_later_batch(request):
            if (
                request.url.path.endswith("/sections/s-a/pages")
                and request.url.params.get("$skip") == "100"
            ):
                graph.requests.append(("sections/s-a/pages", dict(request.url.params)))
                return httpx.Response(403, json={"error": {"code": "40003"}})
            return original_handle(request)

        graph.handle = failed_later_batch

    sync(pipeline, graph, max_requests=8)
    assert graph.last_stats["complete"] == 0
    assert graph.last_stats["failed"] == 1
    assert rows(pipeline, table) == before
    if failure_kind == "malformed_links":
        assert "zz-malformed" in resource_cache(pipeline, table)["bodies"]
    else:
        assert any(
            path == "sections/s-a/pages" and params.get("$skip") == "100"
            for path, params in graph.requests
        )
    # A positive observation invalidates old deletion evidence immediately, even
    # though later metadata or pagination prevents the traversal from completing.
    assert restored_id not in resource_cache(pipeline, table)["confirmed_missing"]
    assert set(resource_cache(pipeline, table)["confirmed_missing"]) == set(confirmed) - {
        restored_id
    }

    restored = dlt.attach(pipeline.pipeline_name, pipelines_dir=pipeline.pipelines_dir)
    restored.destination = pipeline.destination
    graph.handle = original_handle
    graph.pages["a"] = []
    graph.errors[f"pages/{restored_id}"] = (403, "40003")
    graph.requests.clear()
    sync(restored, graph, max_requests=8)
    assert any(path == f"pages/{restored_id}" for path, _ in graph.requests)
    assert graph.last_stats["complete"] == 0
    assert graph.last_stats["failed"] == 1
    assert rows(restored, table) == before
    assert restored_id not in resource_cache(restored, table)["confirmed_missing"]
