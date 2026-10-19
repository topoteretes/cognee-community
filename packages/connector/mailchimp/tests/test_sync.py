"""Sync logic (_iter_rows): change detection, drafts, sends, renames, deletes,
notes and the guards. Each scenario replays an action checked on a live
account (see conftest.py)."""

import httpx
import pytest

from cognee_community_connector_mailchimp.mailchimp import (
    MailchimpClient,
    MailchimpError,
    MailchimpNotFoundError,
    _iter_rows,
)


def run(fake, state, **kwargs):
    """One sync run; returns ({row id: row}, stats). ``state`` is updated in place."""
    stats: dict[str, int] = {}
    client = MailchimpClient(fake.api_key, http_client=fake.http_client())
    rows = _iter_rows(client, state=state, stats=stats, **kwargs)
    return {row["id"]: row for row in rows}, stats


NO_COUNTS = {
    "scanned": 0,
    "changed": 0,
    "unchanged": 0,
    "deleted": 0,
    "skipped": 0,
    "held_back": 0,
    "failed": 0,
    "failed_auth": 0,
    "failed_rate_limit": 0,
}


def counts(**values):
    return {**NO_COUNTS, **values}


def content_reads(fake):
    return [r for r in fake.requests if r.url.path.endswith("/content")]


def test_first_run_syncs_sent_campaigns_only_by_default(seeded, campaign_named):
    rows, stats = run(seeded, {})
    launch = campaign_named("Spring launch")
    assert list(rows) == [f"campaign:{launch['id']}"]
    row = rows[f"campaign:{launch['id']}"]
    assert "The new checkout flow ships on 15 March." in row["content"]
    assert "Copyright" not in row["content"]
    assert "View this email" not in row["content"]
    assert row["url"].startswith("https://mailchi.mp/")
    assert row["cognee_node_set"] == [f"mailchimp:{seeded.list_id}"]
    assert stats == counts(scanned=1, changed=1)


def test_unchanged_sent_campaign_is_not_read_again(seeded):
    state: dict = {}
    run(seeded, state)
    seeded.requests.clear()
    rows, stats = run(seeded, state)
    assert rows == {}
    assert stats["unchanged"] == 1
    assert content_reads(seeded) == []


def test_drafts_are_opt_in_and_read_every_run(seeded, campaign_named):
    state: dict = {}
    rows, _ = run(seeded, state, include_unsent=True)
    webinar = campaign_named("Webinar")
    assert "Status: draft, not sent" in rows[f"campaign:{webinar['id']}"]["content"]
    seeded.requests.clear()
    rows, stats = run(seeded, state, include_unsent=True)
    assert rows == {}
    # The draft's text is read again (drafts change silently), the sent one is not.
    assert len(content_reads(seeded)) == 1
    assert stats["unchanged"] == 2


def test_draft_edit_is_caught_by_the_content_hash(seeded, campaign_named):
    # Live: editing a draft's body changed no field on the campaign.
    state: dict = {}
    run(seeded, state, include_unsent=True)
    webinar = campaign_named("Webinar")
    seeded.edit_body(webinar["id"], "Join our product webinar on 27 March.")
    rows, _ = run(seeded, state, include_unsent=True)
    assert list(rows) == [f"campaign:{webinar['id']}"]
    assert "27 March" in rows[f"campaign:{webinar['id']}"]["content"]


def test_sending_a_draft_re_renders_it_as_sent(seeded, campaign_named):
    state: dict = {}
    run(seeded, state)
    webinar = campaign_named("Webinar")
    seeded.send(webinar["id"])
    rows, _ = run(seeded, state)
    row = rows[f"campaign:{webinar['id']}"]
    assert "Sent on 2026-10-16" in row["content"]
    assert "Join our product webinar" in row["content"]


def test_sending_to_sent_does_not_resend_the_document(seeded, campaign_named):
    state: dict = {}
    webinar = campaign_named("Webinar")
    seeded.send(webinar["id"], finished=False)
    rows, _ = run(seeded, state)
    assert f"campaign:{webinar['id']}" in rows
    seeded.campaigns[webinar["id"]]["status"] = "sent"
    rows, _ = run(seeded, state)
    assert rows == {}


def test_halted_send_is_a_draft_again(seeded, campaign_named):
    # Live: an account put under review moved a "sending" campaign back to
    # "save"; send_time stayed set.
    state: dict = {}
    webinar = campaign_named("Webinar")
    key = f"campaign:{webinar['id']}"
    seeded.send(webinar["id"], finished=False)
    rows, _ = run(seeded, state)
    assert key in rows
    seeded.campaigns[webinar["id"]]["status"] = "save"
    # Sent campaigns only (the default): Mailchimp no longer counts it as sent.
    rows, _ = run(seeded, state)
    assert rows[key] == {"id": key, "_deleted": True}
    rows, _ = run(seeded, {}, include_unsent=True)
    assert "Status: draft; a send started on 2026-10-16 was stopped" in rows[key]["content"]


def test_archived_sent_campaign_is_kept_not_forgotten(seeded, campaign_named):
    # Documented status "archived": archiving hides a campaign, it deletes nothing.
    state: dict = {}
    run(seeded, state)
    launch = campaign_named("Spring launch")
    key = f"campaign:{launch['id']}"
    seeded.campaigns[launch["id"]]["status"] = "archived"
    rows, stats = run(seeded, state)
    assert stats["deleted"] == 0
    assert "Sent on 2026-10-16; archived in Mailchimp" in rows[key]["content"]
    rows, _ = run(seeded, state)
    assert rows == {}  # and like any sent campaign it is not read again


def test_archived_unsent_campaign_follows_include_unsent(seeded, campaign_named):
    webinar = campaign_named("Webinar")
    seeded.campaigns[webinar["id"]]["status"] = "archived"
    rows, _ = run(seeded, {})
    assert f"campaign:{webinar['id']}" not in rows
    rows, _ = run(seeded, {}, include_unsent=True)
    content = rows[f"campaign:{webinar['id']}"]["content"]
    assert "Status: archived in Mailchimp, not sent" in content


def test_canceled_send_is_unsent(seeded, campaign_named):
    webinar = campaign_named("Webinar")
    seeded.send(webinar["id"], finished=False)
    seeded.campaigns[webinar["id"]]["status"] = "canceled"
    rows, _ = run(seeded, {})
    assert f"campaign:{webinar['id']}" not in rows
    rows, _ = run(seeded, {}, include_unsent=True)
    content = rows[f"campaign:{webinar['id']}"]["content"]
    assert "Status: canceled; a send started on 2026-10-16 was stopped" in content


def test_rename_of_a_sent_campaign_is_caught(seeded, campaign_named):
    # Live: a rename changed only settings.title; send_time filters missed it.
    state: dict = {}
    run(seeded, state)
    launch = campaign_named("Spring launch")
    seeded.rename(launch["id"], "Spring launch (sent)")
    rows, _ = run(seeded, state)
    assert rows[f"campaign:{launch['id']}"]["title"] == "Spring launch (sent)"


def test_deleted_campaign_becomes_a_tombstone(seeded, campaign_named):
    state: dict = {}
    run(seeded, state)
    launch = campaign_named("Spring launch")
    seeded.delete_campaign(launch["id"])
    rows, stats = run(seeded, state)
    key = f"campaign:{launch['id']}"
    assert rows == {key: {"id": key, "_deleted": True}}
    assert stats["deleted"] == 1
    assert key not in state["items"]


def test_campaign_gone_mid_run_is_skipped_not_forgotten(seeded, campaign_named):
    state: dict = {}
    run(seeded, state)
    launch = campaign_named("Spring launch")
    seeded.rename(launch["id"], "Spring launch v2")
    seeded.vanish_on_content.add(launch["id"])
    rows, stats = run(seeded, state)
    assert rows == {}
    assert stats["skipped"] == 1
    assert f"campaign:{launch['id']}" in state["items"]


def test_audience_and_folder_filters(fake):
    a = fake.add_campaign("Main list news", "A.", folder_id="f1")
    b = fake.add_campaign("Partner news", "B.", list_id="cccccccccc", folder_id="f2")
    c = fake.add_campaign("No audience", "C.", list_id=None)
    for cid in (a["id"], b["id"], c["id"]):
        fake.send(cid)
    rows, _ = run(fake, {}, audience_ids=["cccccccccc"])
    assert list(rows) == [f"campaign:{b['id']}"]
    rows, _ = run(fake, {}, folder_ids=["f1"])
    assert list(rows) == [f"campaign:{a['id']}"]
    rows, _ = run(fake, {})
    assert rows[f"campaign:{c['id']}"]["cognee_node_set"] == ["mailchimp:no-audience"]


def test_notes_are_opt_in_and_keep_the_email_private(seeded):
    rows, _ = run(seeded, {})
    assert not [k for k in rows if k.startswith("note:")]
    rows, _ = run(seeded, {}, include_notes=True)
    notes = [r for k, r in rows.items() if k.startswith("note:")]
    assert len(notes) == 1
    assert "Prefers updates about the checkout redesign." in notes[0]["content"]
    assert "Anand Prakash Srivastava" in notes[0]["content"]
    assert "@example.com" not in notes[0]["content"]
    rows, _ = run(seeded, {}, include_notes=True, include_contact_email=True)
    assert any("anand@example.com" in r["content"] for r in rows.values())


def test_note_edit_and_delete(seeded):
    # Live: an edit keeps the note id and moves updated_at; a delete removes it.
    state: dict = {}
    run(seeded, state, include_notes=True)
    member = next(iter(seeded.members[seeded.list_id]))
    note = seeded.notes[(seeded.list_id, member)][0]
    seeded.edit_note(member, note["id"], "Prefers updates about the March launch.")
    rows, _ = run(seeded, state, include_notes=True)
    (row,) = rows.values()
    assert "March launch" in row["content"]
    seeded.delete_note(member, note["id"])
    rows, stats = run(seeded, state, include_notes=True)
    assert rows == {row["id"]: {"id": row["id"], "_deleted": True}}
    assert stats["deleted"] == 1


def test_contacts_without_notes_are_not_asked_for_notes(seeded):
    # Live: a contact has last_note only while it has notes.
    for i in range(5):
        seeded.add_member(f"Quiet Person{i}")
    run(seeded, {}, include_notes=True)
    note_reads = [r for r in seeded.requests if r.url.path.endswith("/notes")]
    assert len(note_reads) == 1


def test_unknown_audience_stops_without_forgetting(seeded):
    state: dict = {}
    run(seeded, state, include_notes=True)
    before = dict(state)
    with pytest.raises(MailchimpNotFoundError, match="Nothing was synced or forgotten"):
        run(seeded, state, include_notes=True, audience_ids=["nope"])
    assert state == before


def test_contact_gone_mid_run_keeps_its_notes(seeded):
    state: dict = {}
    run(seeded, state, include_notes=True)
    member = next(iter(seeded.members[seeded.list_id]))
    seeded.vanish_on_notes.add(member)
    rows, stats = run(seeded, state, include_notes=True)
    assert rows == {}
    assert stats["skipped"] == 1
    assert any(k.startswith(f"note:{seeded.list_id}:{member}:") for k in state["items"])


def test_stats_count_campaigns_and_notes_alike(seeded):
    # Live: a first run with notes on reported scanned 1, changed 2 before this.
    state: dict = {}
    _, stats = run(seeded, state, include_unsent=True, include_notes=True)
    assert stats == counts(scanned=3, changed=3)
    _, stats = run(seeded, state, include_unsent=True, include_notes=True)
    assert stats == counts(scanned=3, unchanged=3)
    seeded.vanish_on_notes.add(next(iter(seeded.members[seeded.list_id])))
    _, stats = run(seeded, state, include_unsent=True, include_notes=True)
    assert stats == counts(scanned=3, unchanged=2, skipped=1)


def _twelve_sent(fake):
    for i in range(12):
        fake.send(fake.add_campaign(f"Campaign {i}", "Body.")["id"])


def _answering(fake, rewrite):
    """Wrap the fake so ``rewrite(path, body)`` can change any JSON body it returns."""
    original = fake.handler

    def handler(request):
        response = original(request)
        if response.status_code != 200:
            return response
        return httpx.Response(200, json=rewrite(request.url.path, response.json()))

    fake.handler = handler


def test_an_empty_listing_body_forgets_nothing(seeded):
    state: dict = {}
    run(seeded, state, include_unsent=True, include_notes=True)
    before = dict(state)
    _answering(seeded, lambda path, body: {} if path.endswith("/campaigns") else body)
    with pytest.raises(MailchimpError, match="nothing was synced or forgotten"):
        run(seeded, state, include_unsent=True, include_notes=True)
    assert state == before


def test_a_delete_while_paging_never_forgets_a_skipped_campaign(fake, monkeypatch):
    # Offset paging: a campaign deleted after page 1 shifts page 2 back by one,
    # so one live campaign is never listed in that run.
    monkeypatch.setattr("cognee_community_connector_mailchimp.mailchimp.PAGE_COUNT", 2)
    ids = [fake.add_campaign(f"Campaign {i}", "Body.")["id"] for i in range(5)]
    for cid in ids:
        fake.send(cid)
    state: dict = {}
    run(fake, state)
    original = fake.handler

    def delete_after_first_page(request):
        response = original(request)
        if request.url.path.endswith("/campaigns") and request.url.params["offset"] == "0":
            fake.delete_campaign(ids[0])
        return response

    fake.handler = delete_after_first_page
    rows, stats = run(fake, state)
    # Page 1 had already listed the deleted campaign; page 2 skipped ids[2],
    # which is still live and must not be forgotten.
    assert rows == {}
    assert stats["held_back"] == 1
    assert set(state["items"]) == {f"campaign:{cid}" for cid in ids}

    fake.handler = original
    rows, stats = run(fake, state)
    assert rows == {f"campaign:{ids[0]}": {"id": f"campaign:{ids[0]}", "_deleted": True}}
    assert (stats["deleted"], stats["held_back"]) == (1, 0)


def test_a_contact_deleted_while_paging_never_forgets_a_skipped_note(fake, monkeypatch):
    monkeypatch.setattr("cognee_community_connector_mailchimp.mailchimp.PAGE_COUNT", 2)
    members = [fake.add_member(f"Person {i}") for i in range(4)]
    for member in members:
        fake.add_note(member, "Note.")
    state: dict = {}
    run(fake, state, include_notes=True)
    original = fake.handler

    def delete_after_first_page(request):
        response = original(request)
        first_page = request.url.params.get("offset") == "0"
        if request.url.path.endswith(f"/lists/{fake.list_id}/members") and first_page:
            # Deleting a contact from page 1 shifts page 2 back by one.
            del fake.members[fake.list_id][members[0]]
            del fake.notes[(fake.list_id, members[0])]  # its notes now answer 404
        return response

    fake.handler = delete_after_first_page
    rows, stats = run(fake, state, include_notes=True)
    assert rows == {}
    # Person 0's notes answer 404 (skipped, kept); Person 2 was never listed.
    assert (stats["skipped"], stats["held_back"]) == (1, 1)
    assert len(state["items"]) == 4


def test_the_same_items_in_another_order_change_nothing(seeded):
    seeded.add_note(next(iter(seeded.members[seeded.list_id])), "Second note.")
    state: dict = {}
    run(seeded, state, include_unsent=True, include_notes=True)

    def reverse(path, body):
        for key in ("campaigns", "members", "notes"):
            if isinstance(body.get(key), list):
                body[key] = body[key][::-1]
        return body

    _answering(seeded, reverse)
    rows, stats = run(seeded, state, include_unsent=True, include_notes=True)
    assert rows == {}
    assert stats["unchanged"] == 4


def test_mass_delete_is_held_back_until_the_next_run_confirms_it(fake):
    _twelve_sent(fake)
    state: dict = {}
    run(fake, state)
    for cid in list(fake.campaigns)[:11]:
        fake.delete_campaign(cid)
    rows, stats = run(fake, state)
    assert rows == {}
    assert (stats["held_back"], stats["deleted"]) == (11, 0)
    assert len(state["items"]) == 12  # still tracked, so the next run sees them gone
    rows, stats = run(fake, state)
    assert (stats["held_back"], stats["deleted"]) == (0, 11)
    assert all(row["_deleted"] for row in rows.values())
    assert "pending_delete" not in state


def test_held_back_items_that_come_back_are_not_forgotten(fake):
    # A wrong selection for one run, then the right one again.
    _twelve_sent(fake)
    state: dict = {}
    run(fake, state)
    _, stats = run(fake, state, folder_ids=["nope"])
    assert stats["held_back"] == 12
    rows, stats = run(fake, state)
    assert rows == {}
    assert (stats["deleted"], stats["unchanged"]) == (0, 12)
    assert "pending_delete" not in state


def test_a_different_mass_delete_needs_its_own_confirmation(fake):
    _twelve_sent(fake)
    state: dict = {}
    run(fake, state)
    ids = list(fake.campaigns)
    for cid in ids[:10]:
        fake.delete_campaign(cid)
    _, stats = run(fake, state)
    assert stats["held_back"] == 10
    fake.delete_campaign(ids[10])
    _, stats = run(fake, state)
    assert (stats["held_back"], stats["deleted"]) == (11, 0)


def test_allow_mass_delete_forgets_at_once(fake):
    _twelve_sent(fake)
    state: dict = {}
    run(fake, state)
    for cid in list(fake.campaigns)[:11]:
        fake.delete_campaign(cid)
    rows, stats = run(fake, state, allow_mass_delete=True)
    assert stats["deleted"] == 11
    assert all(row["_deleted"] for row in rows.values())


def test_small_deletes_never_trip_the_guard(seeded):
    state: dict = {}
    run(seeded, state, include_unsent=True, include_notes=True)
    for cid in list(seeded.campaigns):
        seeded.delete_campaign(cid)
    _, stats = run(seeded, state, include_unsent=True, include_notes=True)
    assert stats["deleted"] == 2


def test_check_active_runs_even_when_nothing_changed(seeded):
    state: dict = {}
    run(seeded, state, include_unsent=True, include_notes=True)
    calls = []
    rows, _ = run(
        seeded, state, include_unsent=True, include_notes=True, check_active=lambda: calls.append(1)
    )
    assert rows == {}
    assert len(calls) >= 3  # every campaign and every contact
