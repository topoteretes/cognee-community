"""Unit and dlt-pipeline tests for the HackMD connector. No live HackMD account needed.

* Client tests run ``HackMDClient`` against ``httpx.MockTransport``.
* Rendering tests cover the note document.
* Sync tests drive ``_iter_rows`` with a fake API and a plain dict as state, and
  run ``hackmd_source`` through a dlt pipeline into a temp sqlite destination to
  prove the incremental sync and forget-on-delete.
"""

import json
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from conftest import TEAM, FakeHackMD

from cognee_community_connector_hackmd import hackmd as hackmd_module
from cognee_community_connector_hackmd.hackmd import (
    HackMDAPIError,
    HackMDAuthError,
    HackMDClient,
    HackMDQuotaError,
    HackMDRateLimitedError,
    HackMDSourceError,
    _HackMDConfig,
    _iter_rows,
    hackmd_source,
    render_note,
)

TOKEN = "hmd-test-token"


def _config(**overrides):
    values = {"personal": True, "team_paths": (TEAM,)}
    values.update(overrides)
    return _HackMDConfig(**values)


def _run(hackmd, state, stats=None, check_active=None, **overrides):
    stats = stats if stats is not None else {}
    return list(_iter_rows(hackmd, _config(**overrides), state, stats, check_active))


def _ids(rows):
    return sorted(r["id"] for r in rows)


def _live(rows):
    return sorted(r["id"] for r in rows if not r["_deleted"])


def _gone(rows):
    return sorted(r["id"] for r in rows if r["_deleted"])


def _personal_id(note):
    return f"note:me:{note['id']}"


def _team_id(note):
    return f"note:team:{TEAM}:{note['id']}"


# ---------------------------------------------------------------------------
# HackMDClient over httpx.MockTransport
# ---------------------------------------------------------------------------
def _client(handler, **kwargs):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    sleeps = []
    client = HackMDClient(
        TOKEN,
        http=httpx.Client(transport=httpx.MockTransport(record)),
        sleep=sleeps.append,
        **kwargs,
    )
    return client, seen, sleeps


def test_client_sends_the_token_and_returns_json():
    client, seen, _ = _client(lambda r: httpx.Response(200, json=[{"id": "n1"}]))
    assert client.get("/notes") == [{"id": "n1"}]
    assert str(seen[0].url) == "https://api.hackmd.io/v1/notes"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_client_raises_auth_error_on_401():
    client, _, _ = _client(lambda r: httpx.Response(401, json={"error": "Unauthorized"}))
    with pytest.raises(HackMDAuthError):
        client.get("/notes")


def test_used_up_quota_is_not_retried():
    body = {
        "error": {
            "code": "quota_exceeded",
            "message": "Quota exceeded",
            "upgradeUrl": "https://hackmd.io/upgrade",
            "helpUrl": "https://hackmd.io/@docs/api-policy",
        }
    }
    client, seen, sleeps = _client(lambda r: httpx.Response(429, json=body))
    with pytest.raises(HackMDQuotaError):
        client.get("/notes")
    assert len(seen) == 1 and sleeps == []


def test_rate_limit_backs_off_then_gives_up():
    client, seen, sleeps = _client(lambda r: httpx.Response(429, json={"error": "Too many"}))
    with pytest.raises(HackMDRateLimitedError):
        client.get("/notes")
    assert len(seen) == 5 and sleeps == [1.0, 2.0, 4.0, 8.0]


def test_long_retry_after_stops_at_once():
    client, seen, sleeps = _client(
        lambda r: httpx.Response(429, headers={"Retry-After": "300"}, json={"error": "Too many"})
    )
    with pytest.raises(HackMDRateLimitedError):
        client.get("/notes")
    assert len(seen) == 1 and sleeps == []


def test_server_errors_are_retried():
    answers = iter([httpx.Response(502), httpx.Response(200, json=[])])
    client, seen, sleeps = _client(lambda r: next(answers))
    assert client.get("/notes") == []
    assert len(seen) == 2 and sleeps == [1.0]


def test_transport_errors_are_retried_then_raised():
    def fail(request):
        raise httpx.ConnectError("boom", request=request)

    client, seen, _ = _client(fail)
    with pytest.raises(HackMDAPIError, match="ConnectError"):
        client.get("/notes")
    assert len(seen) == 5


def test_wait_budget_stops_retries():
    client, _, sleeps = _client(lambda r: httpx.Response(503), max_sleep=2.5)
    with pytest.raises(HackMDRateLimitedError):
        client.get("/notes")
    assert sleeps == [1.0]


def test_check_active_runs_after_every_retry_wait():
    checks = []
    answers = iter([httpx.Response(503), httpx.Response(503), httpx.Response(200, json=[])])
    client, _, _ = _client(lambda r: next(answers), check_active=lambda: checks.append(1))
    client.get("/notes")
    assert len(checks) == 2


def test_errors_never_carry_the_token_or_the_body():
    client, _, _ = _client(lambda r: httpx.Response(500, text=f"<html>token={TOKEN}</html>"))
    with pytest.raises(HackMDAPIError) as caught:
        client.get("/notes")
    assert TOKEN not in str(caught.value) and "html" not in str(caught.value)
    assert caught.value.status == 500


def test_a_200_that_is_not_json_raises():
    client, _, _ = _client(lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(HackMDAPIError, match="not JSON"):
        client.get("/notes")


def test_token_is_stripped_and_a_bad_one_never_echoed():
    client, seen, _ = _client(lambda r: httpx.Response(200, json=[]))
    padded = HackMDClient(f"  {TOKEN}\n", http=client._http)
    padded.get("/notes")
    assert seen[-1].headers["Authorization"] == f"Bearer {TOKEN}"
    with pytest.raises(ValueError) as caught:
        HackMDClient("bad token\x00value")
    assert "bad token" not in str(caught.value)
    assert "redacted" in repr(padded)


def test_close_only_closes_its_own_http_client():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    HackMDClient(TOKEN, http=http).close()
    assert not http.is_closed
    owned = HackMDClient(TOKEN)
    owned.close()
    assert owned._http.is_closed


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_note_has_workspace_folders_and_tags(hackmd):
    note = hackmd.add_note(
        "RFC: Rate limiting", "", tags=["rfc", " design "], folders=["RFCs", "2026"]
    )
    row = render_note("me", note, "# RFC: Rate limiting\n\nA token bucket.\n")
    assert row == {
        "id": _personal_id(note),
        "title": "RFC: Rate limiting",
        "content": (
            "Workspace: Personal\nFolder: RFCs / 2026\nTags: rfc, design\n\n"
            "# RFC: Rate limiting\n\nA token bucket."
        ),
        "url": note["publishLink"],
        "_deleted": False,
    }


def test_render_team_note_without_folders_or_title(hackmd):
    note = hackmd.add_note("  ", "", team=TEAM)
    row = render_note(f"team:{TEAM}", note, "Body")
    assert row["title"] == "Untitled note"
    assert row["content"] == f"Workspace: Team {TEAM}\n\nBody"


def test_folder_path_follows_parents_whatever_the_order(hackmd):
    note = hackmd.add_note("Deep", "", folders=["A", "B", "C"])
    note["folderPaths"].reverse()
    assert "Folder: A / B / C" in render_note("me", note, "x")["content"]
    # A malformed loop ends instead of hanging.
    note["folderPaths"][0]["parentId"] = note["folderPaths"][-1]["id"]
    render_note("me", note, "x")


def test_row_becomes_a_hackmd_document(hackmd):
    note = hackmd.add_note("Spec", "")
    result = render_note("me", note, "Ship it.")
    dlt_row = SimpleNamespace(
        table_name="hackmd_documents",
        primary_key_value=result["id"],
        row_data=result,
        content_hash="h",
    )
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, result["id"]), "hackmd")
    assert item.data.startswith("# Spec")
    assert item.system_metadata["source"] == "hackmd"


# ---------------------------------------------------------------------------
# Sync state machine (_iter_rows with a dict as state)
# ---------------------------------------------------------------------------
def test_first_sync_emits_personal_and_team_notes(hackmd):
    mine = hackmd.add_note("Weekly sync", "Decisions.", tags=["meeting"])
    theirs = hackmd.add_note("Incident", "Root cause.", team=TEAM)
    stats = {}
    rows = _run(hackmd, {}, stats)
    assert _ids(rows) == sorted([_personal_id(mine), _team_id(theirs)])
    contents = {r["id"]: r["content"] for r in rows}
    assert contents[_personal_id(mine)].endswith("Decisions.")
    assert stats["emitted"] == 2 and stats["scanned"] == 2 and stats["failed"] == 0
    # Team notes are read through the team path, which uses the team's own quota.
    assert hackmd.calls == [
        "/notes",
        f"/teams/{TEAM}/notes",
        f"/notes/{mine['id']}",
        f"/teams/{TEAM}/notes/{theirs['id']}",
    ]


def test_unchanged_run_costs_one_listing_per_workspace(hackmd):
    hackmd.add_note("A", "a")
    hackmd.add_note("B", "b", team=TEAM)
    state = {}
    _run(hackmd, state)
    hackmd.calls.clear()
    stats = {}
    assert _run(hackmd, state, stats) == []
    assert hackmd.calls == ["/notes", f"/teams/{TEAM}/notes"]
    assert stats["unchanged"] == 2 and stats["emitted"] == 0


def test_only_edited_notes_are_read_again(hackmd):
    a = hackmd.add_note("A", "a")
    hackmd.add_note("B", "b")
    state = {}
    _run(hackmd, state)
    hackmd.calls.clear()
    hackmd.edit(a, "a, edited")
    rows = _run(hackmd, state, team_paths=())
    assert _ids(rows) == [_personal_id(a)]
    assert hackmd.note_reads() == [f"/notes/{a['id']}"]


def test_a_touch_without_a_text_change_is_not_emitted(hackmd):
    a = hackmd.add_note("A", "same text")
    state = {}
    _run(hackmd, state, team_paths=())
    hackmd.edit(a)
    stats = {}
    assert _run(hackmd, state, stats, team_paths=()) == []
    assert stats["unchanged"] == 1 and len(hackmd.note_reads()) == 2
    # Settled: the next run reads nothing.
    hackmd.calls.clear()
    _run(hackmd, state, team_paths=())
    assert hackmd.note_reads() == []


def test_a_retag_without_lastchangedat_is_still_picked_up(hackmd):
    a = hackmd.add_note("A", "text", tags=["draft"])
    state = {}
    _run(hackmd, state, team_paths=())
    a["tags"] = ["final"]
    rows = _run(hackmd, state, team_paths=())
    assert len(rows) == 1 and "Tags: final" in rows[0]["content"]


def test_a_rename_without_lastchangedat_is_still_picked_up(hackmd):
    a = hackmd.add_note("Old title", "text")
    state = {}
    _run(hackmd, state, team_paths=())
    a["title"] = "New title"
    rows = _run(hackmd, state, team_paths=())
    assert [r["title"] for r in rows] == ["New title"]


def test_deleted_notes_are_tombstoned(hackmd):
    a = hackmd.add_note("A", "a")
    b = hackmd.add_note("B", "b", team=TEAM)
    # Each workspace keeps a note, so its listing is not empty.
    keep = [hackmd.add_note("Keep", "k"), hackmd.add_note("Keep", "k", team=TEAM)]
    state = {}
    _run(hackmd, state)
    del hackmd.personal[a["id"]]
    del hackmd.teams[TEAM][b["id"]]
    stats = {}
    rows = _run(hackmd, state, stats)
    assert _gone(rows) == sorted([_personal_id(a), _team_id(b)]) and _live(rows) == []
    assert stats["deleted"] == 2
    assert set(state["workspaces"]["me"]["notes"]) == {keep[0]["id"]}


def test_a_note_moved_to_a_team_is_forgotten_in_one_workspace_and_added_in_the_other(hackmd):
    a = hackmd.add_note("A", "a")
    hackmd.add_note("Keep", "k")
    state = {}
    _run(hackmd, state)
    moved = hackmd.personal.pop(a["id"])
    moved["teamPath"] = TEAM
    hackmd.teams[TEAM][a["id"]] = moved
    rows = _run(hackmd, state)
    assert _gone(rows) == [_personal_id(a)] and _live(rows) == [_team_id(a)]


def test_tag_filter_keeps_matching_notes_and_forgets_the_rest(hackmd):
    rfc = hackmd.add_note("RFC", "r", tags=["rfc"])
    hackmd.add_note("Other", "o", tags=["misc"])
    state = {}
    rows = _run(hackmd, state, team_paths=(), tags=("rfc",))
    assert _ids(rows) == [_personal_id(rfc)]
    rfc["tags"] = ["misc"]
    rows = _run(hackmd, state, team_paths=(), tags=("rfc",))
    assert _gone(rows) == [_personal_id(rfc)]


def test_folder_filter_matches_any_depth(hackmd):
    deep = hackmd.add_note("Deep", "d", folders=["RFCs", "2026"])
    hackmd.add_note("Elsewhere", "e", folders=["Meetings"])
    hackmd.add_note("Root", "r")
    rows = _run(hackmd, {}, team_paths=(), folders=("RFCs",))
    assert _ids(rows) == [_personal_id(deep)]


def test_deselecting_a_workspace_forgets_its_notes(hackmd):
    a = hackmd.add_note("A", "a")
    b = hackmd.add_note("B", "b", team=TEAM)
    state = {}
    _run(hackmd, state)
    rows = _run(hackmd, state, team_paths=())
    assert _gone(rows) == [_team_id(b)]
    rows = _run(hackmd, state, personal=False, team_paths=(TEAM,))
    assert _gone(rows) == [_personal_id(a)] and _live(rows) == [_team_id(b)]


def test_a_team_that_cannot_be_read_on_its_first_sync_raises(hackmd):
    with pytest.raises(HackMDSourceError, match="nope"):
        _run(hackmd, {}, team_paths=("nope",))


@pytest.mark.parametrize("status", [403, 404])
def test_a_known_team_that_becomes_unreadable_is_kept(hackmd, status):
    b = hackmd.add_note("B", "b", team=TEAM)
    state = {}
    _run(hackmd, state)
    hackmd.errors[f"/teams/{TEAM}/notes"] = status
    stats = {}
    assert _run(hackmd, state, stats) == []
    assert stats["unreadable"] == 1 and stats["deleted"] == 0
    assert b["id"] in state["workspaces"][f"team:{TEAM}"]["notes"]


def test_personal_listing_errors_raise(hackmd):
    hackmd.errors["/notes"] = 403
    with pytest.raises(HackMDAPIError):
        _run(hackmd, {}, team_paths=())


def test_an_empty_listing_is_confirmed_by_the_next_run(hackmd):
    a = hackmd.add_note("A", "a")
    state = {}
    _run(hackmd, state, team_paths=())
    hackmd.personal.clear()
    assert _run(hackmd, state, team_paths=()) == []
    assert _gone(_run(hackmd, state, team_paths=())) == [_personal_id(a)]


def test_a_listing_with_notes_again_cancels_the_confirmation(hackmd):
    a = hackmd.add_note("A", "a")
    state = {}
    _run(hackmd, state, team_paths=())
    saved = dict(hackmd.personal)
    hackmd.personal.clear()
    _run(hackmd, state, team_paths=())
    hackmd.personal.update(saved)
    _run(hackmd, state, team_paths=())
    hackmd.personal.clear()
    # A fresh empty listing needs its own confirmation.
    assert _run(hackmd, state, team_paths=()) == []
    assert a["id"] in state["workspaces"]["me"]["notes"]


def test_a_filter_that_matches_nothing_forgets_at_once(hackmd):
    a = hackmd.add_note("A", "a", tags=["x"])
    state = {}
    _run(hackmd, state, team_paths=())
    assert _gone(_run(hackmd, state, team_paths=(), tags=("y",))) == [_personal_id(a)]


def test_a_listing_that_is_not_a_list_raises(hackmd):
    hackmd.add_note("A", "a")
    hackmd.listing_body = {"error": "maintenance"}
    with pytest.raises(HackMDAPIError, match="not a list"):
        _run(hackmd, {}, team_paths=())


def test_malformed_listing_items_are_skipped(hackmd):
    a = hackmd.add_note("A", "a")
    good = hackmd._listing(hackmd.personal)
    hackmd.listing_body = [*good, "junk", {"title": "no id"}]
    stats = {}
    rows = _run(hackmd, {}, stats, team_paths=())
    assert _ids(rows) == [_personal_id(a)] and stats["skipped"] == 2


def test_malformed_listing_forgets_no_known_note(hackmd):
    a = hackmd.add_note("A", "a")
    b = hackmd.add_note("B", "b")
    state = {}
    _run(hackmd, state, team_paths=())
    # b is still there, but the listing carries an item without an id in its place.
    hackmd.listing_body = [*hackmd._listing({a["id"]: a}), {"title": "B", "id": None}]
    stats = {}
    assert _gone(_run(hackmd, state, stats, team_paths=())) == []
    assert b["id"] in state["workspaces"]["me"]["notes"] and stats["skipped"] == 1


def test_a_note_deleted_after_the_listing_is_forgotten_by_the_next_listing(hackmd):
    a = hackmd.add_note("A", "a")
    hackmd.add_note("B", "b")  # keeps the listing non-empty once a is gone
    state = {}
    _run(hackmd, state, team_paths=())
    hackmd.edit(a, "new")
    hackmd.errors[f"/notes/{a['id']}"] = 404
    stats = {}
    # Still listed, so it is kept; the 404 alone never forgets a note.
    assert _run(hackmd, state, stats, team_paths=()) == [] and stats["gone"] == 1
    assert a["id"] in state["workspaces"]["me"]["notes"]

    del hackmd.personal[a["id"]]
    assert _gone(_run(hackmd, state, team_paths=())) == [_personal_id(a)]


def test_a_new_note_deleted_after_the_listing_emits_nothing(hackmd):
    a = hackmd.add_note("A", "a")
    hackmd.errors[f"/notes/{a['id']}"] = 404
    assert _run(hackmd, {}, team_paths=()) == []


def test_a_note_that_answers_403_is_kept(hackmd):
    a = hackmd.add_note("A", "a")
    state = {}
    _run(hackmd, state, team_paths=())
    hackmd.edit(a, "new")
    hackmd.errors[f"/notes/{a['id']}"] = 403
    stats = {}
    assert _run(hackmd, state, stats, team_paths=()) == []
    assert stats["unreadable"] == 1 and a["id"] in state["workspaces"]["me"]["notes"]


def test_a_note_without_content_raises(hackmd):
    a = hackmd.add_note("A", "a")
    original = hackmd.get

    def without_content(path):
        body = original(path)
        if path == f"/notes/{a['id']}":
            body.pop("content")
        return body

    hackmd.get = without_content
    with pytest.raises(HackMDAPIError, match="without its content"):
        _run(hackmd, {}, team_paths=())


def test_used_up_quota_stops_cleanly_and_keeps_progress(hackmd):
    notes = [hackmd.add_note(f"N{i}", f"text {i}") for i in range(3)]
    hackmd.quota_used_up.add(f"/notes/{notes[1]['id']}")
    state, stats = {}, {}
    rows = _run(hackmd, state, stats, team_paths=())
    assert _ids(rows) == [_personal_id(notes[0])]
    assert stats["failed"] == 1 and stats["failed_quota"] == 1
    hackmd.quota_used_up.clear()
    rows = _run(hackmd, state, team_paths=())
    assert _ids(rows) == sorted(_personal_id(n) for n in notes[1:])


def test_quota_during_the_listings_forgets_nothing(hackmd):
    a = hackmd.add_note("A", "a")
    hackmd.add_note("B", "b", team=TEAM)
    state = {}
    _run(hackmd, state)
    del hackmd.personal[a["id"]]
    hackmd.quota_used_up.add("/notes")
    stats = {}
    assert _run(hackmd, state, stats) == []
    assert stats["failed_quota"] == 1 and a["id"] in state["workspaces"]["me"]["notes"]


def test_rate_limit_stops_cleanly(hackmd):
    hackmd.add_note("A", "a")
    hackmd.rate_limited.add("/notes/")
    stats = {}
    assert _run(hackmd, {}, stats, team_paths=()) == []
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1


def test_a_wrong_token_raises_on_the_first_request(hackmd):
    hackmd.bad_token = True
    with pytest.raises(HackMDAuthError):
        _run(hackmd, {})


def test_a_token_revoked_during_the_run_keeps_what_is_done(hackmd):
    notes = [hackmd.add_note(f"N{i}", f"text {i}") for i in range(3)]
    hackmd.revoke_after = 2  # the listing and one note
    state, stats = {}, {}
    rows = _run(hackmd, state, stats, team_paths=())
    assert _ids(rows) == [_personal_id(notes[0])]
    assert stats["failed"] == 1 and stats["failed_auth"] == 1
    assert notes[0]["id"] in state["workspaces"]["me"]["notes"]


def test_budget_pauses_and_resumes(hackmd):
    notes = [hackmd.add_note(f"N{i}", f"text {i}") for i in range(5)]
    state = {}
    seen = []
    for _ in range(10):
        stats = {}
        seen += _live(_run(hackmd, state, stats, team_paths=(), max_requests=3))
        # A cut run is incomplete, so failed is set with the budget as the reason.
        assert stats["failed"] == stats["failed_budget"]
        if not stats["failed_budget"]:
            break
    assert sorted(seen) == sorted(_personal_id(n) for n in notes)
    assert "resume" not in state


def test_budget_never_stops_a_run_before_any_progress(hackmd):
    hackmd.add_note("A", "a")
    hackmd.add_note("B", "b", team=TEAM)
    stats = {}
    rows = _run(hackmd, {}, stats, max_requests=1)
    # Two listings ran though the budget is one; the first note then finished.
    assert len(rows) == 1 and stats["failed_budget"] == 1


def test_resume_gives_every_note_a_turn(hackmd):
    notes = [hackmd.add_note(f"N{i}", f"text {i}") for i in range(4)]
    state = {}
    _run(hackmd, state, team_paths=())
    for note in notes:
        hackmd.edit(note, note["content"] + " v2")
    first = _live(_run(hackmd, state, team_paths=(), max_requests=2))
    second = _live(_run(hackmd, state, team_paths=(), max_requests=2))
    assert first and second and not set(first) & set(second)


def test_a_note_that_keeps_failing_does_not_block_the_others(hackmd):
    stuck = hackmd.add_note("A", "a")
    others = [hackmd.add_note(f"N{i}", f"text {i}") for i in range(2)]
    # The 403 note is kept as changed, so every run tries it again.
    hackmd.errors[f"/notes/{stuck['id']}"] = 403
    state, seen = {}, []
    for _ in range(4):
        seen += _live(_run(hackmd, state, team_paths=(), max_requests=2))
    assert sorted(seen) == sorted(_personal_id(n) for n in others)


def test_check_active_runs_before_every_request(hackmd):
    hackmd.add_note("A", "a")
    hackmd.add_note("B", "b", team=TEAM)
    checks = []
    _run(hackmd, {}, check_active=lambda: checks.append(len(hackmd.calls)))
    assert len(checks) == len(hackmd.calls) == 4


def test_stats_always_carry_every_key(hackmd):
    stats = {}
    _run(hackmd, {}, stats)
    assert set(stats) == set(hackmd_module._STATS)
    assert all(value == 0 for key, value in stats.items() if key.startswith("failed"))


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------
def test_default_resource_names_follow_the_scope():
    def name(**kwargs):
        return getattr(hackmd_source(service=FakeHackMD(), **kwargs), dlt_utils.PIPELINE_SCOPE_ATTR)

    # Personal notes have no account identity up front, so two accounts in one
    # dataset would share a constant name.
    for kwargs in ({}, {"team_paths": ["eng"]}, {"tags": ["rfc"]}):
        with pytest.raises(ValueError, match="Pass a resource_name"):
            name(**kwargs)
    assert name(resource_name="hackmd_mine") == "hackmd_mine"
    team = {"personal": False}
    assert name(team_paths=["eng"], **team) == "hackmd_eng"
    # Case keeps team paths apart.
    assert name(team_paths=["Eng"], **team) != name(team_paths=["eng"], **team)
    # Filters scope their own state, so two filtered syncs never share cleanup.
    rfc = name(team_paths=["eng"], tags=["rfc"], **team)
    assert (
        rfc
        != name(team_paths=["eng"], tags=["meeting"], **team)
        != name(team_paths=["eng"], **team)
    )
    assert len(name(team_paths=["t" * 80], **team)) <= 64
    for path in ["\u65e5\u672c", "---"]:
        derived = name(team_paths=[path], **team)
        assert derived.startswith("hackmd_") and "__" not in derived


def test_resource_name_is_validated():
    fake = FakeHackMD()
    for bad in ["", "  ", "Hackmd", "hack-md", "a" * 65, "1hackmd"]:
        with pytest.raises(ValueError, match="resource_name"):
            hackmd_source(service=fake, resource_name=bad)
    assert hackmd_source(service=fake, resource_name="hackmd_rfcs").name == "hackmd_rfcs"


def test_selection_is_validated():
    fake = FakeHackMD()
    with pytest.raises(ValueError, match="Select something"):
        hackmd_source(service=fake, personal=False)
    with pytest.raises(ValueError, match="path only"):
        hackmd_source(service=fake, team_paths=["https://hackmd.io/team/eng"])
    for empty in [{"team_paths": [" "]}, {"tags": [""]}, {"folders": ["  "]}]:
        with pytest.raises(ValueError, match="must not be empty"):
            hackmd_source(service=fake, **empty)


def test_a_token_is_required_and_checked_up_front(monkeypatch):
    monkeypatch.setenv("HACKMD_API_TOKEN", "from-env")
    with pytest.raises(ValueError, match="token is required"):
        hackmd_source(resource_name="hackmd_mine")
    with pytest.raises(ValueError, match="invalid format"):
        hackmd_source(token="has space", resource_name="hackmd_mine")


def test_resource_carries_the_document_contract():
    resource = hackmd_source(service=FakeHackMD(), personal=False, team_paths=["eng"])
    assert getattr(resource, dlt_utils.DOCUMENT_SOURCE_ATTR) == "hackmd"
    assert getattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR) == resource.name
    assert resource.cognee_sync_stats == {}


def test_client_is_an_alias_of_service():
    fake = FakeHackMD()
    fake.add_note("A", "a")
    resource = hackmd_source(client=fake, resource_name="hackmd_mine")
    rows = list(resource)
    assert [r["title"] for r in rows] == ["A"]


def test_an_owned_client_is_closed_after_the_run(monkeypatch):
    built = []

    class Recording:
        def __init__(self, *args, **kwargs):
            self.closed = False
            built.append(self)

        def get(self, path):
            return []

        def close(self):
            self.closed = True

    monkeypatch.setattr(hackmd_module, "HackMDClient", Recording)
    list(hackmd_source(token=TOKEN, resource_name="hackmd_mine"))
    list(hackmd_source(token=TOKEN, resource_name="hackmd_mine"))
    assert len(built) == 2 and all(c.closed for c in built)


# ---------------------------------------------------------------------------
# dlt pipeline: incremental sync + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


PipelineStepFailed = pytest.importorskip("dlt.pipeline.exceptions").PipelineStepFailed


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "hackmd.db").as_posix()
    return dlt.pipeline(
        pipeline_name="hackmd_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="hackmd_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline, table="hackmd_documents"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id FROM {table}") as cursor,
    ):
        return {r[0] for r in cursor.fetchall()}


def _source(hackmd, name="hackmd_documents", **kwargs):
    return hackmd_source(service=hackmd, team_paths=[TEAM], resource_name=name, **kwargs)


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, hackmd):
    a = hackmd.add_note("A", "a")
    b = hackmd.add_note("B", "b", team=TEAM)
    c = hackmd.add_note("C", "c", team=TEAM)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(hackmd))
    assert _staged(pipeline) == {_personal_id(a), _team_id(b), _team_id(c)}

    # The state lives in dlt, so a fresh source object resumes from it.
    hackmd.calls.clear()
    source = _source(hackmd)
    pipeline.run(source)
    assert hackmd.note_reads() == [] and source.cognee_sync_stats["emitted"] == 0

    del hackmd.teams[TEAM][b["id"]]
    pipeline.run(_source(hackmd))
    assert _staged(pipeline) == {_personal_id(a), _team_id(c)}


def test_two_sources_in_one_dataset_leave_each_other_alone(dlt_mod, tmp_path, hackmd):
    rfc = hackmd.add_note("RFC", "r", tags=["rfc"])
    meeting = hackmd.add_note("Sync", "s", tags=["meeting"])
    pipeline = _pipeline(dlt_mod, tmp_path)
    rfcs = hackmd_source(service=hackmd, tags=["rfc"], resource_name="hackmd_rfcs")
    meetings = hackmd_source(service=hackmd, tags=["meeting"], resource_name="hackmd_meetings")
    pipeline.run(rfcs)
    pipeline.run(meetings)

    del hackmd.personal[rfc["id"]]
    pipeline.run(hackmd_source(service=hackmd, tags=["rfc"], resource_name="hackmd_rfcs"))
    pipeline.run(hackmd_source(service=hackmd, tags=["meeting"], resource_name="hackmd_meetings"))
    assert _staged(pipeline, rfcs.name) == set()
    assert _staged(pipeline, meetings.name) == {_personal_id(meeting)}


def test_failed_run_keeps_staging_and_state(dlt_mod, tmp_path, hackmd):
    a = hackmd.add_note("A", "a")
    b = hackmd.add_note("B", "b")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(hackmd))

    del hackmd.personal[b["id"]]
    hackmd.errors[f"/teams/{TEAM}/notes"] = 500
    failing = _source(hackmd)
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(failing)
    assert "HTTP 500" in str(caught.value)
    assert _staged(pipeline) == {_personal_id(a), _personal_id(b)}

    hackmd.errors.clear()
    pipeline.run(_source(hackmd))
    assert _staged(pipeline) == {_personal_id(a)}


def test_stats_are_plain_json(hackmd):
    hackmd.add_note("A", "a")
    resource = hackmd_source(service=hackmd, resource_name="hackmd_mine")
    list(resource)
    assert json.loads(json.dumps(resource.cognee_sync_stats))["emitted"] == 1
