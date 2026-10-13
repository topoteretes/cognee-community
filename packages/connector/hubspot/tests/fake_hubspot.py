"""An in-memory HubSpot for tests (no token, no network).

It behaves like the endpoints the connector uses, as seen on a free HubSpot account:

  - search: GT/GTE filter on the last-modified property, ascending sort,
    ``paging.next.after`` (contacts use ``lastmodifieddate``)
  - archived records only appear in ``?archived=true`` listings, with ``archivedAt``
  - v4 batch associations, v3 batch reads, owners and pipelines

``crm()`` builds the small CRM most tests start from.
"""

from datetime import UTC, datetime

from cognee_community_connector_hubspot import hubspot as hs
from cognee_community_connector_hubspot.hubspot import HubSpotAPIError

NOW = 1_760_000_000_000  # ms
DAY = 86_400_000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


class FakeHubSpot:
    """In-memory HubSpot: records with a modified time, links, archive and merge."""

    def __init__(self):
        self.records = {}  # type -> id -> {"props", "modified", "archived_at"}
        self.links = set()  # (type_a, id_a, type_b, id_b), stored both ways
        self.owners_forbidden = False
        self.fail_listing = False
        self.calls = []

    # -- setup helpers --------------------------------------------------------
    def put(self, object_type, rid, modified, **props):
        rec = self.records.setdefault(object_type, {}).setdefault(
            str(rid), {"props": {}, "modified": modified, "archived_at": None}
        )
        rec["props"].update(props)
        rec["modified"] = modified

    def link(self, a, ida, b, idb):
        self.links.add((a, str(ida), b, str(idb)))
        self.links.add((b, str(idb), a, str(ida)))

    def unlink(self, a, ida, b, idb):
        self.links.discard((a, str(ida), b, str(idb)))
        self.links.discard((b, str(idb), a, str(ida)))

    def archive(self, object_type, rid, at):
        self.records[object_type][str(rid)]["archived_at"] = at

    def forget(self, object_type, rid):  # GDPR delete: gone without an archive entry
        del self.records[object_type][str(rid)]

    def _out(self, object_type, rid, rec):
        prop = hs.modified_property(object_type)
        props = {**rec["props"], prop: iso(rec["modified"])}
        out = {"id": rid, "properties": props, "archived": rec["archived_at"] is not None}
        if rec["archived_at"] is not None:
            out["archivedAt"] = iso(rec["archived_at"])
        return out

    def _live(self, object_type):
        return {
            rid: rec
            for rid, rec in self.records.get(object_type, {}).items()
            if rec["archived_at"] is None
        }

    # -- API ------------------------------------------------------------------
    def post(self, path, body):
        self.calls.append(("POST", path, body))
        parts = path.strip("/").split("/")
        if path.endswith("/search"):
            object_type = parts[3]
            f = body["filterGroups"][0]["filters"][0]
            assert f["propertyName"] == hs.modified_property(object_type)
            value = int(f["value"])
            keep = (lambda m: m > value) if f["operator"] == "GT" else (lambda m: m >= value)
            rows = sorted(
                (rec["modified"], rid, rec)
                for rid, rec in self._live(object_type).items()
                if keep(rec["modified"])
            )
            after = int(body.get("after", 0))
            page = rows[after : after + body["limit"]]
            assert after < hs._SEARCH_CAP, "search API rejects paging past the cap"
            out = {
                "total": len(rows),
                "results": [self._out(object_type, r, c) for _, r, c in page],
            }
            if after + body["limit"] < len(rows):
                out["paging"] = {"next": {"after": str(after + body["limit"])}}
            return out
        if path.endswith("/batch/read") and parts[1] == "v4":
            a, b = parts[3], parts[4]
            results = []
            for item in body["inputs"]:
                to = sorted(
                    i2 for (x, i, y, i2) in self.links if x == a and i == item["id"] and y == b
                )
                if to:
                    results.append(
                        {"from": {"id": item["id"]}, "to": [{"toObjectId": int(t)} for t in to]}
                    )
            return {"results": results}
        if path.endswith("/batch/read"):
            object_type = parts[3]
            live = self._live(object_type)
            return {
                "results": [
                    self._out(object_type, i["id"], live[i["id"]])
                    for i in body["inputs"]
                    if i["id"] in live
                ]
            }
        raise AssertionError(path)

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        if path == "/crm/v3/owners":
            if self.owners_forbidden:
                raise HubSpotAPIError("HTTP 403")
            return {"results": [{"id": "77", "firstName": "Sam", "lastName": "Owner"}]}
        if path.startswith("/crm/v3/pipelines/"):
            return {
                "results": [
                    {
                        "id": "default",
                        "label": "Sales Pipeline",
                        "stages": [
                            {"id": "closedwon", "label": "Closed won"},
                            {"id": "1", "label": "New"},
                            {"id": "2", "label": "Waiting on contact"},
                        ],
                    }
                ]
            }
        if path == "/account-info/v3/details":
            return {"portalId": 4242}
        if path.startswith("/crm/v3/objects/"):
            object_type = path.split("/")[4]
            if params.get("archived") == "true":
                recs = [
                    self._out(object_type, rid, rec)
                    for rid, rec in self.records.get(object_type, {}).items()
                    if rec["archived_at"] is not None
                ]
                return {"results": recs}
            if self.fail_listing:
                raise HubSpotAPIError("HTTP 500")
            return {"results": [{"id": rid} for rid in self._live(object_type)]}
        raise AssertionError(path)


def crm(fake=None):
    """A small CRM: Jane at Acme, on the Q4 Renewal deal and an SSO ticket, plus a note."""
    fake = fake or FakeHubSpot()
    fake.put(
        "contacts",
        1,
        NOW - 9 * DAY,
        firstname="Jane",
        lastname="Doe",
        email="jane@acme.com",
        jobtitle="CTO",
        hubspot_owner_id="77",
        date_of_birth="1990-01-01",
    )
    fake.put("companies", 10, NOW - 9 * DAY, name="Acme Corp", domain="acme.com", industry="SaaS")
    fake.put(
        "deals",
        20,
        NOW - 9 * DAY,
        dealname="Q4 Renewal",
        amount="12000",
        deal_currency_code="USD",
        pipeline="default",
        dealstage="closedwon",
    )
    fake.put("tickets", 30, NOW - 9 * DAY, subject="Login fails on SSO", hs_pipeline_stage="2")
    fake.put(
        "notes",
        40,
        NOW - 9 * DAY,
        hs_note_body="<p>Call went well.</p><p>Renewal <b>signed</b>.</p>",
        hs_timestamp=iso(NOW - 9 * DAY),
        hubspot_owner_id="77",
    )
    fake.link("contacts", 1, "companies", 10)
    fake.link("contacts", 1, "deals", 20)
    fake.link("contacts", 1, "tickets", 30)
    fake.link("deals", 20, "companies", 10)
    fake.link("notes", 40, "contacts", 1)
    fake.link("notes", 40, "deals", 20)
    return fake
