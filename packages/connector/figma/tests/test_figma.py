"""Unit tests for the Figma dlt connector.

Three layers, all runnable in CI without a Figma account:

* DB-free tests for node-tree and comment-thread rendering, and the generic
  document DataItem tagging (``source="figma"``) that routes rows through
  cognify.
* Sync tests against ``FakeFigma`` (a transport adapter on a real requests
  session, so dlt's auth, ``rest_api`` resolution and ``RESTClient`` all
  run), covering the selection, the version-gated render cache and every
  forget-on-delete path.
* dlt-pipeline tests (temp sqlite destination) proving that ``replace``
  drops deleted rows, that the render cache survives ``replace`` runs in
  source state, and that a failed run keeps both staging and the cache.
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import dlt
import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from fake_figma import TOKEN, FakeFigma, frame, text

from cognee_community_connector_figma.figma import (
    FIGMA_COMMENTS_TABLE_NAME,
    FIGMA_SOURCE_NAME,
    FIGMA_TABLE_NAME,
    comment_thread_rows,
    figma_client_config,
    figma_source,
    list_files,
    list_folders,
    render_document,
    sync_files,
)

TEAM = "team-1"
DESIGN = "folder-design"
NESTED = "folder-nested"
OTHER = "folder-other"


@pytest.fixture
def figma():
    fake = FakeFigma()
    fake.add_team(TEAM, "Acme")
    fake.add_folder(TEAM, DESIGN, "Design")
    fake.add_folder(TEAM, NESTED, "Checkout", parent=DESIGN)
    fake.add_folder(TEAM, OTHER, "Marketing")
    fake.add_file(
        DESIGN,
        "file-a",
        "Onboarding",
        {"Flows": [frame("Welcome", text("Create your account"), id="7:1")]},
    )
    fake.add_file(NESTED, "file-b", "Payments", {"Cards": [frame("Card form", text("Pay now"))]})
    fake.add_file(OTHER, "file-c", "Campaign", {"Ads": [text("Spring sale")]})
    return fake


def _config(figma, **kwargs):
    return figma_client_config(TOKEN, session=figma.session(), **kwargs)


def _sync(figma, state, team_ids=(TEAM,), folder_ids=None, file_keys=None, comments=True):
    """Run one sync against the fake; return {table: {id: row}}."""
    config = _config(figma)
    files = list_files(config, list(team_ids) or None, folder_ids, file_keys)
    tables: dict[str, dict] = {FIGMA_TABLE_NAME: {}, FIGMA_COMMENTS_TABLE_NAME: {}}
    for table, row in sync_files(config, files, state, include_comments=comments):
        tables[table][row["id"]] = row
    return tables


def _files(tables):
    return tables[FIGMA_TABLE_NAME]


def _threads(tables):
    return tables[FIGMA_COMMENTS_TABLE_NAME]


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def _document(pages, editor="figma"):
    return {
        "name": "Spec",
        "editorType": editor,
        "version": "42",
        "document": {
            "type": "DOCUMENT",
            "children": [
                {"id": f"{index}:0", "type": "CANVAS", "name": name, "children": nodes}
                for index, (name, nodes) in enumerate(pages.items())
            ],
        },
    }


def test_render_document_keeps_pages_frame_names_and_text_only():
    document = _document(
        {
            "Checkout": [
                frame(
                    "Payment",
                    text("Card number"),
                    frame("Frame 12", text("Pay $10"), id="2:5"),  # default name: no heading
                    frame("Errors", text("Card declined"), node_type="SECTION"),
                    {"type": "RECTANGLE", "name": "Background", "fills": [{"type": "SOLID"}]},
                    text("Old copy", visible=False),
                    frame("Hidden variant", text("Never shown"), visible=False),
                    id="2:1",
                ),
            ],
            "Archive": [],
        }
    )

    content, locations = render_document(document, folder="Design")

    assert content == (
        "Type: Figma design file\n"
        "Folder: Design\n"
        "Pages: Checkout, Archive\n"
        "\n"
        "## Page: Checkout\n"
        "### Frame: Payment\n"
        "Card number\n"
        "Pay $10\n"
        "#### Section: Errors\n"
        "Card declined\n"
        "\n"
        "## Page: Archive"
    )
    # Geometry, fills and the file version never reach the text.
    assert "Background" not in content and "42" not in content
    assert locations["2:1"] == "Checkout / Payment"
    assert locations["2:5"] == "Checkout / Payment"
    assert locations["0:0"] == "Checkout"


def test_render_document_reads_figjam_stickies_and_shapes():
    document = _document(
        {
            "Board": [
                {"type": "STICKY", "name": "Sticky", "characters": "Ship the beta in May"},
                {"type": "SHAPE_WITH_TEXT", "name": "Shape", "characters": "Decision"},
                {"type": "CONNECTOR", "name": "Arrow", "characters": ""},
            ]
        },
        editor="figjam",
    )

    content, _ = render_document(document)

    assert content.startswith("Type: FigJam board\n")
    assert content.endswith("## Page: Board\nShip the beta in May\nDecision")


def test_render_document_survives_very_deep_trees():
    node = text("bottom")
    for depth in range(3000):
        node = {"type": "GROUP", "name": f"Group {depth}", "children": [node]}

    content, _ = render_document(_document({"Deep": [node]}))

    assert content.endswith("bottom")


def _comment(comment_id, message, created, author="Ada", parent="", **extra):
    return {
        "id": comment_id,
        "parent_id": parent,
        "user": {"handle": author},
        "created_at": created,
        "resolved_at": None,
        "message": message,
        "order_id": None if parent else "3",
        "client_meta": {"x": 1, "y": 2},
        **extra,
    }


def test_comment_threads_render_root_then_replies_with_authors_and_status():
    comments = [
        _comment("2", "Agreed, use the pill.", "2026-01-01T09:05:00Z", "Bob", parent="1"),
        _comment(
            "1",
            "Should the CTA be a pill?\nSee spec.",
            "2026-01-01T09:00:00Z",
            client_meta={"node_id": "2:1", "node_offset": {"x": 0, "y": 0}},
            resolved_at="2026-01-02T10:00:00Z",
        ),
        _comment("3", "Fine by me.", "2026-01-01T09:01:00Z", "Cy", parent="1"),
        _comment("9", "", "2026-01-01T09:00:00Z"),  # no text: no document
    ]

    rows = comment_thread_rows("key", "Checkout", comments, {"2:1": "Checkout / Payment"})

    assert rows == [
        {
            "id": "comment:key:1",
            "title": "Checkout: comment #3: Should the CTA be a pill?",
            "content": (
                'Comment thread on Figma file "Checkout".\n'
                "Pinned on: Checkout / Payment\n"
                "Status: resolved on 2026-01-02\n"
                "\n"
                "Ada (2026-01-01 09:00 UTC):\nShould the CTA be a pill?\nSee spec.\n\n"
                "Reply from Cy (2026-01-01 09:01 UTC):\nFine by me.\n\n"
                "Reply from Bob (2026-01-01 09:05 UTC):\nAgreed, use the pill."
            ),
            "url": "https://www.figma.com/file/key",
        }
    ]


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "file:abc",
            "url": "https://www.figma.com/file/abc",
            "title": "Onboarding",
            "content": "## Page: Flows",
        },
        table_name=FIGMA_TABLE_NAME,
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, row.row_data["id"])

    item = _build_document_data_item(row, data_id, FIGMA_SOURCE_NAME)

    # source="figma" (not "dlt") is what routes the file through normal cognify.
    assert item.system_metadata["source"] == "figma"
    assert item.system_metadata["external_id"] == "file:abc"
    assert item.data.startswith("# Onboarding")


# ---------------------------------------------------------------------------
# Source construction
# ---------------------------------------------------------------------------


def test_figma_source_declares_document_marker_and_pipeline_scope():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, document_source_tag

    source = figma_source(TOKEN, folder_ids=[DESIGN])

    assert document_source_tag(source) == "figma"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "figma"


def test_a_selection_is_required_but_may_be_explicitly_empty():
    with pytest.raises(ValueError, match="team_ids=, folder_ids= or file_keys="):
        figma_source(TOKEN)
    figma_source(TOKEN, folder_ids=[])


def test_tokens_fall_back_to_env_and_are_required(monkeypatch):
    monkeypatch.delenv("FIGMA_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("FIGMA_PERSONAL_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="FIGMA_ACCESS_TOKEN"):
        figma_client_config()

    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "oauth")
    assert figma_client_config()["auth"].token == "oauth"


def test_personal_access_token_is_sent_as_x_figma_token(figma):
    config = figma_client_config(personal_access_token=TOKEN, session=figma.session())

    files = list_files(config, folder_ids=[OTHER])

    assert [file["key"] for file in files] == ["file-c"]


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_team_listing_walks_nested_folders(figma):
    folders = list_folders(_config(figma), team_ids=[TEAM])

    assert {folder["id"] for folder in folders} == {DESIGN, NESTED, OTHER}
    assert {f["id"]: f.get("_teams_id") for f in folders}[DESIGN] == TEAM


def test_folder_selection_includes_subfolders_and_names_the_folder(figma):
    files = list_files(_config(figma), folder_ids=[DESIGN])

    assert {file["key"]: file["folder"] for file in files} == {
        "file-a": "Design",
        "file-b": "Checkout",
    }


def test_file_keys_use_file_metadata_and_skip_missing_files(figma):
    files = list_files(_config(figma), file_keys=["file-c", "missing"])

    assert files == [
        {
            "key": "file-c",
            "name": "Campaign",
            "modified": figma.files["file-c"]["last_modified"],
            "folder": "Marketing",
        }
    ]


def test_unknown_team_aborts_instead_of_listing_nothing(figma):
    with pytest.raises(Exception, match="404"):
        list_files(_config(figma), team_ids=["no-such-team"])


# ---------------------------------------------------------------------------
# Sync: ingest path and incremental cursor
# ---------------------------------------------------------------------------


def test_first_sync_ingests_files_and_comment_threads(figma):
    root = figma.comment("file-a", "Is *Welcome* final?", node_id="7:1")
    figma.comment("file-a", "Yes, ship it.", author="Bob", parent=root)

    tables = _sync(figma, {})

    assert set(_files(tables)) == {"file:file-a", "file:file-b", "file:file-c"}
    onboarding = _files(tables)["file:file-a"]
    assert onboarding["title"] == "Onboarding"
    assert onboarding["url"] == "https://www.figma.com/file/file-a"
    assert "### Frame: Welcome\nCreate your account" in onboarding["content"]
    thread = _threads(tables)[f"comment:file-a:{root}"]
    assert "Pinned on: Flows / Welcome" in thread["content"]
    assert "Reply from Bob" in thread["content"]


def test_unchanged_files_come_from_the_cache_without_fetching_node_trees(figma):
    state = {}
    first = _sync(figma, state)
    assert len(figma.file_requests()) == 3

    figma.requests.clear()
    second = _sync(figma, state)

    # Still a complete snapshot (replace needs every row), from the cache.
    assert second == first
    assert figma.file_requests() == []


def test_only_files_whose_stamp_moved_are_fetched_again(figma):
    state = {}
    _sync(figma, state)
    figma.edit("file-a", {"Flows": [frame("Welcome", text("Sign up free"))]})
    figma.requests.clear()

    tables = _sync(figma, state)

    assert figma.file_requests() == ["/v1/files/file-a"]
    assert "Sign up free" in _files(tables)["file:file-a"]["content"]
    assert state["files"]["file-a"]["version"] == "2"


def test_geometry_only_edit_re_renders_to_identical_content(figma):
    state = {}
    before = _sync(figma, state)
    figma.touch("file-b")

    after = _sync(figma, state)

    # Re-fetched (the stamp moved) but content is unchanged, so cognee keeps
    # the same content-hash data_id and does not re-cognify it.
    assert after == before


def test_comments_are_synced_every_run_without_touching_the_file(figma):
    state = {}
    _sync(figma, state)
    root = figma.comment("file-c", "Use the blue banner")
    figma.requests.clear()

    tables = _sync(figma, state)

    assert set(_threads(tables)) == {f"comment:file-c:{root}"}
    assert figma.file_requests() == []

    figma.comment("file-c", "Blue it is", author="Bob", parent=root)
    figma.resolve("file-c", root)
    content = _threads(_sync(figma, state))[f"comment:file-c:{root}"]["content"]
    assert "Status: resolved on" in content and "Reply from Bob" in content


def test_comments_can_be_turned_off(figma):
    figma.comment("file-a", "hello")

    tables = _sync(figma, {}, comments=False)

    assert _threads(tables) == {}
    assert not any(path.endswith("/comments") for path in figma.requests)


# ---------------------------------------------------------------------------
# Sync: forget-on-delete
# ---------------------------------------------------------------------------


def test_deleted_file_drops_out_of_the_snapshot_and_the_cache(figma):
    state = {}
    figma.comment("file-b", "Card copy?")
    _sync(figma, state)

    figma.delete_file("file-b")
    tables = _sync(figma, state)

    assert set(_files(tables)) == {"file:file-a", "file:file-c"}
    assert _threads(tables) == {}
    assert "file-b" not in state["files"]


def test_deleted_folder_forgets_its_files(figma):
    state = {}
    _sync(figma, state)

    figma.remove_folder(NESTED)
    tables = _sync(figma, state)

    assert set(_files(tables)) == {"file:file-a", "file:file-c"}


def test_deleted_selected_folder_answers_404_and_is_forgotten(figma):
    files = list_files(_config(figma), folder_ids=["deleted-folder", OTHER])

    assert [file["key"] for file in files] == ["file-c"]


def test_deselected_folder_is_forgotten(figma):
    state = {}
    _sync(figma, state, team_ids=(), folder_ids=[DESIGN, OTHER])

    tables = _sync(figma, state, team_ids=(), folder_ids=[OTHER])

    assert set(_files(tables)) == {"file:file-c"}


def test_deleted_comment_and_reply_drop_out(figma):
    state = {}
    keep = figma.comment("file-a", "keep")
    reply = figma.comment("file-a", "reply to drop", parent=keep)
    gone = figma.comment("file-a", "delete me")
    _sync(figma, state)

    figma.delete_comment("file-a", gone)
    figma.delete_comment("file-a", reply)
    tables = _sync(figma, state)

    assert set(_threads(tables)) == {f"comment:file-a:{keep}"}
    assert "reply to drop" not in _threads(tables)[f"comment:file-a:{keep}"]["content"]


def test_file_deleted_between_listing_and_fetch_is_forgotten(figma):
    state = {}
    _sync(figma, state)
    figma.edit("file-a", {"Flows": []})
    figma.fail_once("/v1/files/file-a", 404)

    tables = _sync(figma, state, comments=False)

    assert "file:file-a" not in _files(tables)
    assert "file-a" not in state["files"]


def test_explicitly_empty_selection_yields_an_empty_snapshot(figma):
    state = {}
    _sync(figma, state)

    tables = _sync(figma, state, team_ids=(), folder_ids=[])

    assert _files(tables) == {} and _threads(tables) == {}


# ---------------------------------------------------------------------------
# Failure safety and retries
# ---------------------------------------------------------------------------


def test_forbidden_file_aborts_and_keeps_the_cache(figma):
    state = {}
    _sync(figma, state)
    before = {key: dict(entry) for key, entry in state["files"].items()}
    figma.edit("file-a", {"Flows": []})
    figma.fail_once("/v1/files/file-a", 403)

    with pytest.raises(Exception, match="403"):
        _sync(figma, state)

    assert state["files"] == before


def test_throttled_requests_are_retried_with_retry_after(figma):
    from dlt.sources.helpers.requests import Client

    figma.fail_once("/v1/files/file-c", 429, {"Retry-After": "0"})
    # dlt's retrying client, as used in production, routed to the fake.
    session = Client(raise_for_status=False, request_max_attempts=3).session
    session.mount("https://api.figma.com/", figma)
    config = figma_client_config(TOKEN, session=session)

    files = list_files(config, folder_ids=[OTHER])
    rows = list(sync_files(config, files, {}))

    assert [row["id"] for _table, row in rows] == ["file:file-c"]
    assert figma.file_requests() == ["/v1/files/file-c", "/v1/files/file-c"]


# ---------------------------------------------------------------------------
# dlt pipeline: replace snapshot, cache in source state, failure safety
# ---------------------------------------------------------------------------


def _pipeline(tmp_path):
    db_path = (tmp_path / "figma.db").as_posix()
    return dlt.pipeline(
        pipeline_name="figma_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="figma_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run(pipeline, figma, **source_kwargs):
    source_kwargs.setdefault("team_ids", [TEAM])
    return pipeline.run(figma_source(TOKEN, session=figma.session(), **source_kwargs))


def _read(pipeline, table):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {table}") as cursor,
    ):
        return dict(cursor.fetchall())


def test_pipeline_replaces_the_snapshot_and_keeps_the_cache_across_runs(figma, tmp_path):
    root = figma.comment("file-a", "Is this final?")
    pipeline = _pipeline(tmp_path)
    _run(pipeline, figma)
    assert set(_read(pipeline, FIGMA_TABLE_NAME)) == {"file:file-a", "file:file-b", "file:file-c"}
    assert set(_read(pipeline, FIGMA_COMMENTS_TABLE_NAME)) == {f"comment:file-a:{root}"}

    figma.delete_file("file-b")
    figma.delete_comment("file-a", root)
    figma.requests.clear()
    _run(pipeline, figma)

    # replace dropped the deleted file and the deleted (last) comment thread...
    assert set(_read(pipeline, FIGMA_TABLE_NAME)) == {"file:file-a", "file:file-c"}
    assert _read(pipeline, FIGMA_COMMENTS_TABLE_NAME) == {}
    # ...while the render cache survived in source state, which replace keeps.
    assert figma.file_requests() == []


def test_failed_run_keeps_staging_and_cache_so_the_next_run_catches_up(figma, tmp_path):
    pipeline = _pipeline(tmp_path)
    _run(pipeline, figma)

    figma.edit("file-c", {"Ads": [text("Summer sale")]})
    figma.fail_once("/v1/files/file-c", 503)
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the error in PipelineStepFailed
        _run(pipeline, figma)
    files = _read(pipeline, FIGMA_TABLE_NAME)
    assert set(files) == {"file:file-a", "file:file-b", "file:file-c"}
    assert "Spring sale" in files["file:file-c"]

    _run(pipeline, figma)
    assert "Summer sale" in _read(pipeline, FIGMA_TABLE_NAME)["file:file-c"]
