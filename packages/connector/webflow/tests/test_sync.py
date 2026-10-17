"""The sync state machine against a fake Webflow API (see fake_webflow.py).

Covers the ingest path (items, collection cards, pages), the lastUpdated cursor
(unchanged items, edits, schema changes, full refresh) and forget-on-delete
(deleted, unpublished and archived items, removed collections and pages), plus
the rule that a failed or inconsistent read changes nothing.
"""

import pytest
from fake_webflow import TOKEN, FakeWebflow

from cognee_community_connector_webflow import WebflowAPIError, WebflowClient
from cognee_community_connector_webflow.webflow import _Selection, sync_rows

FIELDS = [
    {"slug": "name", "displayName": "Name", "type": "PlainText"},
    {"slug": "slug", "displayName": "Slug", "type": "PlainText"},
    {"slug": "body", "displayName": "Body", "type": "RichText"},
    {
        "slug": "category",
        "displayName": "Category",
        "type": "Option",
        "validations": {"options": [{"id": "o1", "name": "News"}]},
    },
]


class Run:
    """The rows, tombstones and stats of one sync."""

    def __init__(self, emitted, stats):
        self.rows: dict[str, dict] = {}
        self.deleted: set[str] = set()
        self.tables: dict[str, str] = {}
        for table, row in emitted:
            if row["_deleted"]:
                self.deleted.add(row["id"])
            else:
                self.rows[row["id"]] = row
            self.tables[row["id"]] = table
        self.stats = stats

    @property
    def items(self) -> set[str]:
        return {row_id.split(":")[-1] for row_id in self.rows if ":item:" in row_id}

    @property
    def pages(self) -> set[str]:
        return {row_id.split(":")[-1] for row_id in self.rows if ":page:" in row_id}

    @property
    def cards(self) -> set[str]:
        return {row_id.split(":")[-1] for row_id in self.rows if ":collection:" in row_id}

    @property
    def forgotten(self) -> set[str]:
        return {row_id.split(":")[-1] for row_id in self.deleted}


def selection(**overrides) -> _Selection:
    options = {
        "site_ids": None,
        "collections": None,
        "include_pages": False,
        "include_drafts": False,
        "include_collection_cards": True,
        "full_refresh": False,
        "resource_name": "webflow",
    }
    return _Selection(**{**options, **overrides})


def sync(fake, state, **overrides) -> Run:
    client = WebflowClient(TOKEN, transport=fake.transport(), sleep=lambda _s: None)
    stats: dict = {}
    try:
        emitted = list(sync_rows(client, selection(**overrides), state, stats=stats))
    finally:
        client.close()
    return Run(emitted, stats)


@pytest.fixture
def fake():
    fake = FakeWebflow()
    fake.site = fake.add_site("Acme Kettles", "acme-kettles")
    fake.posts = fake.add_collection(fake.site, "posts", "Blog Posts", "Blog Post", FIELDS)
    fake.alpha = fake.add_item(fake.posts, "Alpha", body="<p>First post.</p>", category="o1")
    fake.bravo = fake.add_item(fake.posts, "Bravo", body="<p>Second post.</p>")
    return fake


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def test_first_sync_renders_items_and_a_collection_card(fake):
    run = sync(fake, {})

    assert run.items == {fake.alpha, fake.bravo}
    assert run.cards == {fake.posts}
    alpha = run.rows[f"webflow:item:{fake.alpha}"]
    assert alpha["title"] == "Alpha"
    assert alpha["content"].startswith(
        'Webflow Blog Post "Alpha" (collection: Blog Posts, site: Acme Kettles)'
    )
    assert "Category: News" in alpha["content"]
    assert "Body: First post." in alpha["content"]
    card = run.rows[f"webflow:collection:{fake.posts}"]["content"]
    assert "- Category (Option: News)" in card
    assert run.stats["items"] == 2 and run.stats["collections"] == 1


def test_items_and_their_card_share_a_node_set(fake):
    run = sync(fake, {})

    node_sets = {tuple(row["cognee_node_set"]) for row in run.rows.values()}
    assert node_sets == {("webflow:acme-kettles/posts",)}


def test_each_collection_has_its_own_staging_table(fake):
    docs = fake.add_collection(fake.site, "docs", fields=FIELDS)
    guide = fake.add_item(docs, "Guide")

    run = sync(fake, {})

    assert run.tables[f"webflow:item:{fake.alpha}"] == f"webflow_items_{fake.posts}"
    assert run.tables[f"webflow:item:{guide}"] == f"webflow_items_{docs}"
    assert run.tables[f"webflow:collection:{docs}"] == "webflow_collection_cards"


def test_only_published_items_are_synced_by_default(fake):
    fake.add_item(fake.posts, "Secret draft", draft=True)
    fake.add_item(fake.posts, "Old news", archived=True)

    run = sync(fake, {})

    assert run.items == {fake.alpha, fake.bravo}
    assert fake.requests_to("/items/live")
    assert not [c for c in fake.calls if c[0].endswith("/items")]


def test_include_drafts_reads_staged_items_but_not_archived_ones(fake):
    draft = fake.add_item(fake.posts, "Secret draft", draft=True)
    fake.add_item(fake.posts, "Old news", archived=True)

    run = sync(fake, {}, include_drafts=True)

    assert run.items == {fake.alpha, fake.bravo, draft}
    assert "Status: draft" in run.rows[f"webflow:item:{draft}"]["content"]


def test_a_large_collection_is_read_in_pages_of_100(fake):
    for number in range(230):
        fake.add_item(fake.posts, f"Post {number}")

    run = sync(fake, {})

    assert len(run.items) == 232
    offsets = [c[1]["offset"] for c in fake.requests_to("/items/live")]
    assert offsets == ["0", "100", "200"]


def test_cards_can_be_turned_off(fake):
    run = sync(fake, {}, include_collection_cards=False)

    assert run.cards == set()
    assert run.items == {fake.alpha, fake.bravo}


def test_collections_are_selected_by_slug_id_or_name(fake):
    docs = fake.add_collection(fake.site, "docs", "Help Docs", fields=FIELDS)
    fake.add_item(docs, "Guide")

    assert sync(fake, {}, collections=("posts",)).cards == {fake.posts}
    assert sync(fake, {}, collections=(docs,)).cards == {docs}
    assert sync(fake, {}, collections=("Help Docs", "posts")).cards == {docs, fake.posts}


def test_an_unknown_collection_is_refused_and_nothing_is_read_or_forgotten(fake):
    state: dict = {}
    sync(fake, state)
    before = {**state}

    with pytest.raises(ValueError, match="'typo' was not found"):
        sync(fake, state, collections=("typo",))

    assert state == before


def test_every_site_the_token_can_see_is_synced(fake):
    other = fake.add_site("Second", "second")
    notes = fake.add_collection(other, "notes", fields=FIELDS)
    note = fake.add_item(notes, "Note")

    run = sync(fake, {})

    assert note in run.items and fake.alpha in run.items


def test_site_ids_limit_the_sync_to_those_sites(fake):
    other = fake.add_site("Second", "second")
    notes = fake.add_collection(other, "notes", fields=FIELDS)
    fake.add_item(notes, "Note")

    run = sync(fake, {}, site_ids=(other,))

    assert run.cards == {notes}


def test_a_token_without_sites_read_can_still_sync_a_given_site(fake):
    fake.fail("/sites/", 403, code="missing_scopes")

    run = sync(fake, {}, site_ids=(fake.site,))

    assert run.items == {fake.alpha, fake.bravo}
    assert "site: " + fake.site in run.rows[f"webflow:item:{fake.alpha}"]["content"]


def test_an_unknown_site_id_is_refused(fake):
    with pytest.raises(ValueError, match="was not found"):
        sync(fake, {}, site_ids=("deadbeef",))


def test_a_token_that_sees_no_sites_is_refused(fake):
    fake.sites.clear()

    with pytest.raises(ValueError, match="can see no sites"):
        sync(fake, {})


def test_localized_variants_sharing_an_id_keep_the_first(fake):
    variant = dict(fake.items[fake.posts][0], cmsLocaleId="locale2")
    fake.items[fake.posts].append(variant)

    run = sync(fake, {})

    assert run.items == {fake.alpha, fake.bravo}
    assert run.stats["skipped"] == 1


# ---------------------------------------------------------------------------
# lastUpdated cursor
# ---------------------------------------------------------------------------
def test_an_unchanged_resync_emits_no_items(fake):
    state: dict = {}
    sync(fake, state)

    run = sync(fake, state)

    assert run.items == set()
    assert run.stats["unchanged"] == 2 and run.stats["items"] == 0
    assert run.deleted == set()


def test_only_the_edited_item_is_emitted_again(fake):
    state: dict = {}
    sync(fake, state)
    fake.edit_item(fake.posts, fake.alpha, body="<p>Edited.</p>")

    run = sync(fake, state)

    assert run.items == {fake.alpha}
    assert "Body: Edited." in run.rows[f"webflow:item:{fake.alpha}"]["content"]


def test_new_items_are_picked_up(fake):
    state: dict = {}
    sync(fake, state)
    charlie = fake.add_item(fake.posts, "Charlie")

    assert sync(fake, state).items == {charlie}


def test_a_save_that_changes_nothing_renders_the_same_document(fake):
    first = sync(fake, state := {})
    fake.touch_item(fake.posts, fake.alpha)

    run = sync(fake, state)

    # The bumped lastUpdated sends the item again, but its text is identical,
    # so cognee's content hash skips the LLM.
    assert run.items == {fake.alpha}
    key = f"webflow:item:{fake.alpha}"
    assert run.rows[key]["content"] == first.rows[key]["content"]


def test_a_schema_change_renders_every_item_again(fake):
    state: dict = {}
    sync(fake, state)
    fake.collections[fake.posts]["fields"][3]["validations"]["options"][0]["name"] = "Press"

    run = sync(fake, state)

    assert run.items == {fake.alpha, fake.bravo}
    assert "Category: Press" in run.rows[f"webflow:item:{fake.alpha}"]["content"]


def test_full_refresh_sends_every_item_again(fake):
    state: dict = {}
    sync(fake, state)

    assert sync(fake, state, full_refresh=True).items == {fake.alpha, fake.bravo}


def test_state_from_another_rendering_is_re_read_but_still_drives_deletions(fake):
    state: dict = {}
    sync(fake, state)
    state["version"] = 0
    fake.delete_item(fake.posts, fake.bravo)

    run = sync(fake, state)

    assert run.items == {fake.alpha}
    assert run.forgotten == {fake.bravo}


def test_a_changed_item_is_not_lost_when_the_run_fails_before_it_commits(fake):
    state: dict = {}
    sync(fake, state)
    fake.edit_item(fake.posts, fake.alpha, body="<p>Edited.</p>")
    fake.fail(f"/collections/{fake.posts}", 500, times=50)
    before = {**state}

    with pytest.raises(WebflowAPIError):
        sync(fake, state)

    assert state == before
    fake._failures.clear()
    assert sync(fake, state).items == {fake.alpha}


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_a_deleted_item_is_forgotten_and_the_rest_stay(fake):
    state: dict = {}
    sync(fake, state)
    fake.delete_item(fake.posts, fake.bravo)

    run = sync(fake, state)

    assert run.forgotten == {fake.bravo}
    assert run.tables[f"webflow:item:{fake.bravo}"] == f"webflow_items_{fake.posts}"
    assert run.items == set()
    assert fake.alpha in state["collections"][fake.posts]["stamps"]
    assert fake.bravo not in state["collections"][fake.posts]["stamps"]


def test_an_unpublished_item_is_forgotten(fake):
    state: dict = {}
    sync(fake, state)
    fake.unpublish_item(fake.posts, fake.bravo)

    assert sync(fake, state).forgotten == {fake.bravo}


def test_an_unpublished_item_stays_when_drafts_are_included(fake):
    state: dict = {}
    sync(fake, state, include_drafts=True)
    fake.unpublish_item(fake.posts, fake.bravo)

    run = sync(fake, state, include_drafts=True)

    assert run.forgotten == set()
    assert "Status: draft" in run.rows[f"webflow:item:{fake.bravo}"]["content"]


def test_an_archived_item_is_forgotten_even_with_drafts(fake):
    state: dict = {}
    sync(fake, state, include_drafts=True)
    fake.archive_item(fake.posts, fake.bravo)

    assert sync(fake, state, include_drafts=True).forgotten == {fake.bravo}


def test_a_collection_deleted_upstream_is_forgotten_whole(fake):
    docs = fake.add_collection(fake.site, "docs", fields=FIELDS)
    guide = fake.add_item(docs, "Guide")
    state: dict = {}
    sync(fake, state)
    fake.delete_collection(docs)

    run = sync(fake, state)

    assert run.forgotten == {guide, docs}
    assert run.tables[f"webflow:item:{guide}"] == f"webflow_items_{docs}"
    assert fake.alpha not in run.forgotten
    assert docs not in state["collections"] and docs not in state["cards"]


def test_a_collection_taken_out_of_the_selection_is_forgotten(fake):
    docs = fake.add_collection(fake.site, "docs", fields=FIELDS)
    guide = fake.add_item(docs, "Guide")
    state: dict = {}
    sync(fake, state)

    run = sync(fake, state, collections=("posts",))

    assert run.forgotten == {guide, docs}


def test_turning_cards_off_forgets_the_cards_only(fake):
    state: dict = {}
    sync(fake, state)

    run = sync(fake, state, include_collection_cards=False)

    assert run.forgotten == {fake.posts}
    assert run.items == set()


def test_a_listing_that_flickers_does_not_forget_an_item(fake):
    state: dict = {}
    sync(fake, state)
    hidden = fake.items[fake.posts][1]
    seen = []

    def hide_on_the_first_listing_only(path, _params):
        if path.endswith("/items/live"):
            seen.append(path)
            fake.items[fake.posts] = [i for i in fake.items[fake.posts] if i is not hidden]
            if len(seen) > 1:
                fake.items[fake.posts].append(hidden)

    fake.on_request = hide_on_the_first_listing_only

    run = sync(fake, state)

    assert run.forgotten == set(), "an item missing from one read only is not deleted"
    assert fake.bravo in state["collections"][fake.posts]["stamps"]


def test_a_deletion_confirmed_by_the_second_read_is_forgotten(fake):
    state: dict = {}
    sync(fake, state)
    fake.delete_item(fake.posts, fake.bravo)
    fake.calls.clear()

    run = sync(fake, state)

    assert run.forgotten == {fake.bravo}
    assert len(fake.requests_to("/items/live")) == 2, "one read, then one to confirm"


# ---------------------------------------------------------------------------
# Errors never delete
# ---------------------------------------------------------------------------
def test_a_failed_read_forgets_nothing_and_leaves_the_state_alone(fake):
    state: dict = {}
    sync(fake, state)
    fake.delete_item(fake.posts, fake.bravo)
    fake.fail(f"/collections/{fake.posts}/items", 500, times=50)
    before = {**state}

    with pytest.raises(WebflowAPIError):
        sync(fake, state)

    assert state == before


def test_a_revoked_token_forgets_nothing(fake):
    state: dict = {}
    sync(fake, state)
    before = {**state}
    fake.fail("/sites", 401, times=10, code="not_authorized")

    with pytest.raises(WebflowAPIError) as error:
        sync(fake, state)

    assert error.value.status == 401
    assert state == before


def test_a_rate_limited_read_is_retried_and_completes(fake):
    fake.fail("/collections", 429, times=3, code="too_many_requests", headers={"Retry-After": "1"})

    assert sync(fake, {}).items == {fake.alpha, fake.bravo}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def text(value):
    return {"type": "text", "text": {"text": value, "html": f"<p>{value}</p>"}}


@pytest.fixture
def with_pages(fake):
    fake.about = fake.add_page(fake.site, "About", "about", [text("We make kettles.")])
    fake.contact = fake.add_page(fake.site, "Contact", "contact", [text("Write to us.")])
    fake.add_page(fake.site, "Post template", "posts/template", collection_id=fake.posts)
    fake.add_page(fake.site, "Unfinished", "wip", [text("Not ready.")], draft=True)
    return fake


def test_pages_are_off_by_default(with_pages):
    run = sync(with_pages, {})

    assert run.pages == set()
    assert not with_pages.requests_to("/pages")


def test_static_pages_are_synced_but_cms_templates_and_drafts_are_not(with_pages):
    run = sync(with_pages, {}, include_pages=True)

    assert run.pages == {with_pages.about, with_pages.contact}
    about = run.rows[f"webflow:page:{with_pages.about}"]
    assert about["content"].endswith("\n\nWe make kettles.")
    assert about["cognee_node_set"] == ["webflow:acme-kettles/pages"]
    assert run.tables[f"webflow:page:{with_pages.about}"] == f"webflow_pages_{with_pages.site}"


def test_only_changed_pages_are_read_again(with_pages):
    state: dict = {}
    sync(with_pages, state, include_pages=True)
    with_pages.edit_page(with_pages.site, with_pages.about, [text("We make teapots.")])
    with_pages.calls.clear()

    run = sync(with_pages, state, include_pages=True)

    assert run.pages == {with_pages.about}
    dom_reads = with_pages.requests_to("/dom")
    assert [call[0] for call in dom_reads] == [f"/pages/{with_pages.about}/dom"]


def test_a_deleted_page_is_forgotten(with_pages):
    state: dict = {}
    sync(with_pages, state, include_pages=True)
    with_pages.delete_page(with_pages.site, with_pages.contact)

    run = sync(with_pages, state, include_pages=True)

    assert run.forgotten == {with_pages.contact}


def test_an_unreadable_page_is_skipped_and_never_forgotten(with_pages):
    state: dict = {}
    sync(with_pages, state, include_pages=True)
    with_pages.edit_page(with_pages.site, with_pages.about, [text("Changed.")])
    with_pages.fail(f"/pages/{with_pages.about}/dom", 403, times=10, code="forbidden")

    run = sync(with_pages, state, include_pages=True)

    assert run.pages == set() and run.forgotten == set()
    assert with_pages.about in state["pages"][with_pages.site]["stamps"]


def test_a_page_failing_for_a_server_reason_aborts_the_run(with_pages):
    with_pages.fail(f"/pages/{with_pages.about}/dom", 500, times=10)

    with pytest.raises(WebflowAPIError):
        sync(with_pages, {}, include_pages=True)


def test_turning_pages_off_forgets_them(with_pages):
    state: dict = {}
    sync(with_pages, state, include_pages=True)

    run = sync(with_pages, state)

    assert run.forgotten == {with_pages.about, with_pages.contact}
    assert state["pages"] == {}


def test_a_token_without_pages_read_stops_the_run_with_the_api_message(with_pages):
    with_pages.fail(f"/sites/{with_pages.site}/pages", 403, times=10, code="missing_scopes")

    with pytest.raises(WebflowAPIError, match="missing_scopes"):
        sync(with_pages, {}, include_pages=True)
