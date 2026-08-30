"""Unit tests for the Obsidian dlt connector.

Two layers, all runnable in CI with nothing but a tmp_path vault:

* DB-free tests for frontmatter splitting, wikilink parsing (all three forms +
  embeds), vault-wide name resolution, exclusions, and the pure ``sync_notes``
  state machine (ingest, incremental cursor, mtime-backwards regression,
  deletion detection) with a plain dict standing in for dlt's resource state.
* dlt-pipeline tests (temp sqlite destination) covering the acceptance
  criteria end to end: first sync loads notes, edits re-sync, deleted notes
  drop out on ``merge`` via the ``_deleted`` hard-delete marker.
"""

import json
import os
import time

import pytest

from cognee_community_connector_obsidian.obsidian import (
    OBSIDIAN_SOURCE_NAME,
    _build_link_index,
    _extract_wikilinks,
    _split_frontmatter,
    _VaultConfig,
    _walk_vault,
    sync_notes,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _vault(tmp_path, files: dict[str, str]):
    """Materialize {relative_path: content} as a vault directory."""
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def _config(tmp_path, **kwargs) -> _VaultConfig:
    return _VaultConfig(vault_path=tmp_path, **kwargs)


def _run(tmp_path, state) -> list[dict]:
    return list(sync_notes(_config(tmp_path), state))


def _live_rows(rows) -> dict[str, dict]:
    return {row["id"]: row for row in rows if not row.get("_deleted")}


def _deleted_ids(rows) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def test_frontmatter_becomes_metadata_and_is_stripped_from_body():
    meta, body = _split_frontmatter("---\ntitle: My Note\ntags: [a, b]\n---\nThe body.\n")
    assert meta == {"title": "My Note", "tags": ["a", "b"]}
    assert body.strip() == "The body."


def test_no_frontmatter_returns_empty_metadata():
    meta, body = _split_frontmatter("Just a body.")
    assert meta == {}
    assert body == "Just a body."


def test_malformed_frontmatter_keeps_note_ingestable():
    text = "---\n: not yaml [\n---\nStill here.\n"
    meta, body = _split_frontmatter(text)
    assert meta == {}
    assert "Still here." in body


# ---------------------------------------------------------------------------
# Wikilinks: the three forms + embeds
# ---------------------------------------------------------------------------


def test_plain_wikilink():
    (link,) = _extract_wikilinks("See [[Other Note]].")
    assert (link.target, link.heading, link.alias, link.embed) == ("Other Note", None, None, False)


def test_aliased_wikilink_targets_note_not_alias():
    (link,) = _extract_wikilinks("See [[Other Note|the other one]].")
    assert link.target == "Other Note"
    assert link.alias == "the other one"


def test_heading_wikilink_targets_note_not_heading():
    (link,) = _extract_wikilinks("See [[Other Note#Some Heading]].")
    assert link.target == "Other Note"
    assert link.heading == "Some Heading"


def test_embed_sets_transclusion_flag():
    (link,) = _extract_wikilinks("![[Embedded Note]]")
    assert link.target == "Embedded Note"
    assert link.embed is True


def test_combined_heading_and_alias():
    (link,) = _extract_wikilinks("[[Note#H|shown]]")
    assert (link.target, link.heading, link.alias) == ("Note", "H", "shown")


# ---------------------------------------------------------------------------
# Name-based vault-wide resolution
# ---------------------------------------------------------------------------


def test_link_resolves_by_name_anywhere_in_vault():
    index = _build_link_index(["deep/nested/Target.md", "Home.md"])
    assert index["target"] == "deep/nested/Target.md"


def test_ambiguous_name_prefers_shortest_path():
    index = _build_link_index(["a/b/Note.md", "a/Note.md", "Other.md"])
    assert index["note"] == "a/Note.md"


def test_path_style_link_resolves_by_path():
    index = _build_link_index(["a/Note.md", "b/Note.md"])
    assert index["b/note"] == "b/Note.md"
    assert index["b/note.md"] == "b/Note.md"


def test_resolution_is_case_insensitive():
    index = _build_link_index(["Folder/My Target.md"])
    assert index["my target"] == "Folder/My Target.md"


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_default_exclusions_obsidian_trash_and_sync_conflicts(tmp_path):
    _vault(
        tmp_path,
        {
            "Live.md": "live",
            ".obsidian/workspace.md": "config",
            ".trash/Deleted.md": "gone",
            "notes/Live.sync-conflict-20260830-ABCDEF.md": "conflict copy",
            "notes/Nested.md": "nested",
        },
    )
    assert _walk_vault(_config(tmp_path)) == ["Live.md", "notes/Nested.md"]


def test_extra_exclude_patterns(tmp_path):
    _vault(tmp_path, {"Keep.md": "k", "drafts/Skip.md": "s"})
    config = _config(tmp_path, extra_excludes=("drafts/*",))
    assert _walk_vault(config) == ["Keep.md"]


def test_non_markdown_files_are_ignored(tmp_path):
    _vault(tmp_path, {"Note.md": "n"})
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    assert _walk_vault(_config(tmp_path)) == ["Note.md"]


# ---------------------------------------------------------------------------
# Ingest path (pure sync_notes)
# ---------------------------------------------------------------------------


def test_first_sync_ingests_notes_with_metadata_and_links(tmp_path):
    _vault(
        tmp_path,
        {
            "Home.md": (
                "---\ntitle: Home Base\ntags: [index]\n---\n"
                "Start at [[Projects|my projects]] or [[Projects#Active]].\n![[Diagram]]\n"
            ),
            "work/Projects.md": "All projects.",
            "assets/Diagram.md": "A diagram note.",
        },
    )
    state: dict = {}
    rows = _live_rows(_run(tmp_path, state))

    assert set(rows) == {"Home.md", "work/Projects.md", "assets/Diagram.md"}
    home = rows["Home.md"]
    # Frontmatter → metadata (JSON column) and title.
    assert home["title"] == "Home Base"
    assert json.loads(home["metadata"]) == {"title": "Home Base", "tags": ["index"]}
    # Frontmatter is folded into the content so it survives entity extraction.
    assert "tags: index" in home["content"]
    # Wikilinks → edges: alias and heading forms both target Projects.md,
    # the embed targets Diagram.md with the transclusion flag.
    links = json.loads(home["links"])
    targets = {(link["target"], link["embed"]) for link in links}
    assert targets == {("work/Projects.md", False), ("assets/Diagram.md", True)}
    assert "Links to notes: Projects." in home["content"]
    assert "Embeds (transcludes) notes: Diagram." in home["content"]
    # Provenance URL uses Obsidian's own URI scheme.
    assert home["url"] == "obsidian://open?file=Home"


def test_unresolved_link_is_kept_with_null_target(tmp_path):
    _vault(tmp_path, {"A.md": "See [[Missing Note]]."})
    rows = _live_rows(_run(tmp_path, {}))
    (link,) = json.loads(rows["A.md"]["links"])
    assert link["target"] is None
    assert link["raw"] == "Missing Note"


# ---------------------------------------------------------------------------
# Incremental cursor
# ---------------------------------------------------------------------------


def test_second_run_with_no_changes_emits_nothing(tmp_path):
    _vault(tmp_path, {"A.md": "alpha", "B.md": "beta"})
    state: dict = {}
    assert len(_run(tmp_path, state)) == 2
    assert _run(tmp_path, state) == []


def test_edited_note_is_re_emitted(tmp_path):
    _vault(tmp_path, {"A.md": "v1", "B.md": "stays"})
    state: dict = {}
    _run(tmp_path, state)

    (tmp_path / "A.md").write_text("v2", encoding="utf-8")
    rows = _run(tmp_path, state)
    assert set(_live_rows(rows)) == {"A.md"}
    assert "v2" in _live_rows(rows)["A.md"]["content"]


def test_mtime_moved_backwards_with_changed_content_is_still_caught(tmp_path):
    # Regression: sync tools (Syncthing/iCloud) rewrite mtimes and can move
    # them BACKWARDS. A pure "newer than" cursor would skip this edit; the
    # content-hash tiebreak must catch it.
    _vault(tmp_path, {"A.md": "v1"})
    state: dict = {}
    _run(tmp_path, state)

    path = tmp_path / "A.md"
    path.write_text("v2 via sync", encoding="utf-8")
    past = time.time() - 3600
    os.utime(path, (past, past))  # mtime now BEFORE the recorded cursor

    rows = _live_rows(_run(tmp_path, state))
    assert set(rows) == {"A.md"}
    assert "v2 via sync" in rows["A.md"]["content"]


def test_mtime_changed_but_content_identical_is_not_re_emitted(tmp_path):
    # The other half of the (mtime, hash) cursor: a touch without an edit
    # refreshes the cursor but must not re-ingest/re-cognify the note.
    _vault(tmp_path, {"A.md": "same"})
    state: dict = {}
    _run(tmp_path, state)

    past = time.time() - 3600
    os.utime(tmp_path / "A.md", (past, past))
    assert _run(tmp_path, state) == []
    # Cursor refreshed: a third run stays quiet on the fast path too.
    assert _run(tmp_path, state) == []


def test_new_note_re_resolves_links_in_unchanged_neighbors(tmp_path):
    # A.md links [[Target]] before Target.md exists. Creating Target.md later
    # changes A's link resolution even though A's bytes are unchanged — the
    # rendered-row fingerprint must re-emit A with the resolved edge.
    _vault(tmp_path, {"A.md": "See [[Target]]."})
    state: dict = {}
    rows = _live_rows(_run(tmp_path, state))
    (link,) = json.loads(rows["A.md"]["links"])
    assert link["target"] is None

    _vault(tmp_path, {"sub/Target.md": "Now I exist."})
    rows = _live_rows(_run(tmp_path, state))
    assert "A.md" in rows
    (link,) = json.loads(rows["A.md"]["links"])
    assert link["target"] == "sub/Target.md"


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------


def test_deleted_note_yields_hard_delete_tombstone(tmp_path):
    _vault(tmp_path, {"A.md": "a", "B.md": "b"})
    state: dict = {}
    _run(tmp_path, state)

    (tmp_path / "A.md").unlink()
    rows = _run(tmp_path, state)
    assert _deleted_ids(rows) == {"A.md"}
    assert "A.md" not in state["files"]

    # A rename is delete + create by construction (path is identity).
    (tmp_path / "B.md").rename(tmp_path / "C.md")
    rows = _run(tmp_path, state)
    assert _deleted_ids(rows) == {"B.md"}
    assert set(_live_rows(rows)) == {"C.md"}


def test_empty_walk_over_known_vault_skips_mass_deletion(tmp_path):
    _vault(tmp_path, {"A.md": "a"})
    state: dict = {}
    _run(tmp_path, state)

    (tmp_path / "A.md").unlink()
    (tmp_path / ".obsidian").mkdir()  # vault dir still "looks like" a vault
    # Everything vanished at once: treat as a transient walk, not a wipe.
    assert _run(tmp_path, state) == []
    assert "A.md" in state["files"]  # state preserved for the next run


# ---------------------------------------------------------------------------
# Factory validation
# ---------------------------------------------------------------------------


def test_source_requires_vault_path(monkeypatch):
    pytest.importorskip("dlt")
    from cognee_community_connector_obsidian.obsidian import obsidian_source

    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    with pytest.raises(ValueError, match="vault_path is required"):
        obsidian_source()


def test_source_rejects_missing_directory(tmp_path):
    pytest.importorskip("dlt")
    from cognee_community_connector_obsidian.obsidian import obsidian_source

    with pytest.raises(ValueError, match="not found"):
        obsidian_source(vault_path=tmp_path / "nope")


def test_source_declares_document_marker(tmp_path):
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_obsidian.obsidian import obsidian_source

    source = obsidian_source(vault_path=tmp_path)
    assert OBSIDIAN_SOURCE_NAME == "obsidian"
    assert document_source_tag(source) == "obsidian"


# ---------------------------------------------------------------------------
# dlt pipeline: incremental sync + forget-on-delete end to end (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_pipeline(dlt, tmp_path, vault):
    """Run obsidian_source through a dlt pipeline into a temp sqlite destination."""
    from cognee_community_connector_obsidian.obsidian import obsidian_source

    db_path = (tmp_path / "obsidian.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="obsidian_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="obsidian_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(obsidian_source(vault_path=vault))
    return pipeline


def _read_notes(pipeline):
    """Return {id: row-dict} for the obsidian_notes table (positional read —
    dlt's sqlalchemy cursor exposes a SQLAlchemy Result without ``description``)."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM obsidian_notes") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


def test_pipeline_first_sync_loads_notes(dlt_mod, tmp_path):
    vault = _vault(tmp_path / "vault", {"Home.md": "Welcome to [[Projects]].", "Projects.md": "P."})
    pipeline = _run_pipeline(dlt_mod, tmp_path, vault)

    rows = _read_notes(pipeline)
    assert set(rows) == {"Home.md", "Projects.md"}
    assert "Links to notes: Projects." in rows["Home.md"]["content"]


def test_pipeline_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    vault = _vault(tmp_path / "vault", {"A.md": "v1"})
    _run_pipeline(dlt_mod, tmp_path, vault)

    (vault / "A.md").write_text("v2", encoding="utf-8")
    pipeline = _run_pipeline(dlt_mod, tmp_path, vault)

    rows = _read_notes(pipeline)
    assert "v2" in rows["A.md"]["content"]
    assert "v1" not in rows["A.md"]["content"]


def test_pipeline_deleted_note_is_removed_on_resync(dlt_mod, tmp_path):
    vault = _vault(tmp_path / "vault", {"A.md": "a", "B.md": "b"})
    _run_pipeline(dlt_mod, tmp_path, vault)

    (vault / "A.md").unlink()
    pipeline = _run_pipeline(dlt_mod, tmp_path, vault)

    rows = _read_notes(pipeline)
    # The _deleted hard-delete marker removed the row on merge; cognee's
    # orphan_cleanup forgets it downstream.
    assert "A.md" not in rows
    assert "B.md" in rows
