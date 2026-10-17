"""Sync state machine tests: _iter_rows against an in-memory Freshdesk.

No network, no LLM. Each test drives the fake through a real-world change
(reply, delete, restore, spam, unpublished edit, ...) and checks the rows the
connector yields and the state it keeps for the next run.  The behaviours come
from a live trial helpdesk; see tests/fake_freshdesk.py.
"""

import pytest
from fake_freshdesk import HOST, FakeFreshdesk, FakeResponse

from cognee_community_connector_freshdesk.freshdesk import (
    FreshdeskClient,
    FreshdeskError,
    _Config,
    _iter_rows,
)


@pytest.fixture
def fd():
    return FakeFreshdesk()


def client_for(fake, sleeps=None, **kwargs):
    return FreshdeskClient(
        "acme",
        "secret-key",
        session=fake,
        sleep=(sleeps if sleeps is not None else []).append,
        **kwargs,
    )


def sync(fake, state, config=None, client=None):
    stats: dict = {}
    client = client or client_for(fake)
    rows = list(_iter_rows(client, config or _Config(), state, stats))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = sorted(row["id"] for row in rows if row["_deleted"])
    return upserts, deletes, stats


def tid(ticket_id):
    return f"freshdesk:{HOST}:ticket:{ticket_id}"


def aid(article_id):
    return f"freshdesk:{HOST}:article:{article_id}"


def kb(fake):
    """Category > folder > subfolder, like the trial helpdesk."""
    category = fake.add_category("Account Help")
    folder = fake.add_folder("Login", category=category)
    sub = fake.add_folder("SSO", category=category, parent=folder)
    return folder, sub


# ---------------------------------------------------------------------------
# First sync, rendering and request shape
# ---------------------------------------------------------------------------
def test_first_sync_reads_every_page_of_tickets_and_saves_the_cursor():
    fake = FakeFreshdesk(page_size=2)
    ids = [fake.put_ticket(f"Ticket {i}") for i in range(5)]
    state: dict = {}

    upserts, deletes, stats = sync(fake, state, _Config(articles=False))

    assert set(upserts) == {tid(i) for i in ids}
    assert deletes == []
    assert stats["tickets"] == 5
    assert state["tickets"]["cursor"] == fake.tickets[ids[-1]]["updated_at"]
    assert state["tickets"]["known"] == ids


def test_first_sync_never_relies_on_the_30_day_default_list(fd):
    fd.put_ticket("Old ticket")

    sync(fd, {}, _Config(articles=False))

    path, query = fd.calls[0]
    assert path == "/tickets"
    assert query["updated_since"] == "2000-01-01T00:00:00Z"
    assert query["order_by"] == "updated_at" and query["order_type"] == "asc"
    assert query["include"] == "requester,description"


def test_ticket_row_shape_and_content(fd):
    ticket = fd.put_ticket(
        "Login fails after password reset",
        "I reset my password but the login page still says invalid credentials.",
        tags=["login", "sso"],
        type="Problem",
    )
    fd.reply(ticket, "Please clear your browser cache.")
    fd.reply(ticket, "Still broken on Firefox.", incoming=True)

    upserts, _, _ = sync(fd, {}, _Config(articles=False))

    row = upserts[tid(ticket)]
    t = fd.tickets[ticket]
    replies = fd.conversations[ticket]
    assert row == {
        "id": tid(ticket),
        "title": "Login fails after password reset",
        "content": (
            f"Freshdesk ticket #{ticket}: Login fails after password reset\n"
            "Status: Open. Priority: Low. Source: Phone. Type: Problem\n"
            "Requester: Meera Joshi <meera@example.com>\n"
            "Tags: login, sso\n"
            f"Created: {t['created_at']}. Updated: {t['updated_at']}\n\n"
            "I reset my password but the login page still says invalid credentials.\n\n"
            f"Agent reply, {replies[0]['created_at']}:\nPlease clear your browser cache.\n\n"
            f"Customer message, {replies[1]['created_at']}:\nStill broken on Firefox."
        ),
        "url": f"https://{HOST}/a/tickets/{ticket}",
        "_deleted": False,
    }


def test_custom_status_shows_its_number_instead_of_a_guess(fd):
    ticket = fd.put_ticket("Waiting on vendor", status=6)

    upserts, _, _ = sync(fd, {}, _Config(articles=False))

    assert "Status: 6." in upserts[tid(ticket)]["content"]


def test_private_notes_are_left_out_unless_opted_in(fd):
    ticket = fd.put_ticket("SSO loop")
    fd.reply(ticket, "Clear cookies for the SSO domain.")
    fd.reply(ticket, "Internal: legacy SSO tenant, check auth logs.", private=True)

    default, _, _ = sync(fd, {}, _Config(articles=False))
    opted_in, _, _ = sync(fd, {}, _Config(articles=False, private_notes=True))

    assert "legacy SSO tenant" not in default[tid(ticket)]["content"]
    assert "Clear cookies" in default[tid(ticket)]["content"]
    assert "Private note" in opted_in[tid(ticket)]["content"]
    assert "legacy SSO tenant" in opted_in[tid(ticket)]["content"]


def test_conversations_can_be_switched_off_and_cost_no_requests(fd):
    ticket = fd.put_ticket("SSO loop")
    fd.reply(ticket, "Clear cookies.")

    upserts, _, _ = sync(fd, {}, _Config(articles=False, conversations=False))

    assert "Clear cookies" not in upserts[tid(ticket)]["content"]
    assert not any(path.endswith("/conversations") for path, _ in fd.calls)


# ---------------------------------------------------------------------------
# Incremental rounds and the inclusive cursor
# ---------------------------------------------------------------------------
def test_resync_without_changes_yields_nothing(fd):
    fd.put_ticket("One")
    fd.put_ticket("Two")
    state: dict = {}
    sync(fd, state, _Config(articles=False))

    upserts, deletes, stats = sync(fd, state, _Config(articles=False))

    # updated_since is inclusive: the newest ticket comes back but is skipped.
    assert upserts == {} and deletes == []
    assert stats["skipped"] == 1


def test_incremental_round_passes_the_saved_cursor(fd):
    fd.put_ticket("One")
    state: dict = {}
    sync(fd, state, _Config(articles=False))
    fd.calls.clear()

    sync(fd, state, _Config(articles=False))

    sent = [q["updated_since"] for path, q in fd.calls if path == "/tickets"]
    assert sent == [state["tickets"]["cursor"]] * 3  # changed, deleted and spam passes


def test_ticket_changed_in_the_same_second_as_the_cursor_is_not_skipped(fd):
    fd.put_ticket("One")
    state: dict = {}
    sync(fd, state, _Config(articles=False))

    late = fd.put_ticket("Created in the same second", same_second=True)
    upserts, _, _ = sync(fd, state, _Config(articles=False))

    assert list(upserts) == [tid(late)]
    assert len(state["tickets"]["on_cursor"]) == 2


def test_a_new_reply_resyncs_the_ticket_with_it(fd):
    ticket = fd.put_ticket("SSO loop")
    other = fd.put_ticket("Unrelated")
    state: dict = {}
    sync(fd, state, _Config(articles=False))

    fd.reply(ticket, "Fixed after the cookie reset.")
    fd.calls.clear()
    upserts, _, _ = sync(fd, state, _Config(articles=False))

    assert list(upserts) == [tid(ticket)]
    assert "Fixed after the cookie reset." in upserts[tid(ticket)]["content"]
    conversation_calls = [p for p, _ in fd.calls if p.endswith("/conversations")]
    assert conversation_calls == [f"/tickets/{ticket}/conversations"]  # not for {other}
    assert other in state["tickets"]["known"]


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_deleted_ticket_is_forgotten(fd):
    keep = fd.put_ticket("Keep")
    gone = fd.put_ticket("Refund for double charge")
    state: dict = {}
    sync(fd, state, _Config(articles=False))

    fd.delete_ticket(gone)
    upserts, deletes, stats = sync(fd, state, _Config(articles=False))

    assert upserts == {}
    assert deletes == [tid(gone)]
    assert stats["deleted"] == 1
    assert state["tickets"]["known"] == [keep]


def test_restored_ticket_comes_back(fd):
    ticket = fd.put_ticket("Refund for double charge")
    state: dict = {}
    sync(fd, state, _Config(articles=False))
    fd.delete_ticket(ticket)
    sync(fd, state, _Config(articles=False))

    fd.restore_ticket(ticket)
    upserts, deletes, _ = sync(fd, state, _Config(articles=False))

    assert list(upserts) == [tid(ticket)]
    assert deletes == []
    assert state["tickets"]["known"] == [ticket]


def test_a_delete_during_the_run_does_not_move_the_cursor_past_an_edit(fd):
    edited = fd.put_ticket("Edited mid-run")
    deleted = fd.put_ticket("Deleted mid-run")
    state: dict = {}
    sync(fd, state, _Config(articles=False))
    real_get = fd.get
    happened = []

    def change_after_the_normal_listing(url, params=None, **kwargs):
        if params and params.get("filter") == "deleted" and not happened:
            # After the changed-tickets listing, before the deleted pass:
            # an agent edits one ticket, then someone deletes another.
            fd.edit_ticket(edited, subject="Edited mid-run, v2")
            fd.delete_ticket(deleted)
            happened.append(True)
        return real_get(url, params=params, **kwargs)

    fd.get = change_after_the_normal_listing
    _, deletes, _ = sync(fd, state, _Config(articles=False))
    assert deletes == [tid(deleted)]

    upserts, _, _ = sync(fd, state, _Config(articles=False))
    assert upserts[tid(edited)]["title"] == "Edited mid-run, v2", "the edit must not be skipped"


def test_spam_ticket_is_forgotten(fd):
    ticket = fd.put_ticket("Buy cheap watches now")
    state: dict = {}
    sync(fd, state, _Config(articles=False))

    fd.mark_spam(ticket)
    _, deletes, _ = sync(fd, state, _Config(articles=False))

    assert deletes == [tid(ticket)]


def test_deleting_a_ticket_that_was_never_synced_emits_no_tombstone(fd):
    fd.put_ticket("Keep")
    state: dict = {}
    sync(fd, state, _Config(articles=False))

    stranger = fd.put_ticket("Created and deleted between two syncs")
    fd.delete_ticket(stranger)
    upserts, deletes, _ = sync(fd, state, _Config(articles=False))

    assert upserts == {} and deletes == []


def test_ticket_deleted_while_its_conversations_are_read_is_skipped(fd):
    ticket = fd.put_ticket("Vanishing")
    original = fd._route

    def purge_on_conversations(path, query):
        if path.endswith("/conversations"):
            fd.delete_ticket(ticket)
            fd.purge_ticket(ticket)
        return original(path, query)

    fd._route = purge_on_conversations
    upserts, deletes, _ = sync(fd, {}, _Config(articles=False))

    assert upserts == {} and deletes == []


def test_purged_ticket_is_caught_by_the_full_resync_after_an_option_change(fd):
    keep = fd.put_ticket("Keep")
    purged = fd.put_ticket("Deleted forever")
    state: dict = {}
    sync(fd, state, _Config(articles=False))

    # "Delete forever" removes it from filter=deleted too, so an incremental
    # round cannot see it.  This is a documented limitation ...
    fd.delete_ticket(purged)
    fd.purge_ticket(purged)
    _, deletes, _ = sync(fd, state, _Config(articles=False))
    assert deletes == []

    # ... which a full listing (first run or changed render options) reconciles.
    _, deletes, _ = sync(fd, state, _Config(articles=False, private_notes=True))
    assert deletes == [tid(purged)]
    assert state["tickets"]["known"] == [keep]


# ---------------------------------------------------------------------------
# Paging past the page limit
# ---------------------------------------------------------------------------
def test_listing_restarts_from_the_last_updated_at_at_the_page_limit():
    fake = FakeFreshdesk(page_size=2)
    ids = [fake.put_ticket(f"Ticket {i}") for i in range(7)]

    upserts, _, _ = sync(fake, {}, _Config(articles=False), client_for(fake, max_pages=2))

    assert set(upserts) == {tid(i) for i in ids}
    restarts = [q["updated_since"] for p, q in fake.calls if p == "/tickets" and "page" not in q]
    assert len(restarts) > 1, "the listing restarted from a later updated_at"


def test_page_limit_full_of_one_timestamp_raises_instead_of_looping():
    fake = FakeFreshdesk(page_size=2)
    fake.put_ticket("First")
    for i in range(5):
        fake.put_ticket(f"Same second {i}", same_second=True)
    client = client_for(fake, max_pages=2)

    # The second listing starts at the shared timestamp and fills the page
    # limit again without moving forward.
    with pytest.raises(RuntimeError, match="cannot page further"):
        sync(fake, {}, _Config(articles=False), client)


# ---------------------------------------------------------------------------
# Errors, retries and rate limits
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [401, 403, 404])
def test_client_errors_raise_and_never_delete(fd, status):
    fd.put_ticket("Keep")
    state: dict = {}
    sync(fd, state)
    before = repr(state)

    fd.fail_next.append(status)
    with pytest.raises(FreshdeskError) as excinfo:
        sync(fd, state)

    assert excinfo.value.status == status
    assert repr(state) == before


def test_server_errors_are_retried_then_raise_without_changing_state(fd):
    fd.put_ticket("Keep")
    state: dict = {}
    sync(fd, state)
    before = repr(state)

    sleeps: list = []
    fd.fail_next.extend([500] * 6)
    with pytest.raises(FreshdeskError):
        sync(fd, state, client=client_for(fd, sleeps))

    assert sleeps == [1, 2, 4, 8, 16]
    assert repr(state) == before


def test_429_waits_for_retry_after_and_then_succeeds(fd):
    ticket = fd.put_ticket("Keep")
    sleeps: list = []
    fd.fail_next.append(429)

    upserts, _, _ = sync(fd, {}, _Config(articles=False), client_for(fd, sleeps))

    assert sleeps == [7]
    assert list(upserts) == [tid(ticket)]


def test_low_rate_limit_slows_down_even_when_the_header_is_a_float(fd):
    fd.put_ticket("Keep")
    fd.rate_headers = {"X-Ratelimit-Total": "50", "X-Ratelimit-Remaining": "2.0"}
    sleeps: list = []

    sync(fd, {}, _Config(articles=False), client_for(fd, sleeps))

    assert sleeps and all(s == pytest.approx(1.2) for s in sleeps)


def test_plenty_of_rate_limit_left_means_no_waiting(fd):
    fd.put_ticket("Keep")
    sleeps: list = []

    sync(fd, {}, client=client_for(fd, sleeps))

    assert sleeps == []


def test_api_key_is_sent_as_basic_auth_with_x(fd):
    sync(fd, {}, _Config(articles=False))

    assert fd.auth_seen == ("secret-key", "X")


# ---------------------------------------------------------------------------
# Solution articles
# ---------------------------------------------------------------------------
def test_articles_in_subfolders_are_found_and_drafts_are_skipped(fd):
    folder, sub = kb(fd)
    published = fd.put_article("Fix SSO login loop", "Clear cookies.", folder=sub)
    draft = fd.put_article("Password rules", "At least 12 characters.", folder=folder, status=1)

    upserts, _, stats = sync(fd, {}, _Config(tickets=False))

    assert list(upserts) == [aid(published)]
    assert aid(draft) not in upserts
    assert stats["skipped"] == 1


def test_article_row_shape_and_content(fd):
    _, sub = kb(fd)
    article = fd.put_article("Fix SSO login loop", "Clear cookies.", folder=sub, tags=["sso"])

    upserts, _, _ = sync(fd, {}, _Config(tickets=False))

    assert upserts[aid(article)] == {
        "id": aid(article),
        "title": "Fix SSO login loop",
        "content": (
            "Freshdesk solution article: Fix SSO login loop\n"
            "Location: Account Help > Login > SSO\n"
            "Tags: sso\n"
            f"Updated: {fd.articles[article]['updated_at']}\n\n"
            "Clear cookies."
        ),
        "url": f"https://{HOST}/support/solutions/articles/{article}",
        "_deleted": False,
    }


def test_unchanged_articles_are_not_resynced_and_edits_are(fd):
    folder, _ = kb(fd)
    same = fd.put_article("Same", "Untouched.", folder=folder)
    edited = fd.put_article("Edited", "Version one.", folder=folder)
    state: dict = {}
    sync(fd, state, _Config(tickets=False))

    fd.edit_article(edited, text="Version two.")
    upserts, deletes, _ = sync(fd, state, _Config(tickets=False))

    assert list(upserts) == [aid(edited)]
    assert "Version two." in upserts[aid(edited)]["content"]
    assert deletes == []
    assert set(state["articles"]["known"]) == {str(same), str(edited)}


def test_deleted_article_is_forgotten(fd):
    _, sub = kb(fd)
    gone = fd.put_article("Fix SSO login loop", "Clear cookies.", folder=sub)
    state: dict = {}
    sync(fd, state, _Config(tickets=False))

    fd.delete_article(gone)
    upserts, deletes, _ = sync(fd, state, _Config(tickets=False))

    assert upserts == {}
    assert deletes == [aid(gone)]
    assert state["articles"]["known"] == {}


def test_unpublished_article_is_forgotten(fd):
    folder, _ = kb(fd)
    article = fd.put_article("Old policy", "Retired.", folder=folder)
    state: dict = {}
    sync(fd, state, _Config(tickets=False))

    fd.edit_article(article, status=1)
    _, deletes, _ = sync(fd, state, _Config(tickets=False))

    assert deletes == [aid(article)]


def test_deleting_a_folder_forgets_the_articles_below_it(fd):
    folder, sub = kb(fd)
    top = fd.put_article("Top", "In the folder.", folder=folder)
    deep = fd.put_article("Deep", "In the subfolder.", folder=sub)
    state: dict = {}
    sync(fd, state, _Config(tickets=False))

    fd.delete_folder(folder)
    _, deletes, _ = sync(fd, state, _Config(tickets=False))

    assert deletes == sorted([aid(top), aid(deep)])


def test_error_during_the_article_walk_changes_nothing(fd):
    _, sub = kb(fd)
    fd.put_article("Fix SSO login loop", "Clear cookies.", folder=sub)
    state: dict = {}
    sync(fd, state, _Config(tickets=False))
    before = repr(state)
    real_get = fd.get

    def subfolders_always_503(url, **kwargs):
        if "/subfolders" in url:
            return FakeResponse(503, None, dict(fd.rate_headers))
        return real_get(url, **kwargs)

    fd.get = subfolders_always_503
    # Retried, then the run fails: the article must not be forgotten.
    with pytest.raises(FreshdeskError):
        sync(fd, state, _Config(tickets=False))

    assert repr(state) == before


# ---------------------------------------------------------------------------
# Selection changes and several helpdesks
# ---------------------------------------------------------------------------
def test_switching_articles_off_forgets_them(fd):
    folder, _ = kb(fd)
    article = fd.put_article("Fix SSO login loop", "Clear cookies.", folder=folder)
    ticket = fd.put_ticket("Keep")
    state: dict = {}
    sync(fd, state)

    _, deletes, _ = sync(fd, state, _Config(articles=False))

    assert deletes == [aid(article)]
    assert "articles" not in state
    assert state["tickets"]["known"] == [ticket]


def test_switching_tickets_off_forgets_them(fd):
    ticket = fd.put_ticket("Gone from scope")
    state: dict = {}
    sync(fd, state)

    _, deletes, _ = sync(fd, state, _Config(tickets=False))

    assert deletes == [tid(ticket)]
    assert "tickets" not in state


def test_two_helpdesks_keep_separate_row_ids(fd):
    fd.put_ticket("Same number")
    other = FakeFreshdesk()
    other.put_ticket("Same number")

    first, _, _ = sync(fd, {}, _Config(articles=False))
    second, _, _ = sync(
        other,
        {},
        _Config(articles=False),
        FreshdeskClient("globex", "k", session=other, sleep=lambda _s: None),
    )

    assert set(first) == {"freshdesk:acme.freshdesk.com:ticket:1"}
    assert set(second) == {"freshdesk:globex.freshdesk.com:ticket:1"}
