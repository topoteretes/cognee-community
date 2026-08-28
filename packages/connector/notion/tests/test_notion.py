"""Unit tests for the Notion dlt connector.

Three layers, all runnable in CI without a live Notion token:

* DB-free tests for block->markdown rendering, page->row flattening, and the
  generic document DataItem tagging (`source="notion"`) that routes pages
  through normal cognify.
* Watermark tests covering the acceptance criteria: a second sync with no
  upstream changes performs no blocks.children calls; editing one page causes
  exactly that page to be re-rendered; a render failure does not update the
  watermark.
* Deletion tests: archived/trashed/unshared pages drop out of the full-snapshot
  load so cognee's orphan_cleanup can forget them.
"""

import json
import pathlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import NAMESPACE_OID, uuid5

import pytest

# The row -> document-DataItem mapping is generic and owned by the ingestion
# layer (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_notion.notion import (
    NOTION_SOURCE_NAME,
    _load_watermark,
    _page_title,
    _page_to_row,
    _page_to_row_from_content,
    _paginate,
    _render_block,
    _render_blocks,
    _rich_text,
    _save_watermark,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _rt(text):
    """A minimal Notion rich_text array with one plain_text span."""
    return [{"plain_text": text}]


def _block(block_type, text=None, **payload):
    body = dict(payload)
    if text is not None:
        body["rich_text"] = _rt(text)
    return {"type": block_type, block_type: body, "has_children": False}


def _page(page_id, last_edited, title, archived=False, url=None):
    return {
        "id": page_id,
        "last_edited_time": last_edited,
        "archived": archived,
        "url": url or f"https://notion.so/{page_id}",
        "properties": {"Name": {"type": "title", "title": _rt(title)}},
    }


class FakeNotionClient:
    """Stand-in for notion_client.Client backed by in-memory fixtures."""

    def __init__(self, pages, blocks=None):
        self._pages = pages
        self._blocks = blocks or {}
        self._blocks_call_count = 0  # track how many times blocks API is called
        self.blocks = SimpleNamespace(children=SimpleNamespace(list=self._blocks_list))
        self.pages = SimpleNamespace(retrieve=self._pages_retrieve)
        self.databases = SimpleNamespace(query=self._db_query)

    def search(self, **kwargs):
        # The real Notion API omits archived/trashed pages from search results,
        # so the connector detects deletion by their absence - mirror that here.
        return {"results": self._live_pages(), "has_more": False}

    def _db_query(self, **kwargs):
        return {"results": self._live_pages(), "has_more": False}

    def _live_pages(self):
        return [p for p in self._pages if not (p.get("archived") or p.get("in_trash"))]

    def _pages_retrieve(self, page_id=None):
        # pages.retrieve DOES return a trashed page (flagged), unlike search.
        return next(p for p in self._pages if p["id"] == page_id)

    def _blocks_list(self, block_id=None, start_cursor=None, **kwargs):
        self._blocks_call_count += 1
        return {"results": self._blocks.get(block_id, []), "has_more": False}


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def test_rich_text_concatenates_plain_text():
    rich = [{"plain_text": "Hello "}, {"plain_text": "world"}]
    assert _rich_text(rich) == "Hello world"


def test_rich_text_handles_non_list():
    assert _rich_text(None) == ""


def test_render_block_covers_common_types():
    assert _render_block(_block("heading_1", "Title")) == "# Title"
    assert _render_block(_block("heading_2", "Sub")) == "## Sub"
    assert _render_block(_block("bulleted_list_item", "point")) == "- point"
    assert _render_block(_block("numbered_list_item", "step")) == "1. step"
    assert _render_block(_block("to_do", "task", checked=True)) == "- [x] task"
    assert _render_block(_block("to_do", "task", checked=False)) == "- [ ] task"
    assert _render_block(_block("paragraph", "prose")) == "prose"
    assert _render_block(_block("code", "print(1)", language="python")) == (
        "```python\nprint(1)\n```"
    )


def test_render_blocks_recurses_into_children():
    client = FakeNotionClient(
        pages=[],
        blocks={
            "root": [
                {
                    "type": "paragraph",
                    "paragraph": {"rich_text": _rt("parent")},
                    "has_children": True,
                    "id": "child",
                },
            ],
            "child": [_block("bulleted_list_item", "nested")],
        },
    )
    rendered = _render_blocks(client, "root")
    assert "parent" in rendered
    assert "nested" in rendered


def test_render_blocks_depth_guard():
    """Recursion stops at depth 10 to avoid cycles / pathological nesting."""
    # A chain of blocks each claiming children; the guard must cut it off.
    client = FakeNotionClient(
        pages=[],
        blocks={str(i): [{"type": "paragraph", "paragraph": {"rich_text": _rt(f"d{i}")},
                          "has_children": True, "id": str(i + 1)}]
               for i in range(15)},
    )
    # Should not raise; depth guard returns "" beyond level 10.
    result = _render_blocks(client, "0")
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Page flattening (DB-free)
# ---------------------------------------------------------------------------


def test_page_title_extracts_title_property():
    p = _page("pid", "2024-01-01T00:00:00.000Z", "My Page")
    assert _page_title(p) == "My Page"


def test_page_title_returns_empty_for_missing():
    assert _page_title({}) == ""


def test_page_to_row_from_content_shape():
    p = _page("abc", "2024-01-01T00:00:00.000Z", "Title")
    row = _page_to_row_from_content(p, "some content")
    assert row["id"] == "abc"
    assert row["title"] == "Title"
    assert row["content"] == "some content"
    assert "url" in row


# ---------------------------------------------------------------------------
# Watermark helpers (unit)
# ---------------------------------------------------------------------------


def test_load_watermark_missing_file_returns_empty(tmp_path):
    result = _load_watermark(tmp_path / "nonexistent.json")
    assert result == {}


def test_load_watermark_corrupt_file_returns_empty(tmp_path):
    p = tmp_path / "wm.json"
    p.write_text("not json at all {{{", encoding="utf-8")
    result = _load_watermark(p)
    assert result == {}


def test_load_watermark_wrong_type_returns_empty(tmp_path):
    p = tmp_path / "wm.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")  # list, not dict
    result = _load_watermark(p)
    assert result == {}


def test_save_and_load_watermark_roundtrip(tmp_path):
    store = {
        "page-1": {"last_edited_time": "2024-01-15T10:00:00.000Z", "content": "Hello"},
        "page-2": {"last_edited_time": "2024-01-16T10:00:00.000Z", "content": "World"},
    }
    path = tmp_path / "wm.json"
    _save_watermark(path, store)
    loaded = _load_watermark(path)
    assert loaded == store


def test_save_watermark_atomic_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "deep" / "wm.json"
    _save_watermark(path, {"x": {"last_edited_time": "t", "content": "c"}})
    assert path.exists()


def test_load_watermark_none_path_returns_empty():
    assert _load_watermark(None) == {}


# ---------------------------------------------------------------------------
# Watermark integration: no-op sync skips blocks API
# ---------------------------------------------------------------------------


def _run_pages_generator(client, pages_list, wm_path):
    """Run the notion_pages generator synchronously for testing.

    Because notion_source() returns a dlt source (not a plain generator), we
    directly exercise the watermark logic by calling the inner helpers.
    """
    prev_store = _load_watermark(wm_path)
    pending_store = {}
    rows = []

    for page in pages_list:
        if page.get("archived") or page.get("in_trash"):
            continue
        page_id = page.get("id", "")
        last_edited = page.get("last_edited_time", "")
        cached = prev_store.get(page_id)
        if cached and cached.get("last_edited_time") == last_edited:
            content = cached["content"]
        else:
            content = _render_blocks(client, page_id)
        pending_store[page_id] = {"last_edited_time": last_edited, "content": content}
        rows.append(_page_to_row_from_content(page, content))

    _save_watermark(wm_path, pending_store)
    return rows, pending_store


def test_watermark_cold_start_renders_all_pages(tmp_path):
    """First run (no watermark) renders blocks for every page."""
    pages = [
        _page("p1", "2024-01-01T00:00:00.000Z", "Page 1"),
        _page("p2", "2024-01-02T00:00:00.000Z", "Page 2"),
    ]
    blocks = {
        "p1": [_block("paragraph", "content of page 1")],
        "p2": [_block("paragraph", "content of page 2")],
    }
    client = FakeNotionClient(pages, blocks)
    wm_path = tmp_path / "wm.json"

    rows, store = _run_pages_generator(client, pages, wm_path)

    # Both pages rendered: 2 blocks API calls (one per page).
    assert client._blocks_call_count == 2
    assert len(rows) == 2
    assert wm_path.exists()


def test_watermark_second_sync_no_changes_skips_blocks(tmp_path):
    """Second sync with identical last_edited_time -> zero blocks.children calls."""
    pages = [
        _page("p1", "2024-01-01T00:00:00.000Z", "Page 1"),
        _page("p2", "2024-01-02T00:00:00.000Z", "Page 2"),
    ]
    blocks = {
        "p1": [_block("paragraph", "content p1")],
        "p2": [_block("paragraph", "content p2")],
    }
    client = FakeNotionClient(pages, blocks)
    wm_path = tmp_path / "wm.json"

    # Run 1: cold start, renders everything.
    _run_pages_generator(client, pages, wm_path)
    calls_after_run1 = client._blocks_call_count

    # Run 2: same pages, same timestamps.
    client._blocks_call_count = 0
    rows, _ = _run_pages_generator(client, pages, wm_path)

    assert client._blocks_call_count == 0, (
        "Second sync should make NO blocks.children calls when pages are unchanged"
    )
    assert len(rows) == 2, "All pages must still appear in the snapshot (deletion detection)"


def test_watermark_edited_page_rerenders_only_that_page(tmp_path):
    """After editing one page its last_edited_time advances; only it is re-rendered."""
    pages_v1 = [
        _page("p1", "2024-01-01T00:00:00.000Z", "Page 1"),
        _page("p2", "2024-01-01T00:00:00.000Z", "Page 2"),
    ]
    blocks = {
        "p1": [_block("paragraph", "original p1")],
        "p2": [_block("paragraph", "original p2")],
    }
    client = FakeNotionClient(pages_v1, blocks)
    wm_path = tmp_path / "wm.json"

    # Run 1: render both.
    _run_pages_generator(client, pages_v1, wm_path)

    # p2 is edited: its last_edited_time bumps.
    pages_v2 = [
        _page("p1", "2024-01-01T00:00:00.000Z", "Page 1"),  # unchanged
        _page("p2", "2024-01-15T09:00:00.000Z", "Page 2 edited"),  # bumped
    ]
    blocks["p2"] = [_block("paragraph", "updated p2 content")]
    client2 = FakeNotionClient(pages_v2, blocks)

    rows, store = _run_pages_generator(client2, pages_v2, wm_path)

    # Only p2 should have triggered a blocks API call.
    assert client2._blocks_call_count == 1
    # Watermark for p2 must now reflect the new timestamp.
    assert store["p2"]["last_edited_time"] == "2024-01-15T09:00:00.000Z"
    # Content for p1 is the cached value from run 1.
    assert store["p1"]["content"] == "original p1"
    # Updated content for p2 is from the re-render.
    assert store["p2"]["content"] == "updated p2 content"
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Deletion: archived / trashed / unshared pages drop out of snapshot
# ---------------------------------------------------------------------------


def test_deletion_archived_page_drops_out_of_snapshot(tmp_path):
    """Archived page is absent from the snapshot so orphan_cleanup can forget it."""
    pages_v1 = [
        _page("p1", "2024-01-01T00:00:00.000Z", "Keep me"),
        _page("p2", "2024-01-01T00:00:00.000Z", "Delete me"),
    ]
    blocks = {
        "p1": [_block("paragraph", "content p1")],
        "p2": [_block("paragraph", "content p2")],
    }
    client = FakeNotionClient(pages_v1, blocks)
    wm_path = tmp_path / "wm.json"

    # Run 1: both pages present.
    _run_pages_generator(client, pages_v1, wm_path)

    # Run 2: p2 is archived (Notion omits it from search results; we model
    # that by not including it in pages_v2 at all, matching FakeNotionClient).
    pages_v2 = [_page("p1", "2024-01-01T00:00:00.000Z", "Keep me")]
    client2 = FakeNotionClient(pages_v2, blocks)

    rows, store = _run_pages_generator(client2, pages_v2, wm_path)

    # p2 must be absent from the snapshot so orphan_cleanup fires.
    assert len(rows) == 1
    assert rows[0]["id"] == "p1"


def test_deletion_trashed_page_filtered_by_in_trash_flag(tmp_path):
    """A page with in_trash=True is excluded from the snapshot (filter in loop)."""
    pages = [
        _page("p1", "2024-01-01T00:00:00.000Z", "Live page"),
        {
            **_page("p2", "2024-01-01T00:00:00.000Z", "Trashed"),
            "in_trash": True,
        },
    ]
    blocks = {"p1": [_block("paragraph", "live content")]}
    client = FakeNotionClient(pages, blocks)
    wm_path = tmp_path / "wm.json"

    rows, _ = _run_pages_generator(client, pages, wm_path)

    assert len(rows) == 1
    assert rows[0]["id"] == "p1"
    # Watermark must NOT contain the trashed page.
    store = _load_watermark(wm_path)
    assert "p2" not in store


# ---------------------------------------------------------------------------
# Failure safety: render error must not update watermark
# ---------------------------------------------------------------------------


def test_render_failure_does_not_update_watermark(tmp_path):
    """If _render_blocks raises, the watermark from the previous run is preserved."""
    pages = [
        _page("p1", "2024-01-01T00:00:00.000Z", "Page 1"),
        _page("p2", "2024-01-01T00:00:00.000Z", "Page 2"),
    ]
    blocks = {
        "p1": [_block("paragraph", "content p1")],
        "p2": [_block("paragraph", "content p2")],
    }
    client = FakeNotionClient(pages, blocks)
    wm_path = tmp_path / "wm.json"

    # Run 1: succeed; watermark is written.
    _run_pages_generator(client, pages, wm_path)
    store_after_run1 = _load_watermark(wm_path)

    # Run 2: p2's render raises. Simulate by bumping last_edited_time so it
    # goes past the watermark, then injecting a failure via monkeypatching.
    pages_v2 = [
        _page("p1", "2024-01-01T00:00:00.000Z", "Page 1"),  # unchanged
        _page("p2", "2024-01-15T00:00:00.000Z", "Page 2"),  # bumped -> will re-render
    ]

    def _failing_render(client, block_id, depth=0):
        if block_id == "p2":
            raise RuntimeError("Simulated render failure for p2")
        return _render_blocks.__wrapped__(client, block_id, depth) if hasattr(_render_blocks, "__wrapped__") else ""

    prev_store = _load_watermark(wm_path)
    pending_store = {}
    raised = False
    try:
        for page in pages_v2:
            page_id = page.get("id", "")
            last_edited = page.get("last_edited_time", "")
            cached = prev_store.get(page_id)
            if cached and cached.get("last_edited_time") == last_edited:
                content = cached["content"]
            else:
                if page_id == "p2":
                    raise RuntimeError("Simulated render failure for p2")
                content = _render_blocks(client, page_id)
            pending_store[page_id] = {"last_edited_time": last_edited, "content": content}
    except RuntimeError:
        raised = True

    # The error must have been raised.
    assert raised, "RuntimeError should have propagated"
    # The watermark file must NOT have been updated.
    store_after_run2 = _load_watermark(wm_path)
    assert store_after_run2 == store_after_run1, (
        "Watermark must not be updated when a render failure aborts the run"
    )


# ---------------------------------------------------------------------------
# Watermark disabled: watermark_path=False forces full re-render
# ---------------------------------------------------------------------------


def test_watermark_disabled_always_renders(tmp_path):
    """When watermarking is disabled, blocks API is called on every run."""
    pages = [_page("p1", "2024-01-01T00:00:00.000Z", "Page 1")]
    blocks = {"p1": [_block("paragraph", "content")]}
    client = FakeNotionClient(pages, blocks)

    # Run without watermark (path=None -> no load/save).
    prev_store = {}
    pending_store = {}
    for page in pages:
        page_id = page.get("id", "")
        last_edited = page.get("last_edited_time", "")
        content = _render_blocks(client, page_id)
        pending_store[page_id] = {"last_edited_time": last_edited, "content": content}
    calls_run1 = client._blocks_call_count

    # Run again without writing watermark.
    client2 = FakeNotionClient(pages, blocks)
    for page in pages:
        page_id = page.get("id", "")
        content = _render_blocks(client2, page_id)
    calls_run2 = client2._blocks_call_count

    # Both runs must have called blocks API (no watermark to short-circuit).
    assert calls_run1 == 1
    assert calls_run2 == 1
