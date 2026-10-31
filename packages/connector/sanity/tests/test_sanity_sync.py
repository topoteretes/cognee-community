"""Sync state machine tests: _iter_rows against an in-memory Sanity dataset.

No network, no LLM. Each test drives the fake through a real-world change and
checks the rows the connector yields and the state it keeps for the next run.
"""

import pytest
from fake_sanity import FakeSanity, block, ref

from cognee_community_connector_sanity.sanity import SanityError, _iter_rows, _Scope

SCOPE = _Scope(project_id="proj1", dataset="production", document_types=("article", "author"))


def rid(doc_id, scope=SCOPE):
    return f"sanity:{scope.project_id}:{scope.dataset}:{doc_id}"


def sync(fake, state, scope=SCOPE):
    stats: dict = {}
    rows = list(_iter_rows(fake, scope, state, stats))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = sorted(row["id"] for row in rows if row["_deleted"])
    return upserts, deletes, stats


@pytest.fixture
def fake():
    f = FakeSanity()
    f.put("author-alice", "author", name="Alice")
    f.put(
        "article-1",
        "article",
        title="Payments team owns billing",
        author=ref("author-alice"),
        body=[block("Escalate billing incidents to them.")],
    )
    f.put(
        "article-2", "article", title="Reset your password", body=[block("Click Forgot password.")]
    )
    return f


def test_first_sync_renders_every_selected_document(fake):
    state: dict = {}
    upserts, deletes, stats = sync(fake, state)

    assert set(upserts) == {rid("author-alice"), rid("article-1"), rid("article-2")}
    assert deletes == []
    row = upserts[rid("article-1")]
    assert row["title"] == "Payments team owns billing"
    assert "Author: Alice" in row["content"], "references are resolved to names"
    assert "Escalate billing incidents to them." in row["content"]
    assert row["cognee_node_set"] == ["article"]
    assert stats["listed"] == 3 and stats["changed"] == 3
    assert state["source"] == "proj1:production"


def test_unchanged_run_fetches_and_yields_nothing(fake):
    state: dict = {}
    sync(fake, state)
    fake.calls.clear()

    upserts, deletes, _ = sync(fake, state)

    assert upserts == {} and deletes == []
    assert ("get_documents",) not in fake.calls and not any(
        c[0] == "get_documents" for c in fake.calls
    ), "no document content is read when nothing changed"


def test_documents_written_in_the_same_second_are_all_picked_up(fake):
    # article-1 was synced at 10:00:00; two more writes land in that same second.
    state: dict = {}
    sync(fake, state)
    fake.patch("article-2", title="Reset your password (updated)")
    fake.put("article-3", "article", title="Q4 migration plan")

    upserts, _, _ = sync(fake, state)

    assert set(upserts) == {rid("article-2"), rid("article-3")}, (
        "same-second writes are found by _rev, so a timestamp tie cannot hide them"
    )


def test_an_edit_re_renders_only_that_document(fake):
    state: dict = {}
    sync(fake, state)
    fake.patch("article-2", body=[block("Use the reset link in the email.")])

    upserts, deletes, _ = sync(fake, state)

    assert list(upserts) == [rid("article-2")]
    assert "Use the reset link in the email." in upserts[rid("article-2")]["content"]
    assert deletes == []


def test_renaming_a_referenced_author_re_renders_the_articles_that_point_to_it(fake):
    # The article's own _rev does not change when its author is renamed.
    state: dict = {}
    sync(fake, state)
    fake.patch("author-alice", name="Alice Smith")

    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {rid("author-alice"), rid("article-1")}
    assert "Author: Alice Smith" in upserts[rid("article-1")]["content"]
    assert rid("article-2") not in upserts, "an article without that author stays as it is"
    assert stats["reference_updates"] == 1


def test_a_referenced_document_outside_the_selected_types_is_watched_too(fake):
    scope = _Scope(project_id="proj1", dataset="production", document_types=("article",))
    state: dict = {}
    sync(fake, state, scope)
    fake.patch("author-alice", name="Alice Smith")

    upserts, _, _ = sync(fake, state, scope)

    assert set(upserts) == {rid("article-1", scope)}
    assert "Author: Alice Smith" in upserts[rid("article-1", scope)]["content"]


def test_a_deleted_reference_target_is_shown_as_missing_once(fake):
    scope = _Scope(project_id="proj1", dataset="production", document_types=("article",))
    state: dict = {}
    sync(fake, state, scope)
    fake.delete("author-alice")

    upserts, _, _ = sync(fake, state, scope)
    assert "Author: (missing reference)" in upserts[rid("article-1", scope)]["content"]

    again, _, _ = sync(fake, state, scope)
    assert again == {}, "a missing target must not re-render the article on every run"


def test_a_deleted_document_is_forgotten(fake):
    state: dict = {}
    sync(fake, state)
    fake.delete("article-2")

    upserts, deletes, stats = sync(fake, state)

    assert deletes == [rid("article-2")]
    assert upserts == {}
    assert stats["deleted"] == 1
    assert "article-2" not in state["revs"]


def test_drafts_never_reach_cognee_and_unpublishing_forgets(fake):
    state: dict = {}
    sync(fake, state)
    fake.put("drafts.article-1", "article", title="DRAFT: not ready")
    fake.put("drafts.article-9", "article", title="A draft never published")

    upserts, deletes, _ = sync(fake, state)
    assert upserts == {} and deletes == [], "drafts are invisible to the published view"

    fake.delete("article-1")  # unpublish: only the draft is left
    _, deletes, _ = sync(fake, state)
    assert deletes == [rid("article-1")]


def test_system_and_unselected_documents_are_ignored(fake):
    fake.put("_.groups.public", "system.group", members=["everyone"])
    fake.put("settings-1", "siteSettings", title="Site settings")
    upserts, _, _ = sync(fake, {})
    assert rid("settings-1") not in upserts and rid("_.groups.public") not in upserts


def test_dropping_a_type_from_the_selection_forgets_its_documents(fake):
    state: dict = {}
    sync(fake, state)
    narrower = _Scope(project_id="proj1", dataset="production", document_types=("article",))

    _, deletes, _ = sync(fake, state, narrower)

    assert deletes == [rid("author-alice", narrower)]


def test_a_failed_listing_yields_nothing_and_keeps_the_state(fake):
    state: dict = {}
    sync(fake, state)
    before = {k: (dict(v) if isinstance(v, dict) else v) for k, v in state.items()}
    fake.delete("article-2")
    fake.fail["list_revisions"] = ConnectionError("network down")

    with pytest.raises(ConnectionError):
        sync(fake, state)

    assert state == before, "a failed run must not move the state"
    del fake.fail["list_revisions"]
    _, deletes, _ = sync(fake, state)
    assert deletes == [rid("article-2")], "the delete is picked up on the next good run"


def test_a_failed_fetch_after_listing_yields_nothing(fake):
    state: dict = {}
    sync(fake, state)
    fake.delete("article-2")
    fake.patch("article-1", title="Edited")
    fake.fail["get_documents"] = TimeoutError("slow")

    rows = []
    with pytest.raises(TimeoutError):
        rows.extend(_iter_rows(fake, SCOPE, state, {}))
    assert rows == [], "no tombstone is emitted before every request has succeeded"


def test_a_document_deleted_between_listing_and_fetch_is_forgotten(fake):
    state: dict = {}
    sync(fake, state)
    fake.patch("article-2", title="Edited")
    original = fake.get_documents

    def vanish(ids):
        fake.delete("article-2")
        return original(ids)

    fake.get_documents = vanish
    _, deletes, _ = sync(fake, state)
    assert deletes == [rid("article-2")]


def test_state_from_another_project_is_refused(fake):
    state: dict = {}
    sync(fake, state)
    other = _Scope(project_id="proj2", dataset="production", document_types=("article",))
    with pytest.raises(ValueError, match="belongs to Sanity 'proj1:production'"):
        sync(fake, state, other)


def test_the_deployed_schema_becomes_one_document_and_follows_changes(fake):
    fake.deploy_schema(
        [
            {
                "name": "article",
                "title": "Article",
                "type": "document",
                "fields": [
                    {"name": "title", "type": "string"},
                    {"name": "author", "type": "reference", "to": [{"type": "author"}]},
                    {"name": "body", "type": "array", "of": [{"type": "block"}]},
                ],
            },
            {"name": "author", "type": "document", "fields": [{"name": "name", "type": "string"}]},
            {
                "name": "siteSettings",
                "type": "document",
                "fields": [{"name": "x", "type": "string"}],
            },
        ]
    )
    state: dict = {}
    upserts, _, stats = sync(fake, state)

    schema = upserts[rid("_.schemas.default")]
    assert "Document type article (Article)" in schema["content"]
    assert "- author: reference to author" in schema["content"]
    assert "- body: rich text (Portable Text)" in schema["content"]
    assert "siteSettings" not in schema["content"], "only the selected types are described"
    assert stats["schema"] == 1

    again, _, _ = sync(fake, state)
    assert rid("_.schemas.default") not in again, "an unchanged schema is not re-ingested"

    # Without a working token the schema (a system document) reads as missing,
    # even in a public dataset, so a missing schema keeps the synced one.
    fake.schema = None
    _, deletes, _ = sync(fake, state)
    assert deletes == [], "an unreadable schema is not forgotten"

    # Turning the schema off is what forgets it.
    off = _Scope(**{**SCOPE.__dict__, "include_schema": False})
    _, deletes, _ = sync(fake, state, off)
    assert deletes == [rid("_.schemas.default")]


def test_a_project_without_a_deployed_schema_just_skips_it(fake):
    upserts, deletes, stats = sync(fake, {})
    assert rid("_.schemas.default") not in upserts and deletes == []
    assert stats["schema"] == 0


def test_include_schema_false_never_asks_for_it(fake):
    scope = _Scope(
        project_id="proj1", dataset="production", document_types=("article",), include_schema=False
    )
    sync(fake, {}, scope)
    assert ("get_schema",) not in fake.calls


def test_an_empty_listing_never_forgets_everything(fake):
    state: dict = {}
    sync(fake, state)
    before = dict(state)

    # What a private dataset answers without a working token, or a mistyped type.
    for doc_id in ("author-alice", "article-1", "article-2"):
        fake.delete(doc_id)
    stats: dict = {}
    with pytest.raises(SanityError, match="nothing is forgotten"):
        list(_iter_rows(fake, SCOPE, state, stats))
    assert state == before, "the state keeps every synced document"
    assert stats["failed"] == 1


def test_an_empty_listing_forgets_when_allowed(fake):
    state: dict = {}
    sync(fake, state)
    for doc_id in ("author-alice", "article-1", "article-2"):
        fake.delete(doc_id)

    allowed = _Scope(**{**SCOPE.__dict__, "allow_empty_listing": True})
    upserts, deletes, _ = sync(fake, state, allowed)
    assert upserts == {}
    assert deletes == sorted(rid(d) for d in ("article-1", "article-2", "author-alice"))


def test_an_empty_first_sync_is_not_an_error():
    upserts, deletes, stats = sync(FakeSanity(), {})
    assert (upserts, deletes, stats["failed"]) == ({}, [], 0)


def test_rendering_does_not_depend_on_field_order():
    forward = FakeSanity()
    forward.put("a1", "article", title="T", summary="S", meta={"tag": "x", "level": 2})
    backward = FakeSanity()
    backward.put("a1", "article", meta={"level": 2, "tag": "x"}, summary="S", title="T")

    first, _, _ = sync(forward, {})
    second, _, _ = sync(backward, {})
    assert first[rid("a1")]["content"] == second[rid("a1")]["content"]
