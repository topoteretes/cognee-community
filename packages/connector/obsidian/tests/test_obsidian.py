"""Unit tests for the Obsidian vault dlt connector.

Two layers, all runnable in CI with no real vault, no network, and no LLM key:

* DB-free tests for frontmatter splitting, title/tags fallbacks, wikilink
  extraction, note→row flattening, and the generic document DataItem tagging
  (``source="obsidian"``) that routes notes through normal cognify.
* dlt-pipeline tests (vaults built in ``tmp_path``, temp sqlite destination)
  covering the acceptance criteria: re-sync reflects edits, and deleted notes
  drop out of the full-snapshot load (forget-on-delete).
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

# The row → document-DataItem mapping is generic and owned by the ingestion
# layer (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_obsidian.obsidian import (
    OBSIDIAN_SOURCE_NAME,
    _extract_wikilinks,
    _note_content,
    _note_tags,
    _note_title,
    _note_to_row,
    _split_frontmatter,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_vault(tmp_path, files):
    """Write ``{relative_path: text | bytes}`` into a vault dir; return it."""
    from pathlib import Path

    vault = Path(tmp_path) / "vault"
    vault.mkdir()
    for rel, content in files.items():
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return vault


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def test_split_frontmatter_parses_metadata():
    text = "---\ntitle: Hello\ntags: [a, b]\n---\nBody here.\n"
    metadata, body = _split_frontmatter(text)
    assert metadata == {"title": "Hello", "tags": ["a", "b"]}
    assert body == "Body here.\n"


def test_split_frontmatter_missing_returns_empty_metadata():
    text = "# Just a note\n\nNo frontmatter.\n"
    assert _split_frontmatter(text) == ({}, text)


def test_split_frontmatter_malformed_degrades_to_plain_text():
    text = "---\ntitle: [unclosed\n---\nBody here.\n"
    metadata, body = _split_frontmatter(text)
    assert metadata == {}
    assert "Body here." in body


def test_split_frontmatter_non_mapping_degrades_to_plain_text():
    text = "---\n- just\n- a\n- list\n---\nBody here.\n"
    metadata, body = _split_frontmatter(text)
    assert metadata == {}
    assert "Body here." in body


# ---------------------------------------------------------------------------
# Title / tags fallbacks
# ---------------------------------------------------------------------------


def test_note_title_prefers_frontmatter(tmp_path):
    from pathlib import Path

    assert _note_title({"title": "  Front  "}, "# Heading", Path("note.md")) == "Front"


def test_note_title_falls_back_to_heading_then_stem(tmp_path):
    from pathlib import Path

    assert _note_title({}, "# Real Heading\n\nbody", Path("note.md")) == "Real Heading"
    assert _note_title({}, "no heading here", Path("my_note.md")) == "my note"
    assert _note_title({"title": ""}, "no heading", Path("x.md")) == "x"


def test_note_tags_normalizes_shapes():
    assert _note_tags({"tags": ["b", "a", "a"]}) == "a, b"
    assert _note_tags({"tags": "alpha, beta"}) == "alpha, beta"
    assert _note_tags({"tags": "#a #b"}) == "a, b"
    assert _note_tags({"tag": "solo"}) == "solo"
    assert _note_tags({}) == ""
    assert _note_tags({"tags": 42}) == ""


# ---------------------------------------------------------------------------
# Wikilinks
# ---------------------------------------------------------------------------


def test_extract_wikilinks_covers_forms():
    body = (
        "See [[target]] and [[other|Alias]] and [[doc#Section]] "
        "and ![[embedded note]] and [[target]] again."
    )
    assert _extract_wikilinks(body) == ["target", "other", "doc", "embedded note"]


def test_extract_wikilinks_skips_attachments():
    body = "![[photo.png]] and ![[clip.mp3]] and [[real note]]"
    assert _extract_wikilinks(body) == ["real note"]


def test_extract_wikilinks_empty():
    assert _extract_wikilinks("no links here") == []


def test_note_content_appends_related_line():
    assert _note_content("body", []) == "body"
    assert _note_content("body", ["a", "b"]) == "body\n\nRelated notes: a, b"


# ---------------------------------------------------------------------------
# Note → row
# ---------------------------------------------------------------------------


def test_note_to_row_flattens_note(tmp_path):
    vault = _write_vault(
        tmp_path,
        {"projects/alpha.md": "---\ntitle: Alpha\ntags: [x]\n---\nBody with [[beta]].\n"},
    )

    row = _note_to_row(vault, vault / "projects" / "alpha.md")

    # Stable relative-path id; only identity/text kept, so a metadata-only
    # touch does not churn the content-hash data_id.
    assert row["id"] == "projects/alpha.md"
    assert row["title"] == "Alpha"
    assert row["tags"] == "x"
    assert "Body with" in row["content"]
    assert "Related notes: beta" in row["content"]
    assert "mtime" not in row


def test_note_to_row_handles_bad_encoding(tmp_path):
    vault = _write_vault(tmp_path, {"broken.md": b"\xff\xfe not utf-8 \x00 binary"})
    row = _note_to_row(vault, vault / "broken.md")
    assert row["id"] == "broken.md"
    assert row["title"] == "broken"
    assert isinstance(row["content"], str)


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        table_name="obsidian_notes",
        row_data={
            "id": "alpha.md",
            "title": "Alpha",
            "tags": "x",
            "content": "Body with\n\nRelated notes: beta",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "alpha.md")

    item = _build_document_data_item(row, data_id, "obsidian")

    # source="obsidian" (not "dlt") is what routes the note through normal cognify.
    assert item.system_metadata["source"] == "obsidian"
    assert item.system_metadata["external_id"] == "alpha.md"
    assert item.data_id == data_id
    assert item.data.startswith("# Alpha")
    assert "Related notes: beta" in item.data


def test_obsidian_source_declares_document_marker(tmp_path):
    # resolve_dlt_sources routes on the document-source marker (not on this name),
    # but the tag it carries is the source name; keep it stable.
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_obsidian.obsidian import obsidian_source

    vault = _write_vault(tmp_path, {"a.md": "hello"})
    source = obsidian_source(vault)
    assert OBSIDIAN_SOURCE_NAME == "obsidian"
    assert document_source_tag(source) == "obsidian"


def test_obsidian_source_rejects_missing_vault(tmp_path):
    from cognee_community_connector_obsidian.obsidian import obsidian_source

    with pytest.raises(ValueError, match="not a directory"):
        obsidian_source(tmp_path / "nope")


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete
# ---------------------------------------------------------------------------


def _run_sync(dlt, tmp_path, vault):
    """Run obsidian_source through a dlt pipeline into a temp sqlite destination."""
    from cognee_community_connector_obsidian.obsidian import obsidian_source

    db_path = (tmp_path / "obsidian.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="obsidian_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="obsidian_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(obsidian_source(vault))
    return pipeline


def _read_notes(pipeline):
    """Return {id: row-dict} for the obsidian_notes table.

    Reads positionally (the SELECT fixes the column order) since dlt's
    sqlalchemy cursor exposes a SQLAlchemy Result without DB-API ``description``.
    """
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM obsidian_notes") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_notes(dlt_mod, tmp_path):
    vault = _write_vault(
        tmp_path,
        {
            "alpha.md": "# Alpha\n\nLinks to [[beta]].\n",
            "sub/beta.md": "# Beta\n\nNo links.\n",
            ".obsidian/config": "should be skipped",
        },
    )

    rows = _read_notes(_run_sync(dlt_mod, tmp_path, vault))

    assert set(rows) == {"alpha.md", "sub/beta.md"}
    assert "Related notes: beta" in rows["alpha.md"]["content"]


def test_include_glob_selects_scope(dlt_mod, tmp_path):
    from cognee_community_connector_obsidian.obsidian import obsidian_source

    vault = _write_vault(tmp_path, {"keep/a.md": "x", "skip/b.md": "y", "top.md": "z"})
    db_path = (tmp_path / "scoped.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="obsidian_scoped",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="obsidian_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(obsidian_source(vault, include=["keep/*.md"]))

    assert set(_read_notes(pipeline)) == {"keep/a.md"}


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    vault = _write_vault(tmp_path, {"a.md": "# A\n\nv1"})
    _run_sync(dlt_mod, tmp_path, vault)

    (vault / "a.md").write_text("# A\n\nv2", encoding="utf-8")
    rows = _read_notes(_run_sync(dlt_mod, tmp_path, vault))

    assert "v2" in rows["a.md"]["content"]
    assert "v1" not in rows["a.md"]["content"]


def test_deleted_note_is_removed_on_resync(dlt_mod, tmp_path):
    vault = _write_vault(tmp_path, {"a.md": "# A", "b.md": "# B"})
    _run_sync(dlt_mod, tmp_path, vault)

    # Deleting the note upstream drops it from the replace load, so it falls
    # out of staging → orphan cleanup forgets it downstream.
    (vault / "a.md").unlink()
    rows = _read_notes(_run_sync(dlt_mod, tmp_path, vault))

    assert "a.md" not in rows
    assert "b.md" in rows
