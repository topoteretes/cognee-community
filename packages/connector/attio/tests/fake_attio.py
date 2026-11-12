"""An in-memory stand-in for Attio's v2 API, shaped after the documented responses."""

from typing import Any


def text(value: str) -> dict[str, Any]:
    return {"attribute_type": "text", "value": value}


def name(first: str, last: str) -> dict[str, Any]:
    return {
        "attribute_type": "personal-name",
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}",
    }


def attribute(slug: str, kind: str, title: str | None = None, archived: bool = False) -> dict:
    return {
        "api_slug": slug,
        "type": kind,
        "title": title or slug.replace("_", " ").title(),
        "is_archived": archived,
    }


def record(object_slug: str, record_id: str, values: dict[str, list[dict]]) -> dict[str, Any]:
    return {
        "id": {"workspace_id": "w", "object_id": object_slug, "record_id": record_id},
        "web_url": f"https://app.attio.com/w/{object_slug}/{record_id}",
        "values": values,
    }


class FakeAttio:
    """Answers the endpoints the connector calls, paging like the real API.

    ``page_cap`` mimics a server that silently returns fewer items than ``limit`` asks for.
    """

    def __init__(self, page_cap: int | None = None):
        self.page_cap = page_cap
        self.objects: list[dict] = []
        self.attributes: dict[tuple[str, str], list[dict]] = {}
        self.records: dict[str, list[dict]] = {}
        self.notes: list[dict] = []
        self.lists: list[dict] = []
        self.entries: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_on: str | None = None

    # -- building the workspace -------------------------------------------
    def add_object(self, slug: str, singular: str, attributes: list[dict]) -> None:
        self.objects.append({"api_slug": slug, "singular_noun": singular, "plural_noun": slug})
        self.attributes[("objects", slug)] = attributes
        self.records[slug] = []

    def add_list(self, slug: str, list_name: str, parent: str, attributes: list[dict]) -> None:
        self.lists.append({"api_slug": slug, "name": list_name, "parent_object": [parent]})
        self.attributes[("lists", slug)] = attributes
        self.entries[slug] = []

    def note(self, note_id: str, parent_object: str, parent_id: str, title: str, body: str) -> None:
        self.notes.append(
            {
                "id": {"workspace_id": "w", "note_id": note_id},
                "parent_object": parent_object,
                "parent_record_id": parent_id,
                "title": title,
                "content_plaintext": body,
                "created_at": "2026-01-01T00:00:00.000000000Z",
            }
        )

    # -- the API -----------------------------------------------------------
    def request(self, method: str, path: str, *, params=None, body=None) -> dict[str, Any]:
        self.calls.append((method, path))
        if self.fail_on and self.fail_on in path:
            from cognee_community_connector_attio.attio import AttioAPIError

            raise AttioAPIError("boom")
        parts = path.strip("/").split("/")
        if parts == ["objects"]:
            return {"data": self.objects}
        if parts == ["lists"]:
            return {"data": self.lists}
        if parts[0] in ("objects", "lists") and parts[2:] == ["attributes"]:
            return self._page(self.attributes[(parts[0], parts[1])], params)
        if parts[0] == "objects" and parts[2:] == ["records", "query"]:
            return self._page(self.records[parts[1]], body)
        if parts == ["notes"]:
            return self._page(self.notes, params)
        if parts[0] == "lists" and parts[2:] == ["entries", "query"]:
            return self._page(self.entries[parts[1]], body)
        raise AssertionError(f"unexpected request {method} {path}")

    def _page(self, items: list[dict], query: dict | None) -> dict[str, Any]:
        query = query or {}
        limit = query.get("limit", 500)
        if self.page_cap:
            limit = min(limit, self.page_cap)
        offset = query.get("offset", 0)
        return {"data": items[offset : offset + limit]}


def sample_workspace(page_cap: int | None = None) -> FakeAttio:
    """People, companies and a custom ``deals`` object that points at both, plus notes."""
    fake = FakeAttio(page_cap)
    fake.add_object(
        "people",
        "Person",
        [
            attribute("name", "personal-name", "Name"),
            attribute("email_addresses", "email-address", "Email addresses"),
            attribute("job_title", "text", "Job title"),
            attribute("company", "record-reference", "Company"),
            attribute("owner", "actor-reference", "Owner"),
            attribute("old_field", "text", "Old field", archived=True),
            attribute("record_id", "text", "Record ID"),
            attribute("created_at", "timestamp", "Created at"),
        ],
    )
    fake.add_object(
        "companies",
        "Company",
        [
            attribute("name", "text", "Name"),
            attribute("domains", "domain", "Domains"),
            attribute("employees", "number", "Employees"),
        ],
    )
    fake.add_object(
        "deals",
        "Deal",
        [
            attribute("deal_name", "text", "Deal name"),
            attribute("value", "currency", "Value"),
            attribute("stage", "status", "Stage"),
            attribute("company", "record-reference", "Company"),
        ],
    )
    fake.records["people"] = [
        record(
            "people",
            "p1",
            {
                "name": [name("Ada", "Test")],
                "email_addresses": [
                    {"attribute_type": "email-address", "email_address": "ada@example.test"}
                ],
                "job_title": [text("Engineer")],
                "company": [
                    {
                        "attribute_type": "record-reference",
                        "target_object": "companies",
                        "target_record_id": "c1",
                    }
                ],
                "owner": [
                    {
                        "attribute_type": "actor-reference",
                        "referenced_actor_type": "workspace-member",
                        "referenced_actor_id": "m1",
                    }
                ],
                "old_field": [text("hidden")],
                "record_id": [text("11111111-2222-3333-4444-555555555555")],
                "created_at": [
                    {"attribute_type": "timestamp", "value": "2026-10-19T18:52:44.179874147Z"}
                ],
            },
        ),
        record("people", "p2", {"name": [name("Grace", "Sample")]}),
    ]
    fake.records["companies"] = [
        record(
            "companies",
            "c1",
            {
                "name": [text("Acme Test Co")],
                "domains": [{"attribute_type": "domain", "domain": "acme.test"}],
                "employees": [{"attribute_type": "number", "value": 42.0}],
            },
        )
    ]
    fake.records["deals"] = [
        record(
            "deals",
            "d1",
            {
                "deal_name": [text("Pilot")],
                "value": [
                    {"attribute_type": "currency", "currency_value": 499, "currency_code": "USD"}
                ],
                "stage": [{"attribute_type": "status", "status": {"title": "Negotiating"}}],
                "company": [
                    {
                        "attribute_type": "record-reference",
                        "target_object": "companies",
                        "target_record_id": "c1",
                    }
                ],
            },
        )
    ]
    fake.note("n1", "people", "p1", "Intro call", "Ada wants a pilot next quarter.")
    fake.note("n2", "companies", "c1", "", "Acme is renewing in May.")
    fake.add_list(
        "pipeline",
        "Sales pipeline",
        "people",
        [attribute("status", "status", "Status"), attribute("notes", "text", "Notes")],
    )
    fake.entries["pipeline"] = [
        {
            "id": {"workspace_id": "w", "list_id": "l", "entry_id": "e1"},
            "parent_object": "people",
            "parent_record_id": "p1",
            "entry_values": {
                "status": [{"attribute_type": "status", "status": {"title": "Qualified"}}],
                "notes": [text("Send pricing")],
            },
        }
    ]
    return fake
