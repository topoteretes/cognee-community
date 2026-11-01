"""Incremental sync and forget-on-delete over the fake server, without dlt."""

import logging

import pytest
import requests
from fake_gramps import PASSWORD, FakeGramps, small_tree
from helpers import by_id, client_for, family, person, run

from cognee_community_connector_gramps import gramps as g


def _ids(rows):
    return sorted(row["id"] for row in rows)


def _tombstones(rows):
    return sorted(row["id"] for row in rows if row.get("_deleted"))


def _live(rows):
    return sorted(row["id"] for row in rows if not row.get("_deleted"))


def test_first_run_emits_every_document_and_a_second_run_is_a_cheap_no_op():
    fake = small_tree()
    client, state = client_for(fake), {}

    rows, stats = run(client, state)
    assert _ids(rows) == [family("f1"), person("p_anna"), person("p_john"), person("p_mary")]
    assert stats["synced"] == 4 and stats["full"] == 1
    assert state["tree_id"] == "tree0001"

    fake.calls.clear()
    rows, stats = run(client, state)
    assert rows == []
    assert stats["full"] == 0
    # metadata plus two counts for each of the six object types, no listing
    assert stats["requests"] == 13
    assert all(params.get("pagesize") in (1, None) for _, _, params in fake.calls)


def test_listings_are_read_page_by_page(monkeypatch):
    monkeypatch.setattr(g, "_PAGE_SIZE", 2)
    fake = small_tree()
    for i in range(5):
        fake.add_person(f"extra{i}", f"Extra{i}", "Person")

    rows, _ = run(client_for(fake), {})

    assert len([r for r in rows if r["id"].startswith("gramps:person:")]) == 8
    pages = [p["page"] for m, path, p in fake.calls if path == "/people/" and "keys" in p]
    assert pages == [1, 2, 3, 4]


def test_an_edit_to_an_event_re_emits_only_the_person_it_belongs_to():
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)

    fake.edit("events", "e_occ", description="Headmistress")
    rows, stats = run(client, state)

    assert _ids(rows) == [person("p_mary")]
    assert "Headmistress" in rows[0]["content"]
    assert stats["skipped"] == 3


def test_a_renamed_parent_updates_every_document_that_names_them():
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)

    john = fake.objects["people"]["p_john"]
    fake.edit("people", "p_john", primary_name={**john["primary_name"], "first_name": "Johann"})
    rows, _ = run(client, state)

    assert _ids(rows) == [family("f1"), person("p_anna"), person("p_john"), person("p_mary")]
    assert all("Johann" in row["content"] for row in rows)


def test_an_edit_to_the_most_recently_changed_object_is_picked_up():
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)

    newest = max(fake.objects["people"].values(), key=lambda p: p["change"])
    fake.edit("people", newest["handle"], gender=3)
    rows, _ = run(client, state)
    assert person(newest["handle"]) in by_id(rows)


def test_an_added_person_is_picked_up_by_the_quick_check():
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)

    fake.add_person("p_new", "Newton", "Smith")
    rows, stats = run(client, state)
    assert _ids(rows) == [person("p_new")]
    assert stats["full"] == 1


def test_a_deletion_found_in_the_transaction_history_is_forgotten():
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)

    fake.edit("families", "f1", mother_handle=None)
    fake.delete("people", "p_anna")
    fake.calls.clear()
    rows, stats = run(client, state)

    assert _tombstones(rows) == [person("p_anna")]
    # the family and both relatives named Anna, so they are re-emitted without her
    assert _live(rows) == [family("f1"), person("p_john"), person("p_mary")]
    assert stats["deleted"] == 1
    assert fake.gets("/people/p_anna") == 0, "the history confirmed it, no single check needed"
    assert person("p_anna") not in state["members"]
    assert state["txn_id"] == len(fake.transactions)


def test_without_the_history_a_vanished_record_is_confirmed_by_a_404():
    fake = small_tree(FakeGramps(history=False))
    client, state = client_for(fake), {}
    run(client, state)

    fake.delete("people", "p_anna")
    rows, _ = run(client, state)

    assert _tombstones(rows) == [person("p_anna")]
    assert fake.gets("/people/p_anna") == 1
    assert state["txn_id"] is None


def test_a_deletion_missing_from_the_history_is_still_confirmed_by_a_404():
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)

    fake.delete("people", "p_anna", log=False)  # e.g. a bulk tool that skips the history
    rows, _ = run(client, state)
    assert _tombstones(rows) == [person("p_anna")]


def test_a_record_that_vanished_from_a_listing_but_still_exists_is_kept(monkeypatch):
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)

    real_list_all = g._list_all

    def list_all_missing_anna(client, kind):
        objects = real_list_all(client, kind)
        return [o for o in objects if o["handle"] != "p_anna"] if kind == "people" else objects

    monkeypatch.setattr(g, "_list_all", list_all_missing_anna)
    fake.edit("events", "e_occ", description="Headmistress")
    rows, stats = run(client, state)

    assert _tombstones(rows) == []
    assert stats["kept"] == 1
    assert state["pending_checks"] == [person("p_anna")]
    assert person("p_anna") in state["members"]

    monkeypatch.setattr(g, "_list_all", real_list_all)
    rows, stats = run(client, state)
    assert stats["full"] == 1, "a pending check forces a full pass"
    assert state["pending_checks"] == []


def test_a_failed_single_check_keeps_the_record():
    fake = small_tree(FakeGramps(history=False))
    client, state = client_for(fake), {}
    run(client, state)

    fake.delete("people", "p_anna")
    fake.fail("/people/p_anna", 403)
    rows, stats = run(client, state)
    assert _tombstones(rows) == []
    assert stats["failed"] == 1 and stats["kept"] == 1


def test_a_record_turned_private_is_forgotten():
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)

    fake.edit("people", "p_anna", private=True)
    rows, _ = run(client, state)
    assert _tombstones(rows) == [person("p_anna")]
    assert "Anna" not in by_id(rows)[family("f1")]["content"]


def test_the_tag_filter_scopes_the_sync_and_untagging_forgets():
    fake = small_tree()
    client, state = client_for(fake), {}

    rows, _ = run(client, state, tags=("Family",))
    assert _ids(rows) == [person("p_mary")]

    fake.edit("people", "p_mary", tag_list=[])
    rows, _ = run(client, state, tags=("Family",))
    assert _tombstones(rows) == [person("p_mary")]


def test_dropping_a_kind_from_include_forgets_its_documents():
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)

    rows, stats = run(client, state, include=("people",))
    assert _tombstones(rows) == [family("f1")]
    assert stats["full"] == 1, "a new configuration always does a full pass"


def test_a_full_pass_runs_after_full_sync_hours_even_without_changes():
    fake = small_tree()
    client, state = client_for(fake), {}
    clock = [1_000_000.0]
    run(client, state, now=lambda: clock[0])

    clock[0] += 3600
    assert run(client, state, now=lambda: clock[0])[1]["full"] == 0
    clock[0] += 24 * 3600
    rows, stats = run(client, state, now=lambda: clock[0])
    assert stats["full"] == 1 and rows == [] and stats["skipped"] == 4


def test_state_from_another_tree_is_refused():
    client, state = client_for(small_tree()), {}
    run(client, state)

    other = small_tree(FakeGramps(tree_id="other999"))
    with pytest.raises(g.GrampsSourceError, match="another Gramps tree"):
        run(client_for(other), state)


def test_a_listing_that_keeps_shifting_aborts_without_forgetting(monkeypatch):
    fake = small_tree()
    for i in range(4):
        fake.add_person(f"extra{i}", f"Extra{i}", "Person")
    client, state = client_for(fake), {}
    run(client, state)
    before = dict(state)
    monkeypatch.setattr(g, "_PAGE_SIZE", 2)
    fake.delete("people", "p_anna")

    real_request = fake.request
    added = [0]

    def shifting(method, url, **kwargs):
        response = real_request(method, url, **kwargs)
        params = kwargs.get("params") or {}
        listing = params.get("keys") not in (None, "handle")
        if url.endswith("/people/") and params.get("page") == 1 and listing:
            added[0] += 1
            fake.add_person(f"late{added[0]}", "Late", "Arrival")
        return response

    monkeypatch.setattr(fake, "request", shifting)
    with pytest.raises(g.GrampsListingError):
        run(client, state)
    assert state == before


def test_a_listing_that_shifts_once_is_read_again(monkeypatch):
    fake = small_tree()
    monkeypatch.setattr(g, "_PAGE_SIZE", 2)
    real_request = fake.request
    shifted = []

    def shift_once(method, url, **kwargs):
        response = real_request(method, url, **kwargs)
        params = kwargs.get("params") or {}
        if url.endswith("/people/") and params.get("page") == 1 and not shifted:
            shifted.append(1)
            fake.add_person("late", "Late", "Arrival")
        return response

    monkeypatch.setattr(fake, "request", shift_once)
    rows, _ = run(client_for(fake), {})
    assert person("late") in by_id(rows)


def test_rate_limits_and_server_errors_are_retried_with_backoff():
    fake = small_tree()
    sleeps: list[float] = []
    client = client_for(fake, sleeps)
    fake.fail("/people/", (429, {"Retry-After": "3"}), 503)

    rows, _ = run(client, {})
    assert len(rows) == 4
    assert sleeps == [3.0, 2.0]


def test_a_retry_wait_over_the_budget_aborts_the_run():
    fake = small_tree()
    fake.fail("/metadata/", (429, {"Retry-After": "600"}))
    with pytest.raises(g.GrampsAPIError, match="HTTP 429"):
        run(client_for(fake), {})


def test_network_errors_are_retried_and_their_text_is_not_echoed():
    fake = small_tree()
    secret_url = f"https://owner:{PASSWORD}@gramps.test/api/metadata/"
    fake.fail("/metadata/", *[requests.ConnectionError(secret_url)] * 5)
    with pytest.raises(g.GrampsAPIError) as excinfo:
        run(client_for(fake), {})
    assert PASSWORD not in str(excinfo.value) and PASSWORD not in excinfo.value.detail


def test_an_expired_token_is_renewed_with_the_refresh_token_then_by_logging_in():
    fake = small_tree()
    client, state = client_for(fake), {}
    run(client, state)
    assert client.logins == 1

    fake.expire_tokens()
    run(client, state)
    assert client.logins == 1, "the refresh token renewed the access token"

    fake.expire_tokens()
    fake.refresh_tokens.clear()
    run(client, state)
    assert client.logins == 2


def test_a_wrong_password_fails_without_echoing_it(caplog):
    fake = small_tree()
    client = g.GrampsClient("https://gramps.test", "owner", "wrong-pass", session=fake.session)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(g.GrampsAuthError) as excinfo:
        run(client, {})
    assert "wrong-pass" not in str(excinfo.value)
    assert "wrong-pass" not in repr(client)
    assert "wrong-pass" not in caplog.text


def test_tokens_never_reach_the_logs(caplog):
    fake = small_tree()
    caplog.set_level(logging.DEBUG)
    fake.fail("/people/", 500)
    client, state = client_for(fake), {}
    run(client, state)
    fake.expire_tokens()
    run(client, state)
    assert "access-" not in caplog.text and "refresh-" not in caplog.text
    assert PASSWORD not in caplog.text
