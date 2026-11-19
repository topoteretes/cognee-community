"""Unit tests for the HubSpot connector, against ``FakeHubSpot`` (see fake_hubspot.py).

Coverage: rendering (relationships by name, stage and owner labels, engagement
bodies from HTML), the property allowlist, first sync and cursor, the cursor
staying behind the search index, the 10,000-result window restart, related
documents re-rendered after a rename (notes included), archived, merged and
reconciled deletions, a failed listing or an empty response never deleting, link
order not changing a document, resource_name, check_active, missing owner scope,
auth and config errors, the dlt wiring, the row-to-document mapping in cognee, and
a real dlt merge into SQLite.
"""

import json
from json import dumps as _dumps

import pytest
from fake_hubspot import DAY, NOW, FakeHubSpot, crm

from cognee_community_connector_hubspot import hubspot as hs
from cognee_community_connector_hubspot.hubspot import (
    HubSpotAPIError,
    html_to_text,
    hubspot_source,
    iter_changed,
    record_to_row,
    sync_hubspot,
)


def run(fake, state, **kwargs):
    kwargs.setdefault("now_ms", NOW)
    kwargs.setdefault("portal_id", "4242")
    return [(t, row) for t, row in sync_hubspot(fake, state, **kwargs)]


def by_id(rows):
    return {(t, r["id"]): r for t, r in rows}


# --------------------------------------------------------------------- rendering
def test_contact_document_names_its_company_deal_and_ticket():
    rows = by_id(run(crm(), {}))
    jane = rows[("contacts", "1")]
    assert jane["title"] == "Contact: Jane Doe <jane@acme.com>"
    c = jane["content"]
    assert "Job title: CTO" in c and "Owner: Sam Owner" in c
    assert "Companies: Acme Corp (acme.com)" in c
    assert "Deals: Q4 Renewal (12000 USD, Closed won)" in c
    assert "Tickets: Login fails on SSO (Waiting on contact)" in c
    assert "1990" not in c  # date_of_birth is never read or rendered
    assert jane["url"] == "https://app.hubspot.com/contacts/4242/record/0-1/1"


def test_company_and_deal_list_the_other_side():
    rows = by_id(run(crm(), {}))
    assert "Contacts: Jane Doe <jane@acme.com>" in rows[("companies", "10")]["content"]
    deal = rows[("deals", "20")]["content"]
    assert "Pipeline: Sales Pipeline" in deal and "Companies: Acme Corp (acme.com)" in deal


def test_note_is_its_own_document_with_about_line_and_plain_text_body():
    note = by_id(run(crm(), {}))[("notes", "40")]
    assert note["title"] == "Note: Call went well."  # notes have no subject
    assert "By: Sam Owner" in note["content"]
    assert (
        "About: Contact Jane Doe <jane@acme.com>; Deal Q4 Renewal (12000 USD, Closed won)"
        in note["content"]
    )
    assert note["content"].endswith("Call went well.\nRenewal signed.")
    assert note["url"] is None


def test_long_note_title_is_cut_at_a_word():
    fake = crm()
    body = "<p>Northwind wants a 12% discount if they sign before 15 November this year.</p>"
    fake.put("notes", 40, NOW - 9 * DAY, hs_note_body=body)
    title = by_id(run(fake, {}))[("notes", "40")]["title"]
    assert title == "Note: Northwind wants a 12% discount if they sign before 15..."


def test_html_to_text_keeps_lines_and_lists():
    assert html_to_text("<p>One</p><ul><li>a</li><li>b</li></ul>") == "One\n- a\n- b"


def test_only_allowlisted_properties_are_requested():
    fake = crm()
    run(fake, {}, objects=["contacts"], extra_properties={"contacts": ["phone"]})
    body = next(b for m, p, b in fake.calls if p == "/crm/v3/objects/contacts/search")
    assert "phone" in body["properties"] and "email" in body["properties"]
    assert not {"date_of_birth", "gender", "address"} & set(body["properties"])


def test_row_is_deterministic():
    fake = crm()
    record = fake._out("companies", "10", fake.records["companies"]["10"])
    a = record_to_row("companies", record, {"contacts": ["Jane Doe"]}, "4242")
    assert a == record_to_row("companies", dict(record), {"contacts": ["Jane Doe"]}, "4242")


# --------------------------------------------------------------------- cursor
def test_first_sync_then_cursor_returns_only_changes():
    fake = crm()
    state = {}
    first = run(fake, state)
    assert len(first) == 5
    assert state["cursors"]["contacts"] == NOW - 9 * DAY
    assert run(fake, state) == []  # nothing changed

    fake.put("tickets", 30, NOW - DAY, subject="Login fails on SSO", hs_pipeline_stage="1")
    changed = by_id(run(fake, state))
    # the ticket, plus the contact that lists it (its stage label changed)
    assert set(changed) == {("tickets", "30"), ("contacts", "1")}
    assert "Login fails on SSO (New)" in changed[("contacts", "1")]["content"]


def test_contacts_are_searched_on_lastmodifieddate():
    fake = crm()
    run(fake, {}, objects=["contacts", "companies"])
    props = {
        p: b["filterGroups"][0]["filters"][0]["propertyName"]
        for m, p, b in fake.calls
        if p.endswith("/search")
    }
    assert props["/crm/v3/objects/contacts/search"] == "lastmodifieddate"
    assert props["/crm/v3/objects/companies/search"] == "hs_lastmodifieddate"


def test_cursor_stays_behind_the_search_index():
    fake = FakeHubSpot()
    fake.put("companies", 10, NOW - 30_000, name="Fresh Co")  # changed 30 s before the run
    state = {}
    run(fake, state, objects=["companies"])
    assert state["cursors"]["companies"] == NOW - hs._INDEX_LAG_MS
    # the next run reads the last two minutes again (a harmless re-read, same content)
    assert [r["id"] for _, r in run(fake, state, objects=["companies"])] == ["10"]


def test_search_window_restarts_at_the_result_cap(monkeypatch):
    monkeypatch.setattr(hs, "_SEARCH_PAGE", 2)
    monkeypatch.setattr(hs, "_SEARCH_CAP", 4)
    fake = FakeHubSpot()
    times = [1, 2, 3, 4, 4, 5, 6, 7, 8]  # two records share timestamp 4 across the cap
    for i, t in enumerate(times, start=1):
        fake.put("companies", i, t * 1000, name=f"C{i}")
    got = [r["id"] for r in iter_changed(fake, "companies", None, ["name"])]
    assert got == [str(i) for i in range(1, 10)]  # each once, in order
    searches = [b for m, p, b in fake.calls if p.endswith("/search")]
    assert any(
        b["filterGroups"][0]["filters"][0]["operator"] == "GTE"
        and b["filterGroups"][0]["filters"][0]["value"] != "0"
        for b in searches
    )


def test_window_stuck_on_one_timestamp_raises(monkeypatch):
    monkeypatch.setattr(hs, "_SEARCH_PAGE", 2)
    monkeypatch.setattr(hs, "_SEARCH_CAP", 4)
    fake = FakeHubSpot()
    for i in range(1, 8):
        fake.put("companies", i, 5000, name=f"C{i}")
    with pytest.raises(HubSpotAPIError, match="share one modified timestamp"):
        list(iter_changed(fake, "companies", None, ["name"]))


# --------------------------------------------------------------------- relationships
def test_renamed_company_refreshes_the_documents_that_name_it():
    fake = crm()
    state = {}
    run(fake, state)
    fake.put("companies", 10, NOW - DAY, name="Acme Inc", domain="acme.com")
    rows = by_id(run(fake, state))
    assert ("contacts", "1") in rows and ("deals", "20") in rows
    assert "Companies: Acme Inc (acme.com)" in rows[("contacts", "1")]["content"]


def test_metadata_only_change_does_not_refresh_linked_documents():
    # HubSpot bumps the modified date on its own (activity counts, rollups); the deal is
    # re-read, but nothing the contact or company show about it changed
    fake = crm()
    state = {}
    run(fake, state)
    fake.put("deals", 20, NOW - DAY)
    assert set(by_id(run(fake, state))) == {("deals", "20")}


def test_new_link_refreshes_the_record_it_points_to():
    fake = crm()
    state = {}
    run(fake, state)
    fake.put("contacts", 2, NOW - DAY, firstname="Sam", lastname="Lee", email="sam@acme.com")
    fake.link("contacts", 2, "companies", 10)
    rows = by_id(run(fake, state))
    assert "Sam Lee <sam@acme.com>" in rows[("companies", "10")]["content"]


def test_removed_link_refreshes_the_record_it_pointed_to():
    fake = crm()
    state = {}
    run(fake, state)
    fake.unlink("contacts", 1, "companies", 10)
    fake.put("contacts", 1, NOW - DAY)
    rows = by_id(run(fake, state))
    assert "Jane" not in rows[("companies", "10")]["content"]


def test_renamed_contact_refreshes_the_notes_about_her():
    fake = crm()
    state = {}
    run(fake, state)
    fake.put("contacts", 1, NOW - DAY, lastname="Smith", email="jane.smith@acme.com")
    rows = by_id(run(fake, state))
    assert "About: Contact Jane Smith <jane.smith@acme.com>" in rows[("notes", "40")]["content"]


# --------------------------------------------------------------------- deletion
def test_archived_contact_is_forgotten_once():
    fake = crm()
    state = {}
    run(fake, state)
    fake.archive("contacts", 1, NOW - DAY)
    rows = run(fake, state)
    assert ("contacts", {"id": "1", "_deleted": True}) in rows
    # Jane disappears from the company's document too
    assert "Jane" not in by_id(rows)[("companies", "10")]["content"]
    assert "1" not in state["known"]["contacts"]
    assert run(fake, state) == []  # the archived feed does not repeat the tombstone


def test_archived_record_never_ingested_is_ignored():
    fake = crm()
    fake.put("contacts", 2, NOW - 20 * DAY, firstname="Old")
    fake.archive("contacts", 2, NOW - 19 * DAY)
    rows = run(fake, {})
    assert not any(r.get("_deleted") for _, r in rows)


def test_merged_contact_id_is_tombstoned():
    fake = crm()
    fake.put("contacts", 2, NOW - 9 * DAY, firstname="Jane", lastname="D.", email="jd@acme.com")
    state = {}
    run(fake, state)
    fake.forget("contacts", 2)  # merged away: no archive entry
    fake.put("contacts", 1, NOW - DAY, hs_merged_object_ids="2")
    rows = by_id(run(fake, state))
    assert rows[("contacts", "2")] == {"id": "2", "_deleted": True}
    assert rows[("contacts", "1")]["_deleted"] is False


def test_reconcile_catches_records_removed_for_good():
    fake = crm()
    state = {}
    run(fake, state)
    fake.forget("tickets", 30)  # GDPR delete: no archive entry
    assert not any(r.get("_deleted") for _, r in run(fake, state))
    rows = run(fake, state, reconcile_deletes=True)
    assert ("tickets", {"id": "30", "_deleted": True}) in rows


class _Response:
    def __init__(self, content, status_code=200):
        self.status_code, self.content = status_code, content

    def json(self):
        return json.loads(self.content)


@pytest.mark.parametrize("body", [b"", b"<html>maintenance</html>", b"[]", b"null"])
def test_a_200_without_a_json_object_raises(body):
    with pytest.raises(HubSpotAPIError, match="without a JSON object"):
        hs.HubSpotClient._check(_Response(body), "/crm/v3/objects/contacts")


def test_an_empty_listing_body_never_deletes(monkeypatch):
    """Through the real client: one empty 200 on the id listing must not read as
    "no records left" and tombstone the whole CRM with reconcile_deletes."""
    fake = crm()
    state = {}
    run(fake, state)

    client = hs.HubSpotClient("token")

    def get(path, params=None):
        params = params or {}
        if path.startswith("/crm/v3/objects/") and params.get("archived") != "true":
            return _Response(b"")  # the id listing comes back empty
        return _Response(_dumps(fake.get(path, **params)).encode())

    def post(path, json=None):  # the keyword dlt's RESTClient.post is called with
        return _Response(_dumps(fake.post(path, json)).encode())

    monkeypatch.setattr(client._rest, "get", get)
    monkeypatch.setattr(client._rest, "post", post)
    with pytest.raises(HubSpotAPIError, match="without a JSON object"):
        run(client, state, reconcile_deletes=True)
    assert sorted(state["known"]["contacts"]) == ["1"]  # nothing was forgotten


def test_documents_render_the_same_whatever_order_hubspot_sends():
    import random

    def busy_crm():
        # Acme has several contacts, so the order HubSpot lists them in matters.
        fake = crm()
        for rid, (first, last) in enumerate([("Ada", "King"), ("Bo", "Li"), ("Cy", "Ng")], 2):
            fake.put("contacts", rid, NOW - 9 * DAY, firstname=first, lastname=last)
            fake.link("contacts", rid, "companies", 10)
            fake.link("notes", 40, "contacts", rid)
        return fake

    expected = by_id(run(busy_crm(), {}))
    assert "Ada King; Bo Li; Cy Ng; Jane Doe" in expected[("companies", "10")]["content"]
    for seed in range(10):
        fake = busy_crm()
        fake.shuffle = random.Random(seed)
        assert by_id(run(fake, {})) == expected


def test_failed_listing_never_deletes():
    fake = crm()
    state = {}
    run(fake, state)
    fake.forget("tickets", 30)
    fake.fail_listing = True
    with pytest.raises(HubSpotAPIError):
        run(fake, state, reconcile_deletes=True)


# --------------------------------------------------------------------- scopes / config
def test_missing_owner_scope_only_drops_the_name():
    fake = crm()
    fake.owners_forbidden = True
    jane = by_id(run(fake, {}))[("contacts", "1")]
    assert "Owner: owner 77" in jane["content"]


def test_missing_credentials_raise(monkeypatch):
    for var in (
        "HUBSPOT_ACCESS_TOKEN",
        "HUBSPOT_CLIENT_ID",
        "HUBSPOT_CLIENT_SECRET",
        "HUBSPOT_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="HUBSPOT_ACCESS_TOKEN"):
        hubspot_source()


def test_refresh_token_is_exchanged_when_app_credentials_are_set(monkeypatch):
    seen = {}

    def fake_refresh(client_id, client_secret, refresh_token):
        seen["args"] = (client_id, refresh_token)
        return "fresh-access-token"

    monkeypatch.setattr(hs, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(hs, "_portal_id", lambda client: "4242")
    hubspot_source(client_id="app", client_secret="s", refresh_token="r")
    assert seen["args"] == ("app", "r")


def test_unknown_object_type_raises():
    with pytest.raises(ValueError, match="Unknown"):
        hubspot_source(objects=["invoices"], client=FakeHubSpot(), portal_id="1")


def test_client_maps_auth_errors(monkeypatch):
    class Response:
        status_code = 401
        content = b"{}"

    client = hs.HubSpotClient("token")
    monkeypatch.setattr(client._rest, "get", lambda path, params=None: Response())
    with pytest.raises(HubSpotAPIError, match="scope"):
        client.get("/crm/v3/owners")


def test_expired_token_is_refreshed_once_mid_run(monkeypatch):
    # access tokens last 30 minutes; a long first sync must not die halfway
    class Response:
        def __init__(self, status):
            self.status_code = status
            self.content = b"{}"

        def json(self):
            return {"ok": self.status_code}

    statuses = iter([401, 200])
    minted = []
    client = hs.HubSpotClient("old", refresh=lambda: minted.append(1) or "new")
    monkeypatch.setattr(client._rest, "get", lambda path, params=None: Response(next(statuses)))
    assert client.get("/crm/v3/owners") == {"ok": 200}
    assert minted == [1]
    assert client._rest.auth.token == "new"


# --------------------------------------------------------------------- dlt wiring
def test_source_is_merge_with_hard_delete_and_document_mode():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    source = hubspot_source(client=crm(), portal_id="4242")
    resource = source.resources["hubspot"]
    assert resource.write_disposition == "merge"
    assert resource.compute_table_schema()["columns"]["_deleted"]["hard_delete"] is True
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "hubspot"
    assert getattr(source, hs.PIPELINE_SCOPE_ATTR) == "hubspot:4242"


def test_resource_name_names_the_tables_and_the_sync_state():
    source = hubspot_source(client=crm(), portal_id="4242", resource_name="crm_eu")
    assert set(source.resources) == {"crm_eu"}
    assert getattr(source, hs.PIPELINE_SCOPE_ATTR) == "hubspot:4242:crm_eu"


def test_check_active_runs_before_every_request():
    """A connection revoked mid-run stops the sync before the next API call, not
    only between emitted rows (every request happens before the first row)."""

    class AccessRevokedError(Exception):
        pass

    fake = crm()

    def revoked_after_first_request():
        if fake.calls:
            raise AccessRevokedError

    source = hubspot_source(client=fake, portal_id="4242", check_active=revoked_after_first_request)
    with pytest.raises(Exception) as caught:
        list(source.resources["hubspot"])
    causes, error = [], caught.value
    while error is not None:
        causes.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(e, AccessRevokedError) for e in causes)
    assert len(fake.calls) == 1


def test_portal_id_is_looked_up_when_not_given():
    source = hubspot_source(client=crm())
    assert getattr(source, hs.PIPELINE_SCOPE_ATTR) == "hubspot:4242"


def test_row_becomes_a_hubspot_document_in_cognee():
    # The row -> document mapping is cognee's (the same check the Notion tests do).
    from types import SimpleNamespace
    from uuid import NAMESPACE_OID, uuid5

    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = by_id(run(crm(), {}))[("contacts", "1")]
    item = _build_document_data_item(
        SimpleNamespace(row_data=row, table_name="hubspot_contacts"),
        uuid5(NAMESPACE_OID, "1"),
        "hubspot",
    )
    # source="hubspot" (not "dlt") routes the record through normal cognify.
    assert item.system_metadata["source"] == "hubspot"
    assert item.system_metadata["external_id"] == "1"
    assert item.data.startswith("# Contact: Jane Doe <jane@acme.com>")
    assert "Companies: Acme Corp (acme.com)" in item.data


def test_e2e_dlt_merge_one_table_per_object(tmp_path):
    """Real dlt merge into SQLite: one table per object type, an edit replaces a
    row, an unchanged row is kept, and an archived contact is removed."""
    dlt = pytest.importorskip("dlt")
    db = tmp_path / "hubspot.db"
    fake = crm()

    def sync():
        pipeline = dlt.pipeline(
            pipeline_name="hubspot_e2e",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db}"),
            dataset_name="hubspot_e2e",
            pipelines_dir=str(tmp_path / "pipelines"),
        )
        pipeline.run(hubspot_source(client=fake, portal_id="4242"))
        out = {}
        with pipeline.sql_client() as client:
            for table in ("hubspot_contacts", "hubspot_companies", "hubspot_deals"):
                out[table] = [
                    tuple(r)
                    for r in client.execute_sql(f"SELECT id, title FROM {table} ORDER BY id")
                ]
        return out

    first = sync()
    assert first["hubspot_contacts"] == [("1", "Contact: Jane Doe <jane@acme.com>")]
    assert first["hubspot_companies"] == [("10", "Company: Acme Corp (acme.com)")]

    fake.put("deals", 20, NOW - DAY, dealname="Q4 Renewal (signed)")
    second = sync()
    assert second["hubspot_deals"][0][1].startswith("Deal: Q4 Renewal (signed)")
    assert second["hubspot_companies"] == first["hubspot_companies"]

    fake.archive("contacts", 1, NOW - DAY)
    assert sync()["hubspot_contacts"] == []
