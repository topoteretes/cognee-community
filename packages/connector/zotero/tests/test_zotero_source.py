"""Mock HTTP, deterministic clocks, and real dlt SQLite snapshot loads."""

import json
from types import SimpleNamespace
from uuid import uuid4

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag, is_dlt_sourced
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_zotero import zotero as z


def item(key="A", kind="journalArticle", **data):
    return {"key": key, "version": 7, "data": {"itemType": kind, "title": f"Title {key}", **data}}


class Library:
    def __init__(self):
        self.items = [item("A"), item("B"), item("C")]
        self.version = 7
        self.requests = []
        self.collections = [{"key": "COL", "data": {"name": "Research"}}]
        self.fulltext = {"content": "Indexed text", "indexedPages": 1}
        self.fulltext_status = 200
        self.failure = None
        self.total = None
        self.last_only = False

    def handle(self, request):
        self.requests.append(request)
        headers = {"Last-Modified-Version": str(self.version)}
        if request.headers.get("If-Modified-Since-Version") == str(self.version):
            return httpx.Response(304, headers=headers)
        if request.url.path.endswith("/fulltext"):
            return httpx.Response(self.fulltext_status, json=self.fulltext)
        if request.url.path.endswith("/collections"):
            headers["Total-Results"] = str(len(self.collections))
            return httpx.Response(200, json=self.collections, headers=headers)
        start = int(request.url.params.get("start", 0))
        size = int(request.url.params["limit"])
        if self.failure and start:
            return httpx.Response(self.failure)
        headers["Total-Results"] = str(len(self.items) if self.total is None else self.total)
        if start + size < len(self.items):
            next_url = request.url.copy_set_param("start", start + size)
            headers["Link"] = f'<{next_url}>; rel="next"'
        elif self.last_only:
            headers["Link"] = f'<{request.url}>; rel="last"'
        return httpx.Response(200, json=self.items[start : start + size], headers=headers)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))


def source(client, clock, **kwargs):
    return z.zotero_source("123", client=client, clock=clock, pacing_seconds=0, **kwargs)


def causes(exc):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def test_document_mode(clock):
    with Library().client() as client:
        src = source(client, clock)
        assert document_source_tag(src) == "zotero"
        rows = list(src)
        row = rows[0]
    mapped = _build_document_data_item(
        SimpleNamespace(row_data=row, content_hash="hash"), uuid4(), "zotero"
    )
    assert not is_dlt_sourced(mapped.external_metadata)
    assert mapped.external_metadata["external_id"] == row["id"]
    assert "Title A" in mapped.data


@pytest.mark.parametrize("key,env", [(None, False), ("secret", False), ("secret", True)])
def test_headers(clock, monkeypatch, key, env):
    lib = Library()
    if env:
        monkeypatch.setenv("ZOTERO_API_KEY", key)
    with lib.client() as client:
        list(source(client, clock, zotero_api_key=None if env else key))
        assert not client.is_closed
    for request in lib.requests:
        assert request.headers["Zotero-API-Version"] == "3"
        assert request.headers.get("Zotero-API-Key") == key


def test_user_requires_key(clock):
    lib = Library()
    with lib.client() as client, pytest.raises(ValueError, match="require an API key"):
        source(client, clock, library_type="user")
    assert not lib.requests


def test_user_path(clock):
    lib = Library()
    with lib.client() as client:
        rows = list(source(client, clock, library_type="user", zotero_api_key="key"))
    assert rows[0]["id"] == "zotero://user/123/items/A"
    assert all(r.url.path.startswith("/users/123/") for r in lib.requests)


@pytest.mark.parametrize("size", [0, 101, -1, 1.5, True])
def test_page_size_validation(size):
    with pytest.raises(ValueError, match="page_size"):
        z.zotero_source("123", page_size=size)


@pytest.mark.parametrize("last", [False, True])
def test_three_pages(clock, last):
    lib = Library()
    lib.last_only = last
    with lib.client() as client:
        assert len(list(source(client, clock, page_size=1))) == 3
    assert [r.url.params["start"] for r in lib.requests[:-1]] == ["0", "1", "2"]
    assert lib.requests[-1].url.path.endswith("/collections")


def test_default_page_size(clock):
    lib = Library()
    with lib.client() as client:
        list(source(client, clock))
    assert lib.requests[0].url.params["limit"] == "100"


@pytest.mark.parametrize("total", [2, 4])
def test_total_results_circuit_breaker(clock, total):
    lib = Library()
    lib.total = total
    with lib.client() as client, pytest.raises(Exception) as exc:
        list(source(client, clock))
    assert any(isinstance(e, z.ZoteroSyncError) for e in causes(exc.value))


def test_empty(clock):
    lib = Library()
    lib.items = []
    with lib.client() as client:
        assert list(source(client, clock)) == []


def test_full_fields(clock):
    lib = Library()
    lib.items = [
        item(
            creators=[{"lastName": "Lovelace", "firstName": "Ada"}, {"name": "Smith, AB"}],
            date="2024",
            DOI="10.1/example",
            url="https://example.org",
            publicationTitle="Journal",
            tags=[{"tag": "science"}],
            collections=["COL"],
            abstractNote="Abstract",
        )
    ]
    with lib.client() as client:
        rows = list(source(client, clock))
        row = rows[0]
    assert row["id"] == "zotero://group/123/items/A"
    assert row["content"].splitlines() == [
        "Title A",
        "Lovelace, Ada; Smith, AB",
        "2024",
        "Journal",
        "10.1/example",
        "https://example.org",
        "science",
        "Research",
        "Abstract",
    ]
    assert row["tags"] == "science"
    assert row["collections"] == "Research"
    assert row["doi"] == "10.1/example"
    assert row["journal"] == "Journal"
    assert row["pub_date"] == "2024"
    assert json.loads(row["creators"])[1] == {"name": "Smith, AB"}


@pytest.mark.parametrize("kind", ["book", "report", "webpage", "thesis"])
def test_item_types_without_abstract(clock, kind):
    lib = Library()
    lib.items = [item(kind=kind)]
    with lib.client() as client:
        rows = list(source(client, clock))
        row = rows[0]
    assert row["item_type"] == kind
    assert row["content"] == "Title A"


@pytest.mark.parametrize("include", [False, True])
def test_notes(clock, include):
    lib = Library()
    lib.items = [
        item(),
        item("N", "note", parentItem="A", note="<p>Hello <b>world</b> &amp; all</p>"),
    ]
    with lib.client() as client:
        rows = list(source(client, clock, include_notes=include))
    assert len(rows) == 1 + include
    if include:
        assert rows[1]["content"] == "Title A\nHello world & all"
        assert rows[1]["id"].endswith("/N")


@pytest.mark.parametrize(
    "status,enabled,mode",
    [
        (200, True, "imported_file"),
        (404, True, "imported_file"),
        (200, False, "imported_file"),
        (200, True, "linked_url"),
        (200, True, "imported_url"),
    ],
)
def test_attachments(clock, status, enabled, mode):
    lib = Library()
    lib.items = [
        item(),
        item(
            "F",
            "attachment",
            parentItem="A",
            filename="paper.pdf",
            contentType="application/pdf",
            linkMode=mode,
        ),
    ]
    lib.fulltext_status = status
    with lib.client() as client:
        rows = list(source(client, clock, include_attachment_text=enabled))
    content = rows[1]["content"]
    assert content.startswith(f"paper.pdf\napplication/pdf\n{mode}\nTitle A")
    fetched = enabled and mode != "linked_url"
    assert ("Indexed text" in content) == (fetched and status == 200)
    assert sum(r.url.path.endswith("/fulltext") for r in lib.requests) == int(fetched)


def pipe(tmp_path):
    return dlt.pipeline(
        pipeline_name="zotero_test",
        dataset_name="zotero_ds",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'z.db'}"),
        pipelines_dir=str(tmp_path / "state"),
    )


def rows(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM documents ORDER BY id") as cursor,
    ):
        return cursor.fetchall()


def watermark(pipeline):
    return pipeline.state["sources"]["zotero"]["watermarks"].copy()


@pytest.mark.parametrize("remaining", [1, 0])
def test_replace_deletions(tmp_path, clock, remaining):
    lib, pipeline = Library(), pipe(tmp_path)
    with lib.client() as client:
        pipeline.run(source(client, clock))
        before = rows(pipeline)
        lib.items = lib.items[:remaining]
        lib.version = 8
        pipeline.run(source(client, clock))
    assert rows(pipeline) == before[:remaining]
    assert list(watermark(pipeline).values()) == [8]


def test_304_leaves_staging_untouched(tmp_path, clock):
    lib, pipeline = Library(), pipe(tmp_path)
    with lib.client() as client:
        pipeline.run(source(client, clock))
        before = rows(pipeline)
        assert list(watermark(pipeline).values()) == [7]
        lib.requests.clear()
        with pytest.raises(z.ZoteroLibraryUnchanged, match=r"7.*staging untouched") as exc:
            pipeline.run(source(client, clock))
        assert exc.value.version == 7
    assert rows(pipeline) == before
    assert len(lib.requests) == 1
    probe = lib.requests[0]
    assert dict(probe.url.params) == {"limit": "1", "format": "json"}
    assert probe.headers["If-Modified-Since-Version"] == "7"
    assert list(watermark(pipeline).values()) == [7]


def test_failed_snapshot_preserves_state_and_rows(tmp_path, clock):
    lib, pipeline = Library(), pipe(tmp_path)
    with lib.client() as client:
        pipeline.run(source(client, clock))
        before, state = rows(pipeline), watermark(pipeline)
        lib.version, lib.failure = 8, 503
        with pytest.raises(Exception) as exc:
            pipeline.run(source(client, clock, page_size=1))
        assert any(isinstance(e, z.ZoteroSyncError) for e in causes(exc.value))
        assert rows(pipeline) == before
        assert watermark(pipeline) == state
        lib.failure = None
        pipeline.run(source(client, clock))
        assert list(watermark(pipeline).values()) == [8]


def test_zero_yield_on_mid_page_failure(clock):
    lib = Library()
    lib.failure = 503
    yielded = []
    with lib.client() as client, pytest.raises(Exception) as exc:
        for row in source(client, clock, page_size=1):
            yielded.append(row)
    assert not yielded
    assert any(isinstance(e, z.ZoteroSyncError) for e in causes(exc.value))


@pytest.mark.parametrize(
    "status,headers,expected", [(429, {"Retry-After": "3"}, [3]), (429, {}, [1]), (503, {}, [1])]
)
def test_transient_then_success(clock, status, headers, expected):
    responses = [httpx.Response(status, headers=headers), httpx.Response(200)]
    with httpx.Client(transport=httpx.MockTransport(lambda r: responses.pop(0))) as client:
        request = z._Requester({}, "/groups/123", 0, clock)
        assert request(client, "/groups/123/items").status_code == 200
    assert clock.sleeps == expected


@pytest.mark.parametrize(
    "status,attempts,message",
    [
        (503, 5, "request failed"),
        (429, 5, "request failed"),
        (403, 1, "API key invalid or no access to library /groups/123"),
        (404, 1, "library not found or not readable"),
    ],
)
def test_errors(clock, status, attempts, message):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(z.ZoteroSyncError, match=message),
    ):
        z._Requester({}, "/groups/123", 0, clock)(client, "/groups/123/items")
    assert len(calls) == attempts
    assert clock.sleeps == ([1, 2, 4, 8] if attempts == 5 else [])


@pytest.mark.parametrize(
    "interval,backoff,expected", [(0, None, []), (0.3, None, [0.3]), (0.3, "2", [2])]
)
def test_pacing_and_backoff(clock, interval, backoff, expected):
    headers = {"Backoff": backoff} if backoff else {}
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=headers))
    ) as client:
        request = z._Requester({}, "/groups/123", interval, clock)
        request(client, "/groups/123/items")
        request(client, "/groups/123/collections")
    assert clock.sleeps == expected


def test_transport_retry(clock):
    attempts = []

    def handle(request):
        attempts.append(request)
        if len(attempts) < 2:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        z._Requester({}, "/groups/123", 0, clock)(client, "/groups/123/items")
    assert clock.sleeps == [1]


def test_sockets_blocked():
    import socket

    with socket.socket() as sock, pytest.raises(AssertionError, match="forbidden"):
        sock.connect(("127.0.0.1", 80))


@pytest.mark.parametrize("failure", ["duplicate", "version", "collections", "malformed"])
def test_snapshot_contract_errors(clock, failure):
    lib = Library()
    if failure == "duplicate":
        lib.items[1] = lib.items[0]

    def handle(request):
        response = lib.handle(request)
        if failure == "version" and request.url.params.get("start") == "1":
            response.headers["Last-Modified-Version"] = "8"
        if failure == "collections" and request.url.path.endswith("/collections"):
            response.headers["Total-Results"] = "2"
        if failure == "malformed" and request.url.path.endswith("/items"):
            return httpx.Response(200, text="broken", headers=response.headers)
        return response

    yielded = []
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(Exception) as exc,
    ):
        for row in source(client, clock, page_size=1):
            yielded.append(row)
    assert not yielded
    assert any(isinstance(e, z.ZoteroSyncError) for e in causes(exc.value))


def test_pagination_link_cannot_leak_key(clock):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json=[item()],
            headers={
                "Total-Results": "2",
                "Last-Modified-Version": "7",
                "Link": '<https://example.org/items>; rel="next"',
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(Exception) as exc,
    ):
        list(source(client, clock, zotero_api_key="secret"))
    assert any(isinstance(e, z.ZoteroSyncError) for e in causes(exc.value))
    assert len(calls) == 1


def test_deferred_pipeline_probe(tmp_path, clock):
    lib, pipeline = Library(), pipe(tmp_path)
    with lib.client() as client:
        pipeline.run(source(client, clock))
        before = rows(pipeline)
        pipeline.deactivate()
        pending = source(client, clock)
        lib.requests.clear()
        with pytest.raises(Exception) as exc:
            pipeline.run(pending)
        unchanged = [e for e in causes(exc.value) if isinstance(e, z.ZoteroLibraryUnchanged)]
        assert len(unchanged) == 1
        assert unchanged[0].version == 7
        assert rows(pipeline) == before
        assert len(lib.requests) == 1
        assert list(watermark(pipeline).values()) == [7]


def test_switching_options_rebuilds_snapshot(tmp_path, clock):
    lib, pipeline = Library(), pipe(tmp_path)
    lib.items = [item(), item("N", "note", parentItem="A", note="Note")]
    with lib.client() as client:
        pipeline.run(source(client, clock))
        assert len(rows(pipeline)) == 2
        pipeline.run(source(client, clock, include_notes=False))
        assert len(rows(pipeline)) == 1
        pipeline.run(source(client, clock))
        assert len(rows(pipeline)) == 2


def test_absent_fulltext_content(clock):
    from pathlib import Path

    lib = Library()
    lib.items = [item("F", "attachment", filename="paper.pdf", linkMode="imported_file")]
    lib.fulltext = json.loads((Path(__file__).parent / "fixtures/empty_fulltext.json").read_text())
    with lib.client() as client:
        result = list(source(client, clock))
    assert result[0]["content"] == "paper.pdf\nimported_file"


def test_example_unwraps_unchanged(monkeypatch):
    import asyncio
    import importlib.util
    from pathlib import Path
    from unittest.mock import AsyncMock

    path = Path(__file__).parents[1] / "examples/example.py"
    spec = importlib.util.spec_from_file_location("zotero_example", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    original = z.ZoteroLibraryUnchanged(7)
    wrapper = RuntimeError("pipeline failure")
    wrapper.__cause__ = original
    monkeypatch.setattr(example, "zotero_source", lambda **kwargs: object())
    monkeypatch.setattr(example.cognee, "remember", AsyncMock(side_effect=wrapper))
    with pytest.raises(z.ZoteroLibraryUnchanged) as exc:
        asyncio.run(example.remember_library("123"))
    assert exc.value is original
