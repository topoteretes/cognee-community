"""The sync state machine (_iter_rows) against the in-memory fake Basecamp.

``state`` is a plain dict standing in for dlt's resource state, the same trick
the Google Drive connector uses, so no pipeline is needed here.
"""

from cognee_community_connector_basecamp.basecamp import BasecampClient, _iter_rows, _SyncConfig

UA = "cognee-basecamp-tests (test@example.com)"


def _config(**overrides) -> _SyncConfig:
    values = {
        "types": ("Message", "Todo", "Document", "Comment"),
        "project_ids": (),
        "full_sync": False,
        "full_sync_every": 0,  # sweeps only when a test asks for one
    }
    values.update(overrides)
    return _SyncConfig(**values)


def _run(fake, state, **config):
    client = BasecampClient("999", "token", UA, http_client=fake.http_client())
    rows = list(_iter_rows(client, _config(**config), state))
    live = {r["id"]: r for r in rows if not r["_deleted"]}
    deleted = {r["id"] for r in rows if r["_deleted"]}
    return live, deleted


def test_first_run_loads_everything_including_completed_todos(seeded):
    state = {}
    live, deleted = _run(seeded, state)
    kinds = sorted(rid.split(":")[0] for rid in live)
    assert kinds == ["comment", "comment", "document", "message", "todo", "todo"]
    assert deleted == set()
    assert any("Completed: yes" in r["content"] for r in live.values())
    assert set(state["cursors"]) == {"Message", "Todo", "Document", "Comment"}


def test_second_run_without_changes_yields_nothing(seeded):
    state = {}
    _run(seeded, state)
    live, deleted = _run(seeded, state)
    assert live == {}
    assert deleted == set()


def test_unchanged_lists_are_skipped_with_304(seeded):
    state = {}
    _run(seeded, state)
    seeded.requests.clear()
    _run(seeded, state)
    # The trashed listings send no etag; active and archived get 304s.
    sent_etag = [r for r in seeded.requests if r.headers.get("if-none-match")]
    assert len(sent_etag) == 8  # 4 types x (active, archived)


def test_edit_is_picked_up_incrementally(seeded):
    state = {}
    first, _ = _run(seeded, state)
    doc_id = next(int(rid.split(":")[1]) for rid in first if rid.startswith("document:"))

    seeded.edit(doc_id, content='<p dir="auto">Start with the <strong>prod</strong> setup.</p>')
    live, deleted = _run(seeded, state)
    assert list(live) == [f"document:{doc_id}"]
    assert "prod setup" in live[f"document:{doc_id}"]["content"]
    assert deleted == set()


def test_trashed_item_becomes_a_tombstone(seeded):
    state = {}
    first, _ = _run(seeded, state)
    doc_id = next(int(rid.split(":")[1]) for rid in first if rid.startswith("document:"))

    seeded.set_status(doc_id, "trashed")
    _live, deleted = _run(seeded, state)
    assert deleted == {f"document:{doc_id}"}
    assert f"document:{doc_id}" not in state["known_ids"]["Document"]


def test_completed_todo_is_kept_but_trashed_todo_is_forgotten(seeded):
    state = {}
    _run(seeded, state)
    todos = {r["title"]: r["id"] for r in seeded.recordings.values() if r["type"] == "Todo"}

    seeded.edit(todos["Fix login bug"], completed=True)
    seeded.set_status(todos["Write release notes"], "trashed")
    live, deleted = _run(seeded, state)

    assert "Completed: yes" in live[f"todo:{todos['Fix login bug']}"]["content"]
    assert f"todo:{todos['Fix login bug']}" not in deleted
    assert f"todo:{todos['Write release notes']}" in deleted


def test_archived_item_is_kept_with_its_comment(seeded):
    state = {}
    first, _ = _run(seeded, state)
    msg_id = next(int(rid.split(":")[1]) for rid in first if rid.startswith("message:"))

    seeded.set_status(msg_id, "archived")
    live, deleted = _run(seeded, state)
    assert deleted == set()
    assert "Status: archived" in live[f"message:{msg_id}"]["content"]

    # A full sweep must also treat archived items (and their comments) as present.
    _live, deleted = _run(seeded, state, full_sync=True)
    assert deleted == set()


def test_purged_item_is_forgotten_on_the_next_sweep(seeded):
    state = {}
    first, _ = _run(seeded, state)
    doc_id = next(int(rid.split(":")[1]) for rid in first if rid.startswith("document:"))

    # Trashed and emptied before any sync saw it: gone from every listing.
    seeded.set_status(doc_id, "trashed")
    seeded.purge(doc_id)

    _live, deleted = _run(seeded, state)
    assert deleted == set()  # an incremental run cannot see it

    _live, deleted = _run(seeded, state, full_sync=True)
    assert deleted == {f"document:{doc_id}"}


def test_sweep_runs_every_n_runs(seeded):
    state = {}
    _run(seeded, state, full_sync_every=3)  # run 1: first run is always a sweep
    _run(seeded, state, full_sync_every=3)  # run 2: incremental
    doc_id = next(r["id"] for r in seeded.recordings.values() if r["type"] == "Document")
    seeded.purge(doc_id)
    _live, deleted = _run(seeded, state, full_sync_every=3)  # run 3: sweep
    assert deleted == {f"document:{doc_id}"}


def test_project_filter_is_sent_as_bucket(seeded):
    _run(seeded, {}, project_ids=("1", "7"))
    assert all(r.url.params.get("bucket") == "1,7" for r in seeded.requests)
