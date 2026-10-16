"""Unit tests for the Evernote connector — fully offline, no credentials.

Two layers, neither of which needs a network, a Thrift runtime or an LLM:

* **Pure** — ENML rendering, the sync state machine against
  ``tests/fake_evernote.py`` (a fake with a faithful USN change feed), the
  OAuth token cache, and the EDAM error classifier.
* **dlt wiring** — the resource is configured for merge + id PK + the
  ``_deleted`` hard-delete column and declares document-mode.

The acceptance criteria from issue #4728 map onto these tests as:
incremental-only deltas (``test_incremental_*``), forget-on-delete
(``test_*deletion*`` / ``test_trash_*``), selection (``test_scope_*``), and the
document row contract (``test_row_*``).
"""

import json

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_evernote.evernote import (
    EVERNOTE_SOURCE_NAME,
    EVERNOTE_TABLE_NAME,
    EvernoteAuthError,
    EvernoteNoteStore,
    _deleted_row,
    _error_family,
    _EvernoteConfig,
    _in_scope,
    _iso,
    _note_to_row,
    _note_url,
    _render_enml,
    _retry_delay,
    _scope_key,
    evernote_source,
    load_cached_token,
    resolve_auth_token,
    save_token,
    sync_notes,
)

from .fake_evernote import FakeEvernoteStore, make_note

TOKEN = "S=s1:U=abc:E=def:C=123:P=1cd:A=devtoken:V=2:H=xyz:F=000:T=web&K=abc&S=xyz"


def _config(**kwargs):
    return _EvernoteConfig(**kwargs)


def _rows(store, config=None, state=None):
    """Run one sync and return (live_rows, tombstone_guids)."""
    state = {} if state is None else state
    rows = list(sync_notes(store, config or _config(), state))
    live = [r for r in rows if not r.get("_deleted")]
    tombs = [r["id"] for r in rows if r.get("_deleted")]
    return live, tombs, state


# ---------------------------------------------------------------------------
# ENML rendering
# ---------------------------------------------------------------------------
def test_render_enml_handles_none_and_empty():
    assert _render_enml(None) == ""
    assert _render_enml("") == ""


def test_render_enml_strips_tags_and_unescapes():
    assert _render_enml("<div>Hello&nbsp;<b>world</b> &amp; friends</div>") == (
        "Hello world & friends"
    )


def test_render_enml_renders_headings_as_markdown():
    body = "<div><h1>Title</h1><h3>Sub</h3></div>"
    assert _render_enml(body) == "# Title\n\n### Sub"


def test_render_enml_renders_unordered_and_ordered_lists():
    body = "<ul><li>alpha</li><li>beta</li></ul>"
    assert _render_enml(body) == "- alpha\n- beta"

    body = "<ol><li>first</li><li>second</li></ol>"
    assert _render_enml(body) == "1. first\n2. second"


def test_render_enml_indents_nested_list_items():
    body = "<ul><li>outer<ul><li>inner</li></ul></li><li>sibling</li></ul>"
    assert _render_enml(body) == "- outer\n  - inner\n- sibling"


def test_render_enml_renders_links():
    body = '<div>see <a href="https://example.com/x">the doc</a></div>'
    assert _render_enml(body) == "see [the doc](https://example.com/x)"


def test_render_enml_preserves_ampersands_in_link_hrefs():
    body = '<a href="https://example.com/?a=1&amp;b=2">query</a>'
    assert _render_enml(body) == "[query](https://example.com/?a=1&b=2)"


def test_render_enml_renders_todos_as_checkboxes():
    assert _render_enml('<div><en-todo checked="true"/>done</div>') == "- [x] done"
    assert _render_enml("<div><en-todo/>open</div>") == "- [ ] open"


def test_render_enml_marks_attachments_without_fetching_them():
    body = '<div><en-media type="image/png" hash="h" filename="chart.png"/>caption</div>'
    rendered = _render_enml(body)
    assert "chart.png (image/png)" in rendered
    assert "caption" in rendered


def test_render_enml_renders_table_cells_with_separators():
    body = "<table><tr><td>a</td><td>b</td></tr></table>"
    assert _render_enml(body) == "a | b"


def test_render_enml_drops_script_and_style_content():
    body = "<div>keep<script>drop()</script><style>.x{}</style>this</div>"
    assert _render_enml(body) == "keepthis"


def test_render_enml_collapses_blank_runs_and_trims_lines():
    body = "<div>a</div><div></div><div></div><div>b   </div>"
    assert _render_enml(body) == "a\n\nb"


def test_render_enml_keeps_br_as_a_line_break():
    assert _render_enml("line1<br/>line2") == "line1\nline2"


# ---------------------------------------------------------------------------
# Row contract
# ---------------------------------------------------------------------------
def test_iso_formats_millisecond_timestamps():
    assert _iso(1_700_000_000_000).startswith("2023-11-14T")
    assert _iso(0) == ""
    assert _iso(None) == ""


def test_note_url_prefers_the_clipped_source_url():
    note = make_note("g1", source_url="https://clipped.example/post")
    assert _note_url(note, "https://www.evernote.com") == "https://clipped.example/post"


def test_note_url_falls_back_to_the_web_view():
    assert _note_url(make_note("g1"), "https://www.evernote.com") == (
        "https://www.evernote.com/web/note/g1"
    )


def test_row_carries_the_document_contract_plus_provenance():
    note = make_note("g1", title="My note", notebook_guid="nb-1", tags=["a", "b"])
    row = _note_to_row(note, "body text", "Work", "https://www.evernote.com")

    # Document-mode contract.
    assert row["id"] == "g1"
    assert row["title"] == "My note"
    assert row["content"] == "body text"
    assert row["url"]
    # Provenance columns.
    assert row["notebook"] == "Work"
    assert row["tags"] == "a, b"
    assert row["_deleted"] is False


def test_deleted_row_is_the_minimal_tombstone():
    assert _deleted_row("g1") == {"id": "g1", "_deleted": True}


# ---------------------------------------------------------------------------
# Scope selection
# ---------------------------------------------------------------------------
def test_in_scope_is_permissive_without_a_selection():
    assert _in_scope(make_note("g1", notebook_guid="nb-1", tags=["x"]), _config())


def test_in_scope_filters_by_notebook():
    config = _config(notebook_guids=("nb-1",))
    assert _in_scope(make_note("g1", notebook_guid="nb-1"), config)
    assert not _in_scope(make_note("g2", notebook_guid="nb-2"), config)


def test_in_scope_requires_every_listed_tag_and_is_case_insensitive():
    config = _config(tag_names=("work", "urgent"))
    assert _in_scope(make_note("g1", tags=["Work", "urgent"]), config)
    assert not _in_scope(make_note("g2", tags=["work"]), config)


def test_scope_key_is_order_insensitive_but_selection_sensitive():
    a = _scope_key(_config(tag_names=("x", "y"), notebook_guids=("n1",)))
    b = _scope_key(_config(tag_names=("y", "x"), notebook_guids=("n1",)))
    c = _scope_key(_config(tag_names=("x",), notebook_guids=("n1",)))
    assert a == b, "tag order is not a different selection"
    assert a != c, "dropping a tag IS a different selection"


# ---------------------------------------------------------------------------
# Full sync
# ---------------------------------------------------------------------------
def _seeded_store():
    store = FakeEvernoteStore(notebooks={"nb-1": "Work", "nb-2": "Personal"})
    store.add_note("g1", "<div>alpha note</div>", title="Alpha", notebook_guid="nb-1")
    store.add_note("g2", "<div>bravo note</div>", title="Bravo", notebook_guid="nb-2")
    store.add_note("g3", "<div>charlie note</div>", title="Charlie", notebook_guid="nb-1")
    return store


def test_first_sync_ingests_every_note_and_records_the_cursor():
    store = _seeded_store()
    live, tombs, state = _rows(store)

    assert sorted(r["id"] for r in live) == ["g1", "g2", "g3"]
    assert tombs == []
    assert state["cursor_usn"] == store.usn
    assert sorted(state["known_ids"]) == ["g1", "g2", "g3"]
    assert state["scope_key"]


def test_first_sync_pages_through_multiple_chunks():
    store = _seeded_store()
    store.set_chunk_limit(1)  # force three chunks for three notes
    live, _, state = _rows(store)

    assert sorted(r["id"] for r in live) == ["g1", "g2", "g3"]
    assert state["cursor_usn"] == store.usn
    chunk_calls = [c for c in store.calls if c[0] == "get_sync_chunk"]
    assert len(chunk_calls) == 3, "one chunk per note at max_entries=1"


def test_sync_fetches_content_only_for_the_notes_it_yields():
    store = _seeded_store()
    store.set_chunk_limit(1)
    _rows(store)
    fetched = [args[0] for name, args in store.calls if name == "get_note_content"]
    assert sorted(fetched) == ["g1", "g2", "g3"]


def test_notebook_names_become_provenance():
    store = _seeded_store()
    live, _, _ = _rows(store)
    by_id = {r["id"]: r for r in live}
    assert by_id["g1"]["notebook"] == "Work"
    assert by_id["g2"]["notebook"] == "Personal"


def test_notes_with_no_text_content_are_skipped_not_yielded():
    store = FakeEvernoteStore()
    store.add_note("g1", "")  # empty body
    store.add_note("g2", "<div>real</div>")
    live, _, state = _rows(store)

    assert [r["id"] for r in live] == ["g2"]
    assert "g1" not in state["known_ids"], "an empty note must not linger as known"


# ---------------------------------------------------------------------------
# Incremental sync
# ---------------------------------------------------------------------------
def test_second_sync_with_no_changes_yields_nothing():
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))

    second, tombs, _ = _rows(store, state=state)
    assert second == []
    assert tombs == []


def test_incremental_sync_ingests_only_the_changed_note():
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))

    store.edit_note("g2", "<div>bravo EDITED</div>")
    delta, tombs, _ = _rows(store, state=state)

    assert [r["id"] for r in delta] == ["g2"], "unchanged notes must not be re-ingested"
    assert delta[0]["content"] == "bravo EDITED"
    assert tombs == []


def test_incremental_sync_advances_the_cursor_and_keeps_known_ids():
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))
    store.add_note("g4", "<div>delta</div>")
    _, _, state2 = _rows(store, state=state)

    assert state2["cursor_usn"] == store.usn
    assert sorted(state2["known_ids"]) == ["g1", "g2", "g3", "g4"]


def test_metadata_only_edits_do_not_rewrite_the_body():
    """A notebook move is provenance, not content — the row body is untouched."""
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))

    store.edit_note("g1", title="Alpha renamed", notebook_guid="nb-2")
    delta, _, _ = _rows(store, state=state)

    assert [r["id"] for r in delta] == ["g1"]
    assert delta[0]["content"] == "alpha note", "body must be stable across a rename"
    assert delta[0]["title"] == "Alpha renamed"
    assert delta[0]["notebook"] == "Personal"


def test_notebook_rename_on_the_feed_updates_provenance():
    store = _seeded_store()
    state = {}
    live, _, _ = _rows(store, state=state)
    assert {r["id"]: r["notebook"] for r in live}["g1"] == "Work"

    store.rename_notebook("nb-1", "Work (renamed)")
    store.edit_note("g1", "<div>alpha note</div>")
    delta, _, _ = _rows(store, state=state)
    assert delta[0]["notebook"] == "Work (renamed)"


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_expunged_note_is_tombstoned_incrementally():
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))

    store.expunge_note("g2")
    live, tombs, state2 = _rows(store, state=state)

    assert live == [], "an expunged note must not be re-ingested"
    assert tombs == ["g2"]
    assert "g2" not in state2["known_ids"]


def test_trashing_a_note_tombstones_it_rather_than_reingesting_it():
    """Evernote's Trash is a real notebook — a naive sync would resurrect the note."""
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))

    store.trash_note("g2")
    live, tombs, _ = _rows(store, state=state)

    assert live == [], "a trashed note must not be ingested"
    assert tombs == ["g2"]


def test_restoring_a_trashed_note_reingests_it():
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))
    store.trash_note("g2")
    _rows(store, state=state)

    store.restore_note("g2")
    live, tombs, state2 = _rows(store, state=state)

    assert [r["id"] for r in live] == ["g2"]
    assert tombs == []
    assert "g2" in state2["known_ids"]


def test_vanishing_between_chunk_and_content_fetch_tombstones_the_note():
    """A note expunged mid-sync is forgotten, not a crash."""
    store = _seeded_store()
    state = {}

    original = store.get_note_content

    def racing_get(guid):
        if guid == "g2":
            store.expunge_note("g2")
        return original(guid)

    store.get_note_content = racing_get
    live, tombs, _ = _rows(store, state=state)

    assert "g2" in tombs
    assert "g2" not in [r["id"] for r in live]


# ---------------------------------------------------------------------------
# Scope changes reconcile out-of-scope notes
# ---------------------------------------------------------------------------
def test_narrowing_the_notebook_scope_tombstones_dropped_notes():
    store = _seeded_store()
    state = {}
    live, _, _ = _rows(store, state=state)
    assert len(live) == 3
    original_scope_key = state["scope_key"]

    narrowed = _config(notebook_guids=("nb-1",))
    live, tombs, state2 = _rows(store, config=narrowed, state=state)

    assert sorted(r["id"] for r in live) == ["g1", "g3"], "only nb-1 stays"
    assert tombs == ["g2"], "the note that left the scope must be forgotten"
    assert state2["scope_key"] != original_scope_key, "the selection is now different"
    assert state2["known_ids"] == ["g1", "g3"], "the dropped note must not linger"


def test_scope_change_forces_a_full_rescan_and_keeps_going_forward():
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))

    narrowed = _config(tag_names=("work",))
    store.add_note("g4", "<div>tagged</div>", notebook_guid="nb-1", tags=["work"])
    live, _, _ = _rows(store, config=narrowed, state=state)
    assert [r["id"] for r in live] == ["g4"]

    # And a subsequent unchanged run on the new scope is a no-op.
    live, tombs, _ = _rows(store, config=narrowed, state=state)
    assert live == []
    assert tombs == []


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------
def test_empty_scan_does_not_mass_delete():
    """A transient failure must not look like 'the user deleted everything'."""
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))

    # Wipe the account but keep the cursor and known ids (a failed listing).
    store.notes.clear()
    store.content.clear()
    store.note_usn.clear()

    live, tombs, state2 = _rows(store, state=state)

    assert tombs == [], "no deletion may be inferred from an empty scan"
    assert sorted(state2["known_ids"]) == ["g1", "g2", "g3"], "state must be preserved"
    assert live == []


def test_empty_full_rescan_does_not_mass_delete():
    """The guard must also hold on the full re-scan a scope change forces.

    This is the reachable path to the guard: an incremental run never enumerates
    the live set, so the "everything looks deleted" case can only arise during a
    full scan — which a changed selection triggers.
    """
    store = _seeded_store()
    state = {}
    list(sync_notes(store, _config(), state))
    assert sorted(state["known_ids"]) == ["g1", "g2", "g3"]

    # The selection changes, so the next run re-scans from USN 0 — and that scan
    # transiently fails, returning nothing.
    store.notes.clear()
    store.content.clear()
    store.note_usn.clear()

    live, tombs, state2 = _rows(store, config=_config(notebook_guids=("nb-1",)), state=state)

    assert tombs == [], "a failed re-scan must not tombstone the whole corpus"
    assert sorted(state2["known_ids"]) == ["g1", "g2", "g3"], "state must be preserved"
    assert live == []


def test_a_non_advancing_chunk_stops_the_scan_instead_of_looping_forever():
    from .fake_evernote import make_chunk

    class StuckStore(FakeEvernoteStore):
        def get_sync_chunk(self, after_usn, max_entries):
            return make_chunk(chunk_high_usn=after_usn, update_count=99)

    store = StuckStore()
    store.add_note("g1", "<div>x</div>")
    live, _, _ = _rows(store)
    assert live == []


def test_chunk_limit_is_forwarded_to_the_api():
    store = _seeded_store()
    list(sync_notes(store, _config(chunk_size=7), {}))
    sizes = {args[1] for name, args in store.calls if name == "get_sync_chunk"}
    assert sizes == {7}, "every chunk request must use the caller's max_entries"


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------
def test_save_and_load_token_roundtrip(tmp_path):
    path = str(tmp_path / "tok.json")
    save_token(TOKEN, path, sandbox=True)
    assert load_cached_token(path) == TOKEN

    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["sandbox"] is True


def test_load_token_returns_none_when_missing(tmp_path):
    assert load_cached_token(str(tmp_path / "absent.json")) is None


def test_resolve_auth_token_prefers_argument_then_env_then_cache(tmp_path, monkeypatch):
    path = str(tmp_path / "tok.json")
    save_token("cached", path)

    monkeypatch.delenv("EVERNOTE_AUTH_TOKEN", raising=False)
    assert resolve_auth_token(None, path) == "cached"

    monkeypatch.setenv("EVERNOTE_AUTH_TOKEN", "from-env")
    assert resolve_auth_token(None, path) == "from-env"
    assert resolve_auth_token("explicit", path) == "explicit"


def test_resolve_auth_token_errors_with_actionable_message(tmp_path, monkeypatch):
    monkeypatch.delenv("EVERNOTE_AUTH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="authorize"):
        resolve_auth_token(None, str(tmp_path / "absent.json"))


# ---------------------------------------------------------------------------
# EDAM error classification
# ---------------------------------------------------------------------------
class _EdamError(Exception):
    def __init__(self, code, **extra):
        super().__init__(f"EDAM error {code}")
        self.errorCode = code
        for key, value in extra.items():
            setattr(self, key, value)


@pytest.mark.parametrize(
    ("code", "family"),
    [
        (1, "auth"),  # PERMISSION_DENIED
        (4, "auth"),  # AUTHENTICATION_ERROR
        (5, "not_found"),  # ITEM_NOT_FOUND
        (6, "rate_limit"),  # RATE_LIMIT_REACHED
        (2, "transient"),  # SYSTEM_ERROR
        (99, "fatal"),
    ],
)
def test_error_family_classifies_edam_codes(code, family):
    assert _error_family(_EdamError(code)) == family


def test_error_family_treats_transport_failures_as_transient():
    assert _error_family(TimeoutError()) == "transient"
    assert _error_family(RuntimeError("protocol error")) == "fatal"


def test_retry_delay_prefers_the_servers_hint():
    assert _retry_delay(_EdamError(6, rateLimitWaitSeconds=42), 0) == 42.0
    assert _retry_delay(_EdamError(6, rateLimitWaitSeconds=9999), 0) == 60.0, "capped"
    assert _retry_delay(_EdamError(6), 2) == 4.0, "exponential backoff without a hint"


def test_store_translates_an_auth_error_into_a_recoverable_one():
    """A rejected token must say how to fix it, not surface as a raw Thrift error."""

    class Client:
        def getFilteredSyncChunk(self, *_args):
            raise _EdamError(4)

    store = EvernoteNoteStore(TOKEN)
    store._client = Client()
    with pytest.raises(EvernoteAuthError, match="authorize"):
        store.get_sync_chunk(0, 270)


def test_store_translates_a_gone_object_into_not_found():
    from cognee_community_connector_evernote.evernote import EvernoteNotFoundError

    class Client:
        def getSyncState(self, _token):
            raise _EdamError(5)

    store = EvernoteNoteStore(TOKEN)
    store._client = Client()
    with pytest.raises(EvernoteNotFoundError):
        store.get_sync_state()


def test_store_retries_transient_errors_then_succeeds(monkeypatch):
    store = EvernoteNoteStore(TOKEN, content_throttle=0)
    attempts = {"n": 0}

    class Client:
        def getSyncState(self, _token):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _EdamError(2)
            return 77

    store._client = Client()
    monkeypatch.setattr("cognee_community_connector_evernote.evernote.time.sleep", lambda _s: None)
    assert store.get_sync_state() == 77
    assert attempts["n"] == 3


# ---------------------------------------------------------------------------
# dlt wiring
# ---------------------------------------------------------------------------
def test_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    resource = evernote_source(TOKEN, store=object())
    assert resource.name == EVERNOTE_TABLE_NAME

    schema = resource.compute_table_schema()
    disposition = schema.get("write_disposition")
    if isinstance(disposition, dict):  # dlt may normalise to a config dict
        disposition = disposition.get("disposition")
    assert disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_source_declares_document_mode():
    pytest.importorskip("dlt")

    resource = evernote_source(TOKEN, store=object())
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == EVERNOTE_SOURCE_NAME


def test_source_requires_a_token(monkeypatch, tmp_path):
    pytest.importorskip("dlt")
    monkeypatch.delenv("EVERNOTE_AUTH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        evernote_source(token_path=str(tmp_path / "absent.json"))


def test_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee"):
        evernote_source(TOKEN, store=object())
