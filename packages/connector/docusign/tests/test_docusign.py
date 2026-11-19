"""Unit tests for the DocuSign dlt connector.

Three layers, all runnable in CI without a DocuSign account:

* DB-free tests for the envelope header, PDF text and node sets, and the
  generic document ``DataItem`` tagging (``source="docusign"``).
* Sync tests against ``FakeDocuSign`` covering the incremental cursor and every
  forget-on-delete path, including the two the status-change date never shows:
  the Deleted folder and purges.
* dlt-pipeline tests (temp sqlite destination) proving a tombstone physically
  removes the row on ``merge`` and that a failed run keeps its cursor.
"""

import copy
import logging
import time
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import dlt
import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from fake_docusign import (
    ACCOUNT_ID,
    API_HOST,
    INTEGRATION_KEY,
    SENT_ITEMS,
    USER_ID,
    FakeDocuSign,
    make_pdf,
)

from cognee_community_connector_docusign.auth import DocuSignAuth, resolve_credentials
from cognee_community_connector_docusign.client import (
    DocuSignAPIError,
    DocuSignClient,
    DocuSignRateLimitError,
)
from cognee_community_connector_docusign.docusign import (
    DOCUSIGN_SOURCE_NAME,
    _build_scope,
    _parse_time,
    _pdf_text,
    docusign_source,
    sync_envelopes,
)

LISTING = {"from_date": "2000-01-01", "from_to_status": "changed"}
CONTRACTS = {"folderId": "folder-contracts", "name": "Contracts", "type": "normal"}
ARCHIVE = {"folderId": "folder-archive", "name": "Archive", "type": "normal"}


def _row_id(envelope_id):
    return f"envelope:{envelope_id}"


@pytest.fixture
def fake(rsa_keys):
    return FakeDocuSign(public_key=rsa_keys[1])


@pytest.fixture
def client(fake, rsa_keys):
    session = fake.session()
    auth = DocuSignAuth(resolve_credentials(INTEGRATION_KEY, USER_ID, rsa_keys[0]), session)
    return DocuSignClient(auth, f"https://{API_HOST}", ACCOUNT_ID, session, sleep=lambda _s: None)


def _scope(**overrides):
    options = {
        "statuses": ["completed"],
        "folder_ids": None,
        "from_date": None,
        "include_certificates": False,
        "include_form_data": True,
        "node_set_field": None,
    }
    return _build_scope(**{**options, **overrides})


def _sync(client, state, scope=None, stats=None):
    """Run one sync; return the yielded rows keyed by id."""
    rows = list(sync_envelopes(client, scope or _scope(), state, stats))
    return {row["id"]: row for row in rows}


def _live(rows):
    return {row_id for row_id, row in rows.items() if not row["_deleted"]}


def _tombstones(rows):
    return {row_id for row_id, row in rows.items() if row["_deleted"]}


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def test_row_has_header_sentences_and_the_pdf_text(fake, client):
    envelope_id = fake.add_envelope(
        "Master Services Agreement",
        "This agreement is made between Alphacorp and Bravocorp.\nPayment terms: net 45.",
        signers=[
            {"name": "Alice Smith", "email": "alice@alpha.com", "role": "Customer"},
            {"name": "Bob Jones", "email": "bob@bravo.com", "role": "Vendor"},
        ],
        custom={"agreement_type": "MSA"},
        form={"Contract value": "25000"},
    )

    row = _sync(client, {})[_row_id(envelope_id)]

    assert row["title"] == "Master Services Agreement"
    assert row["_deleted"] is False
    content = row["content"]
    # Sender and dates, as sentences. DocuSign's 7-digit fractions must parse.
    assert (
        'The agreement "Master Services Agreement" was sent by Sam Sender (sam@example.com) '
        "on 2026-01-01 at 01:00 UTC and was completed on 2026-01-01 at 01:00 UTC." in content
    )
    assert "Alice Smith (alice@alpha.com) signed as Customer on 2026-01-01 at 01:00 UTC." in content
    assert "Bob Jones (bob@bravo.com) signed as Vendor on 2026-01-01 at 01:00 UTC." in content
    assert "The custom field agreement_type is MSA." in content
    assert "Alice Smith (alice@alpha.com) filled in Contract value with 25000." in content
    # The signed document's own text follows the header.
    assert content.index("Alice Smith") < content.index("This agreement is made between")
    assert "Payment terms: net 45." in content


def test_declined_and_waiting_signers_are_described(fake, client):
    envelope_id = fake.add_envelope(
        "Offer letter", "Terms", status="sent", signers=[{"name": "Carol"}, {"name": "Dan"}]
    )
    envelope = fake.envelopes[envelope_id]
    envelope["recipients"]["signers"][0].update(
        status="declined", declinedDateTime="2026-01-02T09:30:00.0000000Z", declinedReason="No"
    )
    scope = _scope(statuses=["sent"])

    content = _sync(client, {}, scope)[_row_id(envelope_id)]["content"]

    assert "its status is sent." in content
    assert "Carol (carol@example.com) declined to sign on 2026-01-02 at 09:30 UTC" in content
    assert "and gave the reason: No." in content
    assert "Dan (dan@example.com) has not signed yet." in content


def test_a_scanned_pdf_keeps_its_header_and_is_counted(fake, client):
    envelope_id = fake.add_envelope("Scanned NDA", pdf=make_pdf([]))
    stats: dict = {}

    row = _sync(client, {}, stats=stats)[_row_id(envelope_id)]

    assert row["content"].startswith('The agreement "Scanned NDA"')
    assert stats["pdf_no_text"] == 1
    assert stats["ingested"] == 1


def test_an_unreadable_pdf_keeps_its_header_and_is_counted(fake, client):
    envelope_id = fake.add_envelope("Broken", pdf=b"%PDF-1.4 this is not a pdf")
    stats: dict = {}

    row = _sync(client, {}, stats=stats)[_row_id(envelope_id)]

    assert 'The agreement "Broken"' in row["content"]
    assert stats["pdf_unreadable"] == 1


def test_pdf_text_strips_control_characters():
    stats = {"pdf_no_text": 0, "pdf_unreadable": 0}
    text = _pdf_text(make_pdf(["Line one", "Line   two"]), stats)
    assert text == "Line one\nLine two"
    assert stats == {"pdf_no_text": 0, "pdf_unreadable": 0}


def test_timestamps_with_seven_fraction_digits_parse():
    parsed = _parse_time("2026-01-05T10:00:00.1234567Z")
    assert parsed.isoformat() == "2026-01-05T10:00:00.123456+00:00"
    assert _parse_time("2026-01-05").isoformat() == "2026-01-05T00:00:00+00:00"
    assert _parse_time("garbage") is None


def test_node_sets_come_from_configured_folders_and_the_custom_field(fake, client):
    inside = fake.add_envelope(
        "NDA", "text", folders=[CONTRACTS], custom={"agreement_type": "NDA", "other": "x"}
    )
    scope = _scope(folder_ids=["folder-contracts"], node_set_field="Agreement_Type")

    row = _sync(client, {}, scope)[_row_id(inside)]

    assert row["cognee_node_set"] == ["Contracts", "NDA"]


def test_no_node_set_column_unless_something_names_one(fake, client):
    envelope_id = fake.add_envelope("Plain", "text")
    assert "cognee_node_set" not in _sync(client, {})[_row_id(envelope_id)]


def test_build_document_data_item_tags_source_and_node_sets():
    row = SimpleNamespace(
        row_data={
            "id": _row_id("env-1"),
            "title": "NDA",
            "content": "Alice signed.",
            "cognee_node_set": ["Contracts", "NDA"],
        },
        table_name="docusign_envelopes",
        content_hash="abc123",
        primary_key_value=_row_id("env-1"),
    )
    item = _build_document_data_item(row, uuid5(NAMESPACE_OID, "env-1"), DOCUSIGN_SOURCE_NAME)

    assert item.system_metadata["source"] == "docusign"
    assert item.system_metadata["external_id"] == "envelope:env-1"
    assert item.data.startswith("# NDA")
    assert item.node_set == ["docusign:Contracts", "docusign:NDA"]


def test_scope_validation():
    with pytest.raises(ValueError, match="voided"):
        _scope(statuses=["completed", "voided"])
    with pytest.raises(ValueError, match="at least one"):
        _scope(statuses=[])
    with pytest.raises(ValueError, match="from_date"):
        _scope(from_date="last tuesday")
    assert _scope(statuses=["Completed", "completed"]).statuses == ("completed",)


def test_source_declares_document_marker_and_account_scope(monkeypatch):
    from cognee.tasks.ingestion.dlt_utils import (
        PIPELINE_SCOPE_ATTR,
        document_source_tag,
        pipeline_name_for_source,
    )

    monkeypatch.delenv("DOCUSIGN_ACCOUNT_ID", raising=False)
    source = docusign_source(access_token="tok", account_id=ACCOUNT_ID, base_uri="https://x")

    assert document_source_tag(source) == "docusign"
    # The scope is the resource name, so the staging table and the state share one name.
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "docusign_envelopes"
    assert pipeline_name_for_source(source, "a") != pipeline_name_for_source(source, "b")
    assert pipeline_name_for_source(source, "a") != "ingest_dlt_source"


# ---------------------------------------------------------------------------
# Sync: ingest and the incremental cursor
# ---------------------------------------------------------------------------


def test_first_sync_ingests_only_envelopes_in_scope(fake, client):
    done = fake.add_envelope("Done", "text")
    fake.add_envelope("Still out", "text", status="sent")
    fake.add_envelope("Voided", "text", status="voided")
    stats: dict = {}

    rows = _sync(client, {}, stats=stats)

    assert _live(rows) == {_row_id(done)}
    assert _tombstones(rows) == set()  # never ingested, so nothing to forget
    assert stats["full_sync"] == 1
    # The default window still passes status=completed,deleted,voided: the
    # sent envelope is not even listed, the voided one is listed and dropped.
    assert fake.listing_queries()[0]["status"] == "completed,deleted,voided"
    assert fake.listing_queries()[0]["from_date"] == "2000-01-01T00:00:00Z"


def test_first_sync_pages_through_a_large_account(fake, client):
    ids = {_row_id(fake.add_envelope(f"Agreement {n}", "text")) for n in range(230)}
    assert _live(_sync(client, {})) == ids
    assert len(fake.listing_queries()) == 3


def test_unchanged_resync_fetches_no_documents(fake, client):
    fake.add_envelope("A", "text")
    fake.add_envelope("B", "text")
    state: dict = {}
    _sync(client, state)
    fake.requests.clear()
    stats: dict = {}

    rows = _sync(client, state, stats=stats)

    assert rows == {}
    # Only the newest envelope is inside the cursor's overlap window.
    assert stats["listed"] == 1 and stats["unchanged"] == 1 and stats["full_sync"] == 0
    assert fake.requested("/documents/") == []
    assert fake.requested("/form_data") == []


def test_incremental_listing_starts_at_the_cursor_minus_the_overlap(fake, client):
    fake.add_envelope("A", "text")
    state: dict = {}
    _sync(client, state)
    cursor = state["cursor"]
    assert cursor == "2026-01-01T01:00:00.123Z"
    fake.requests.clear()

    _sync(client, state)

    assert fake.listing_queries()[0]["from_date"] == "2026-01-01T00:55:00.123Z"


def test_new_and_changed_envelopes_are_picked_up(fake, client):
    keep = fake.add_envelope("Keep", "version one")
    edited = fake.add_envelope("Edited", "before")
    state: dict = {}
    _sync(client, state)

    fresh = fake.add_envelope("Fresh", "new")
    fake.edit_text(edited, "after the correction")
    rows = _sync(client, state)

    assert _live(rows) == {_row_id(fresh), _row_id(edited)}
    assert "after the correction" in rows[_row_id(edited)]["content"]
    assert _row_id(keep) not in rows


def test_envelope_that_completes_later_is_ingested(fake, client):
    in_flight = fake.add_envelope("Pending", "text", status="sent")
    state: dict = {}
    assert _sync(client, state) == {}

    fake.complete(in_flight)

    assert _live(_sync(client, state)) == {_row_id(in_flight)}


def test_form_data_can_be_switched_off(fake, client):
    envelope_id = fake.add_envelope("Form", "text", form={"Amount": "10"})
    rows = _sync(client, {}, _scope(include_form_data=False))
    assert "Amount" not in rows[_row_id(envelope_id)]["content"]
    assert fake.requested("/form_data") == []


def test_certificate_flag_reaches_the_document_request(fake, client):
    fake.add_envelope("Cert", "text")
    _sync(client, {}, _scope(include_certificates=True))
    queries = [q for _m, path, q in fake.requests if path.endswith("/documents/combined")]
    assert queries == [{"certificate": "true"}]

    fake.requests.clear()
    _sync(client, {})
    queries = [q for _m, path, q in fake.requests if path.endswith("/documents/combined")]
    assert queries == [{"certificate": "false"}]


# ---------------------------------------------------------------------------
# Sync: forget-on-delete
# ---------------------------------------------------------------------------


def test_voided_in_progress_envelope_is_forgotten(fake, client):
    envelope_id = fake.add_envelope("Pending", "text", status="sent")
    scope = _scope(statuses=["sent", "completed"])
    state: dict = {}
    assert _live(_sync(client, state, scope)) == {_row_id(envelope_id)}

    fake.void(envelope_id)

    assert _tombstones(_sync(client, state, scope)) == {_row_id(envelope_id)}
    assert state["known"] == {}


def test_envelope_moved_to_the_deleted_folder_is_forgotten_by_the_recheck(fake, client):
    binned = fake.add_envelope("Binned", "text")
    keep = fake.add_envelope("Keep", "text")  # newest: the only one the cursor window re-lists
    state: dict = {}
    _sync(client, state)
    fake.move_to_recycle_bin(binned)  # the status-change date does not move
    fake.requests.clear()

    rows = _sync(client, state)

    assert _tombstones(rows) == {_row_id(binned)}
    assert set(state["known"]) == {keep}
    # The listing never returned it, so it was looked up by id.
    (lookup,) = [q for q in fake.listing_queries() if "envelope_ids" in q]
    assert lookup["envelope_ids"] == binned
    assert lookup["include_purge_information"] == "true"
    assert "from_date" not in lookup


def test_envelope_in_the_deleted_folder_is_forgotten_when_the_listing_returns_it(fake, client):
    keep = fake.add_envelope("Keep", "text")
    binned = fake.add_envelope("Binned", "text")  # newest, so re-listed by the overlap window
    state: dict = {}
    _sync(client, state)
    fake.move_to_recycle_bin(binned)

    assert _tombstones(_sync(client, state)) == {_row_id(binned)}
    assert set(state["known"]) == {keep}


def test_emptied_deleted_folder_is_forgotten_by_the_recheck(fake, client):
    # Seen live: days after an envelope is binned, DocuSign empties the folder. The
    # envelope then lists no folders at all.
    gone = fake.add_envelope("Gone", "text")
    keep = fake.add_envelope("Keep", "text")
    state: dict = {}
    _sync(client, state)
    fake.empty_recycle_bin(gone)

    assert _tombstones(_sync(client, state)) == {_row_id(gone)}
    assert set(state["known"]) == {keep}


def test_unreadable_envelope_in_a_first_sync_is_skipped_not_fatal(fake, client):
    # The live failure: a fresh sync listed an envelope whose documents answered HTTP 400.
    readable = fake.add_envelope("Readable", "text")
    unreadable = fake.add_envelope("Unreadable", "text")
    fake.envelopes[unreadable]["_unreadable"] = True  # listed with folders, but not readable

    rows = _sync(client, {})

    assert _live(rows) == {_row_id(readable)}
    assert _tombstones(rows) == set()


def test_known_envelope_that_becomes_unreadable_is_forgotten(fake, client):
    keep = fake.add_envelope("Keep", "text")
    lost = fake.add_envelope("Lost", "text")
    state: dict = {}
    _sync(client, state)
    fake.edit_text(lost, "changed, then access was lost")
    fake.envelopes[lost]["_unreadable"] = True

    assert _tombstones(_sync(client, state)) == {_row_id(lost)}
    assert set(state["known"]) == {keep}


def test_other_client_errors_still_abort_the_run(fake, client):
    fake.add_envelope("A", "text")
    fake.fail_once("/documents/combined", 400)  # not the "unreadable" error code
    with pytest.raises(DocuSignAPIError, match="HTTP 400"):
        _sync(client, {})


def test_purged_documents_are_forgotten_by_the_recheck(fake, client):
    purged = fake.add_envelope("Purged", "text")
    keep = fake.add_envelope("Keep", "text")
    state: dict = {}
    _sync(client, state)

    fake.purge(purged)  # documents_purged: still listed by id, purgeState set

    assert _tombstones(_sync(client, state)) == {_row_id(purged)}
    assert set(state["known"]) == {keep}


def test_envelope_that_vanished_entirely_is_forgotten(fake, client):
    vanished = fake.add_envelope("Vanished", "text")
    keep = fake.add_envelope("Keep", "text")
    state: dict = {}
    _sync(client, state)

    fake.purge(vanished, metadata=True)  # gone from every listing

    assert _tombstones(_sync(client, state)) == {_row_id(vanished)}
    assert set(state["known"]) == {keep}


def test_lookup_that_returns_nothing_does_not_wipe_the_dataset(fake, client, caplog):
    only = fake.add_envelope("Only", "text")
    state: dict = {}
    _sync(client, state)
    fake.purge(only, metadata=True)

    with caplog.at_level(logging.WARNING):
        rows = _sync(client, state)

    assert rows == {}
    assert set(state["known"]) == {only}


def test_a_failed_lookup_aborts_before_any_state_is_written(fake, client):
    fake.add_envelope("Keep", "text")
    binned = fake.add_envelope("Binned", "text")
    state: dict = {}
    _sync(client, state)
    before = copy.deepcopy(state)
    fake.move_to_recycle_bin(binned)
    for _ in range(5):
        fake.fail_once("/envelopes", 503)

    with pytest.raises(DocuSignAPIError, match="503"):
        _sync(client, state)

    assert state == before


def test_failed_listing_page_aborts_and_forgets_nothing(fake, client):
    for n in range(150):
        fake.add_envelope(f"Agreement {n}", "text")
    state: dict = {}
    _sync(client, state)
    known_before = dict(state["known"])
    fake.add_envelope("Late", "text")
    fake.fail_once("/envelopes", 500)  # first page
    fake.fail_once("/envelopes", 500)
    fake.fail_once("/envelopes", 500)
    fake.fail_once("/envelopes", 500)
    fake.fail_once("/envelopes", 500)

    with pytest.raises(DocuSignAPIError):
        _sync(client, state)

    assert state["known"] == known_before


def test_envelope_that_disappears_between_listing_and_fetch_is_forgotten(fake, client):
    gone = fake.add_envelope("Gone", "text")
    fake.add_envelope("Stays", "text")
    state: dict = {}
    _sync(client, state)
    fake.edit_text(gone, "changed again")
    fake.fail_once(f"{gone}/documents/combined", 404)

    rows = _sync(client, state)

    assert _tombstones(rows) == {_row_id(gone)}


# ---------------------------------------------------------------------------
# Sync: scope changes
# ---------------------------------------------------------------------------


def test_folder_filter_ingests_only_those_folders(fake, client):
    inside = fake.add_envelope("Inside", "text", folders=[CONTRACTS])
    fake.add_envelope("Outside", "text", folders=[ARCHIVE])
    scope = _scope(folder_ids=["folder-contracts"])

    assert _live(_sync(client, {}, scope)) == {_row_id(inside)}
    assert fake.listing_queries()[0]["folder_ids"] == "folder-contracts"


def test_envelope_moved_out_of_the_configured_folder_is_forgotten(fake, client):
    moved = fake.add_envelope("Moved", "text", folders=[CONTRACTS])
    fake.add_envelope("Stays", "text", folders=[CONTRACTS])
    scope = _scope(folder_ids=["Contracts"])  # by name
    state: dict = {}
    assert len(_live(_sync(client, state, scope))) == 2

    fake.move_to_folder(moved, ARCHIVE)

    assert _tombstones(_sync(client, state, scope)) == {_row_id(moved)}


def test_changing_the_scope_relists_and_forgets_what_fell_out(fake, client):
    contracts = fake.add_envelope("Contract", "text", folders=[CONTRACTS])
    archived = fake.add_envelope("Archived", "text", folders=[ARCHIVE])
    state: dict = {}
    wide = _scope()
    assert _live(_sync(client, state, wide)) == {_row_id(contracts), _row_id(archived)}

    narrow = _scope(folder_ids=["folder-contracts"])
    stats: dict = {}
    rows = _sync(client, state, narrow, stats)

    assert stats["full_sync"] == 1
    assert _tombstones(rows) == {_row_id(archived)}
    assert _live(rows) == {_row_id(contracts)}  # re-rendered under the new scope
    assert set(state["known"]) == {contracts}


def test_narrowing_statuses_forgets_the_rest(fake, client):
    fake.add_envelope("Done", "text")
    pending = fake.add_envelope("Pending", "text", status="sent")
    state: dict = {}
    both = _scope(statuses=["completed", "sent"])
    assert len(_live(_sync(client, state, both))) == 2

    assert _tombstones(_sync(client, state, _scope())) == {_row_id(pending)}


def test_empty_full_listing_does_not_forget_known_envelopes(fake, client):
    only = fake.add_envelope("Only", "text", folders=[CONTRACTS])
    state: dict = {}
    _sync(client, state, _scope())

    # A from_date in the future makes the next full listing empty.
    rows = _sync(client, state, _scope(from_date="2099-01-01"))

    assert rows == {}
    assert set(state["known"]) == {only}


# ---------------------------------------------------------------------------
# Folders: names resolve to ids, and no status filter next to a folder filter
# ---------------------------------------------------------------------------


def test_custom_folder_name_is_resolved_to_its_id(fake, client):
    inside = fake.add_envelope("Inside", "text", folders=[CONTRACTS])
    fake.add_envelope("Outside", "text", folders=[ARCHIVE])

    rows = _sync(client, {}, _scope(folder_ids=["Contracts"]))

    assert _live(rows) == {_row_id(inside)}
    query = fake.listing_queries()[0]
    assert query["folder_ids"] == "folder-contracts"  # the id, never the name (HTTP 400 live)
    assert rows[_row_id(inside)]["cognee_node_set"] == ["Contracts"]


def test_folder_filter_is_never_combined_with_a_status_filter(fake, client):
    # Against the real API, a folder filter plus several statuses returns nothing at all.
    done = fake.add_envelope("Done", "text", folders=[CONTRACTS])
    fake.add_envelope("Pending", "text", status="sent", folders=[CONTRACTS])

    rows = _sync(client, {}, _scope(folder_ids=["folder-contracts"]))

    assert "status" not in fake.listing_queries()[0]
    assert _live(rows) == {_row_id(done)}  # the status is still enforced, client side


def test_system_folder_names_pass_through(fake, client):
    envelope_id = fake.add_envelope("Sent", "text")
    rows = _sync(client, {}, _scope(folder_ids=["sentitems"]))
    assert _live(rows) == {_row_id(envelope_id)}


def test_unknown_folder_raises_and_lists_the_choices(fake, client):
    fake.add_envelope("A", "text", folders=[CONTRACTS])
    state: dict = {}
    with pytest.raises(ValueError, match="Contracts"):
        _sync(client, state, _scope(folder_ids=["Contrcts"]))
    assert state == {}


# ---------------------------------------------------------------------------
# Resuming: request budget and rate limits
# ---------------------------------------------------------------------------


def _drain(client, state, scope, **kwargs):
    """Run syncs until one finishes without being cut short; return (rows, runs)."""
    rows: dict[str, dict] = {}
    for run in range(1, 20):
        stats: dict = {}
        for row in sync_envelopes(client, scope, state, stats, **kwargs):
            rows[row["id"]] = row
        if not stats["failed"]:
            return rows, run
    raise AssertionError("the sync never finished")


def test_request_budget_stops_cleanly_and_resumes_until_done(fake, client):
    ids = {_row_id(fake.add_envelope(f"Agreement {n}", f"text {n}")) for n in range(6)}
    state: dict = {}
    stats: dict = {}

    first = {r["id"] for r in sync_envelopes(client, _scope(), state, stats, max_requests=5)}

    # 1 listing page + 2 requests per envelope fits two envelopes in a budget of 5.
    assert len(first) == 2
    assert stats["failed"] == 1 and stats["failed_budget"] == 1
    assert len(state["known"]) == 2 and state["cursor"]

    fake.requests.clear()
    _rows, runs = _drain(client, state, _scope(), max_requests=5)

    assert runs <= 4
    assert set(state["known"]) == {i.removeprefix("envelope:") for i in ids}
    # No envelope was fetched twice across the resumed runs.
    fetched = fake.requested("/documents/combined")
    assert len(fetched) == len(set(fetched)) == 4


def test_tiny_budget_still_makes_progress(fake, client):
    fake.add_envelope("A", "text")
    fake.add_envelope("B", "text")
    state: dict = {}
    stats: dict = {}

    rows = list(sync_envelopes(client, _scope(), state, stats, max_requests=1))

    assert len(rows) == 1  # one envelope per run at least, so the sync converges
    assert stats["failed_budget"] == 1


def test_a_run_cut_short_forgets_nothing(fake, client):
    binned = fake.add_envelope("Binned", "text")
    fake.add_envelope("Keep", "text")
    state: dict = {}
    _sync(client, state)
    fake.move_to_recycle_bin(binned)
    for n in range(4):
        fake.add_envelope(f"New {n}", "text")

    stats: dict = {}
    rows = list(sync_envelopes(client, _scope(), state, stats, max_requests=3))

    assert stats["failed_budget"] == 1
    assert not [r for r in rows if r["_deleted"]]  # the lookup is skipped on a partial run
    assert binned in state["known"]

    rows, _ = _drain(client, state, _scope(), max_requests=3)
    assert _tombstones(rows) == {_row_id(binned)}
    assert binned not in state["known"]


def test_a_long_rate_limit_stops_cleanly_and_keeps_progress(fake, client):
    for n in range(4):
        fake.add_envelope(f"Agreement {n}", "text")
    state: dict = {}
    reset = str(int(time.time()) + 3600)
    fake.fail_once("/documents/combined", 200)  # a successful fetch is not a failure
    fake.failures.clear()
    first_pdf = {"n": 0}
    original_send = fake.send

    def send(request, **kwargs):
        if request.url.endswith("/documents/combined?certificate=false"):
            first_pdf["n"] += 1
            if first_pdf["n"] == 3:
                return fake._json(
                    request,
                    429,
                    {"errorCode": "HOURLY_APIINVOCATION_LIMIT"},
                    {"X-RateLimit-Reset": reset},
                )
        return original_send(request, **kwargs)

    fake.send = send
    stats: dict = {}
    rows = list(sync_envelopes(client, _scope(), state, stats))

    assert len(rows) == 2 and stats["failed_rate_limit"] == 1
    assert len(state["known"]) == 2
    fake.send = original_send
    rows, _ = _drain(client, state, _scope())
    assert len(state["known"]) == 4


def test_rate_limit_on_the_first_listing_page_is_a_clean_stop(fake, client):
    fake.add_envelope("A", "text")
    state: dict = {}
    fake.fail_once("/envelopes", 429, {"X-RateLimit-Reset": str(int(time.time()) + 3600)})
    stats: dict = {}

    assert list(sync_envelopes(client, _scope(), state, stats)) == []

    assert stats["failed_rate_limit"] == 1
    assert state["known"] == {} and state["cursor"] is None
    assert _live(_sync(client, state))  # the next run just works


def test_a_rate_limit_during_a_scope_change_aborts_instead(fake, client):
    fake.add_envelope("A", "text", folders=[CONTRACTS])
    state: dict = {}
    _sync(client, state)
    fake.fail_once("/envelopes", 429, {"X-RateLimit-Reset": str(int(time.time()) + 3600)})
    before = copy.deepcopy(state)

    with pytest.raises(DocuSignRateLimitError):
        _sync(client, state, _scope(folder_ids=["Contracts"]))  # needs the whole listing

    assert state == before


# ---------------------------------------------------------------------------
# Malformed answers and guards
# ---------------------------------------------------------------------------


def test_a_listing_that_never_ends_is_an_error(fake, client):
    for n in range(3):
        fake.add_envelope(f"Agreement {n}", "text")
    fake.page_size_cap = 100
    fake.repeat_next_uri = True
    with pytest.raises(DocuSignAPIError, match="nextUri"):
        list(
            client.paginate_envelopes(
                {"from_date": "2000-01-01", "from_to_status": "changed", "count": 1}
            )
        )


@pytest.mark.parametrize("body", ["[]", "not json at all", '"a string"'])
def test_a_body_that_is_not_a_json_object_is_never_an_empty_listing(fake, client, body):
    keep = fake.add_envelope("Keep", "text")
    state: dict = {}
    _sync(client, state)
    fake.raw_responses.append(("/envelopes", body))

    with pytest.raises(DocuSignAPIError):
        _sync(client, state)

    assert keep in state["known"]


def test_a_listed_envelope_without_an_id_is_skipped(fake, client):
    good = fake.add_envelope("Good", "text")
    fake.extra_listing = [{"status": "completed", "emailSubject": "No id"}]

    assert _live(_sync(client, {})) == {_row_id(good)}


def test_state_of_another_account_is_refused(fake, client):
    fake.add_envelope("A", "text")
    state: dict = {}
    list(sync_envelopes(client, _scope(), state, owner="acct-1"))
    assert state["owner"] == "acct-1"

    with pytest.raises(RuntimeError, match="different account"):
        list(sync_envelopes(client, _scope(), state, owner="acct-2"))
    assert list(sync_envelopes(client, _scope(), state, owner="acct-1")) == []


def test_resource_name_is_validated_and_scopes_the_state():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, pipeline_name_for_source

    one = docusign_source(
        access_token="t", account_id="a", base_uri="https://x", resource_name="docusign_acme"
    )
    two = docusign_source(
        access_token="t", account_id="a", base_uri="https://x", resource_name="docusign_beta"
    )

    assert getattr(one, PIPELINE_SCOPE_ATTR) == "docusign_acme"
    assert pipeline_name_for_source(one, "ds") != pipeline_name_for_source(two, "ds")
    assert "docusign_acme" in one.resources
    with pytest.raises(ValueError, match="resource_name"):
        docusign_source(access_token="t", resource_name="not valid!")
    with pytest.raises(ValueError, match="max_requests"):
        docusign_source(access_token="t", max_requests=0)


def test_check_active_runs_before_every_request(fake, rsa_keys):
    calls = {"n": 0}

    def check_active():
        calls["n"] += 1
        if calls["n"] > 3:
            raise PermissionError("connection revoked")

    session = fake.session()
    auth = DocuSignAuth(resolve_credentials(INTEGRATION_KEY, USER_ID, rsa_keys[0]), session)
    guarded = DocuSignClient(
        auth, f"https://{API_HOST}", ACCOUNT_ID, session, check_active=check_active
    )
    for n in range(5):
        fake.add_envelope(f"Agreement {n}", "text")
    state: dict = {}

    with pytest.raises(PermissionError):
        _sync(guarded, state)

    assert state == {}  # a revoked connection stops the run before any state is written


def test_sync_stats_are_plain_integers(fake, client):
    fake.add_envelope("A", "text")
    stats: dict = {}
    _sync(client, {}, stats=stats)
    assert all(type(value) is int for value in stats.values())
    assert {"failed", "failed_rate_limit", "failed_budget"} <= set(stats)


# ---------------------------------------------------------------------------
# dlt pipeline: merge + hard_delete, failure safety
# ---------------------------------------------------------------------------


def _pipeline(tmp_path):
    db_path = (tmp_path / "docusign.db").as_posix()
    return dlt.pipeline(
        pipeline_name="docusign_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="docusign_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run(pipeline, fake, rsa_keys, **source_kwargs):
    source = docusign_source(
        INTEGRATION_KEY, USER_ID, rsa_keys[0], session=fake.session(), **source_kwargs
    )
    return pipeline.run(source)


def _read(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM docusign_envelopes") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_pipeline_merges_edits_and_hard_deletes_tombstones(fake, rsa_keys, tmp_path):
    keep = fake.add_envelope("Keep", "keep v1")
    binned = fake.add_envelope("Binned", "bin me")
    purged = fake.add_envelope("Purged", "purge me")
    pipeline = _pipeline(tmp_path)
    _run(pipeline, fake, rsa_keys)
    assert set(_read(pipeline)) == {_row_id(keep), _row_id(binned), _row_id(purged)}

    fake.edit_text(keep, "keep v2")
    fake.move_to_recycle_bin(binned)
    fake.purge(purged)
    _run(pipeline, fake, rsa_keys)

    rows = _read(pipeline)
    assert set(rows) == {_row_id(keep)}
    assert "keep v2" in rows[_row_id(keep)]


def test_failed_run_keeps_the_cursor_so_the_next_run_catches_up(fake, rsa_keys, tmp_path):
    fake.add_envelope("First", "text")
    pipeline = _pipeline(tmp_path)
    _run(pipeline, fake, rsa_keys)

    late = fake.add_envelope("Posted during an outage", "text")
    for _ in range(5):
        fake.fail_once("/envelopes", 503)
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the error in PipelineStepFailed
        _run(pipeline, fake, rsa_keys)
    assert _row_id(late) not in _read(pipeline)

    _run(pipeline, fake, rsa_keys)
    assert _row_id(late) in _read(pipeline)


def test_replace_runs_fall_back_to_full_snapshots(fake, rsa_keys, tmp_path):
    keep = fake.add_envelope("Keep", "text")
    gone = fake.add_envelope("Gone", "text")
    pipeline = _pipeline(tmp_path)
    source = docusign_source(INTEGRATION_KEY, USER_ID, rsa_keys[0], session=fake.session())
    pipeline.run(source, write_disposition="replace")

    fake.move_to_recycle_bin(gone)
    source = docusign_source(INTEGRATION_KEY, USER_ID, rsa_keys[0], session=fake.session())
    pipeline.run(source, write_disposition="replace")

    # dlt resets resource state under replace, so the run re-lists everything.
    assert set(_read(pipeline)) == {_row_id(keep)}


def test_sent_items_constant_is_a_system_folder():
    # guards the fake itself: tests above rely on SENT_ITEMS not being "recyclebin"
    assert SENT_ITEMS["type"] == "sentitems"
