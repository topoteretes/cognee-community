"""In-memory stand-in for the Pipedrive REST API, shaped like its real responses."""

from cognee_community_connector_pipedrive.pipedrive import PipedriveNotFoundError

DEAL_FIELD_KEY = "a" * 40
PERSON_FIELD_KEY = "b" * 40
DEAL_FIELDS = [
    {"key": "title", "name": "Title", "field_type": "varchar"},
    {
        "key": "label",
        "name": "Label",
        "field_type": "set",
        "options": [{"id": 1, "label": "Hot"}, {"id": 2, "label": "Cold"}],
    },
    {
        "key": DEAL_FIELD_KEY,
        "name": "Contract length",
        "field_type": "enum",
        "options": [{"id": 7, "label": "12 months"}, {"id": 8, "label": "24 months"}],
    },
]
PHONE_FIELD_KEY = "d" * 40
PERSON_FIELDS = [
    {"key": PERSON_FIELD_KEY, "name": "Favourite tool", "field_type": "varchar"},
    # a custom contact channel, which must stay out of memory like the built-in phones
    {"key": PHONE_FIELD_KEY, "name": "Assistant phone", "field_type": "phone"},
]
ORGANIZATION_FIELDS: list[dict] = []
USERS = [{"id": 100, "name": "Sam Seller"}]
PIPELINES = [{"id": 1, "name": "Sales"}]
STAGES = [{"id": 11, "name": "Qualified", "pipeline_id": 1}, {"id": 12, "name": "Proposal"}]

_LISTS = {
    "/api/v2/organizations": "organizations",
    "/api/v2/persons": "persons",
    "/api/v2/deals": "deals",
    "/api/v2/activities": "activities",
}


class FakePipedrive:
    def __init__(self, company_id: int = 1, page_size: int | None = None):
        self.company_id = company_id
        # smaller pages than the connector asks for, to exercise paging
        self.page_size = page_size
        self.organizations: dict[int, dict] = {}
        self.persons: dict[int, dict] = {}
        self.deals: dict[int, dict] = {}
        self.activities: dict[int, dict] = {}
        self.notes: dict[int, dict] = {}
        # still in pipedrive but missing from listings, like a record skipped by a page shift
        self.hidden: set[str] = set()
        # the regular deal listing leaves archived deals out; True lists them anyway
        self.archived_in_listing = False
        self.fail_after: int | None = None
        # (path, n): the nth call of path and every call after it fail
        self.fail_from: tuple[str, int] | None = None
        self.fail_with: Exception | None = None
        self.fail_paths: dict[str, Exception] = {}
        # paths answered with this payload instead
        self.answers: dict[str, object] = {}
        self.calls: list[tuple[str, dict]] = []

    # -- seeding -----------------------------------------------------------
    def add_organization(self, org_id: int, name: str, **fields) -> dict:
        self.organizations[org_id] = {
            "id": org_id,
            "name": name,
            "owner_id": 100,
            "address": {"value": "1 Main St, Springfield"},
            "label_ids": [],
            "custom_fields": {},
            "is_deleted": False,
            "update_time": "2026-10-01T10:00:00Z",
            **fields,
        }
        return self.organizations[org_id]

    def add_person(self, person_id: int, name: str, **fields) -> dict:
        self.persons[person_id] = {
            "id": person_id,
            "name": name,
            "owner_id": 100,
            "org_id": None,
            "label_ids": [],
            "custom_fields": {},
            "is_deleted": False,
            # contact channels the connector must never render
            "emails": [{"value": f"{person_id}@example.com", "primary": True}],
            "phones": [{"value": "+1 555 0100", "primary": True}],
            **fields,
        }
        return self.persons[person_id]

    def add_deal(self, deal_id: int, title: str, **fields) -> dict:
        self.deals[deal_id] = {
            "id": deal_id,
            "title": title,
            "owner_id": 100,
            "person_id": None,
            "org_id": None,
            "pipeline_id": 1,
            "stage_id": 11,
            "value": 5000,
            "currency": "USD",
            "status": "open",
            "is_deleted": False,
            "is_archived": False,
            "label_ids": [],
            "custom_fields": {},
            **fields,
        }
        return self.deals[deal_id]

    def add_activity(self, activity_id: int, subject: str, **fields) -> dict:
        self.activities[activity_id] = {
            "id": activity_id,
            "subject": subject,
            "type": "call",
            "owner_id": 100,
            "deal_id": None,
            "person_id": None,
            "org_id": None,
            "due_date": "2026-10-20",
            "due_time": "",
            "done": False,
            "note": None,
            "is_deleted": False,
            **fields,
        }
        return self.activities[activity_id]

    def add_note(self, note_id: int, content: str, **fields) -> dict:
        self.notes[note_id] = {
            "id": note_id,
            "content": content,
            "user_id": 100,
            "deal_id": None,
            "person_id": None,
            "org_id": None,
            "add_time": "2026-10-02 09:00:00",
            "update_time": "2026-10-02 09:00:00",
            "active_flag": True,
            **fields,
        }
        return self.notes[note_id]

    # -- the api -----------------------------------------------------------
    def count(self, path: str) -> int:
        return sum(1 for called, _ in self.calls if called == path)

    def request(self, path: str, *, params=None, check_active=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path in self.fail_paths:
            raise self.fail_paths[path]
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise self.fail_with
        if self.fail_from is not None and self.count(self.fail_from[0]) >= self.fail_from[1]:
            self.fail_after = len(self.calls) - 1
            raise self.fail_with
        if path in self.answers:
            return self.answers[path]
        if path == "/v1/users/me":
            return _ok({"id": 100, "company_id": self.company_id, "company_domain": "acme"})
        if path == "/v1/users":
            return _ok(USERS)
        if path == "/api/v2/pipelines":
            return self._cursor_page(PIPELINES, params)
        if path == "/api/v2/stages":
            return self._cursor_page(STAGES, params)
        if path == "/v1/dealFields":
            return self._offset_page(DEAL_FIELDS, params)
        if path == "/v1/personFields":
            return self._offset_page(PERSON_FIELDS, params)
        if path == "/v1/organizationFields":
            return self._offset_page(ORGANIZATION_FIELDS, params)
        if path in _LISTS:
            kind = _LISTS[path]
            store = getattr(self, kind)
            records = [
                r
                for r in store.values()
                if f"{kind}:{r['id']}" not in self.hidden
                and not r.get("is_deleted")
                and not (kind == "deals" and r.get("is_archived") and not self.archived_in_listing)
            ]
            return self._cursor_page(records, params)
        if path == "/api/v2/deals/archived":
            records = [r for r in self.deals.values() if r.get("is_archived")]
            return self._cursor_page(records, params)
        if path == "/v1/notes":
            assert params.get("sort") == "id ASC"
            records = [r for r in self.notes.values() if f"notes:{r['id']}" not in self.hidden]
            return self._offset_page(records, params)
        for prefix, kind in (
            ("/api/v2/organizations/", "organizations"),
            ("/api/v2/persons/", "persons"),
            ("/api/v2/deals/", "deals"),
            ("/api/v2/activities/", "activities"),
            ("/v1/notes/", "notes"),
        ):
            if path.startswith(prefix):
                record = getattr(self, kind).get(int(path[len(prefix) :]))
                if record is None:
                    raise PipedriveNotFoundError("Pipedrive record not found (HTTP 404)")
                return _ok(dict(record))
        raise AssertionError(f"unexpected call {path}")

    def _size(self, params: dict) -> int:
        return self.page_size or params.get("limit", 100)

    def _cursor_page(self, records: list[dict], params: dict) -> dict:
        records = sorted(records, key=lambda r: r["id"])
        start = int(params.get("cursor") or 0)
        size = self._size(params)
        chunk = records[start : start + size]
        more = start + size < len(records)
        return {
            "success": True,
            "data": [dict(r) for r in chunk],
            "additional_data": {"next_cursor": str(start + size) if more else None},
        }

    def _offset_page(self, records: list[dict], params: dict) -> dict:
        records = sorted(records, key=lambda r: r.get("id", 0))
        start = params.get("start", 0)
        size = self._size(params)
        chunk = records[start : start + size]
        more = start + size < len(records)
        pagination = {"start": start, "limit": size, "more_items_in_collection": more}
        if more:
            pagination["next_start"] = start + size
        return {
            "success": True,
            # v1 answers an empty listing with null
            "data": [dict(r) for r in chunk] or None,
            "additional_data": {"pagination": pagination},
        }


def _ok(data) -> dict:
    return {"success": True, "data": data}
