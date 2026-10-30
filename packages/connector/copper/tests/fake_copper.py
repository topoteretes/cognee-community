"""In-memory stand-in for the Copper REST API, shaped like its real responses."""

from cognee_community_connector_copper.copper import CopperNotFoundError

USERS = [{"id": 100, "name": "Sam Seller", "email": "sam@example.com"}]
PIPELINES = [
    {
        "id": 1,
        "name": "Sales",
        "stages": [{"id": 11, "name": "Qualified"}, {"id": 12, "name": "Won"}],
    }
]
CONTACT_TYPES = [{"id": 21, "name": "Potential Customer"}, {"id": 22, "name": "Current Customer"}]
CUSTOMER_SOURCES = [{"id": 31, "name": "Email"}]
LOSS_REASONS = [{"id": 41, "name": "Price"}]
CUSTOM_FIELDS = [
    {
        "id": 51,
        "name": "Contract length",
        "data_type": "Dropdown",
        "available_on": ["opportunity"],
        "options": [{"id": 7, "name": "12 months"}, {"id": 8, "name": "24 months"}],
    },
    {"id": 52, "name": "Renewal date", "data_type": "Date", "available_on": ["opportunity"]},
    {"id": 53, "name": "Favourite tool", "data_type": "String", "available_on": ["person"]},
]
ACTIVITY_TYPES = {
    "user": [
        {"id": 0, "category": "user", "name": "Note", "is_disabled": False},
        {"id": 61, "category": "user", "name": "Phone Call", "is_disabled": False},
        {"id": 62, "category": "user", "name": "Old Type", "is_disabled": True},
    ],
    "system": [{"id": 1, "category": "system", "name": "Property Changed"}],
}

_SEARCHES = {
    "/companies/search": "companies",
    "/people/search": "people",
    "/opportunities/search": "opportunities",
    "/activities/search": "activities",
}


class FakeCopper:
    def __init__(self, account_id: int = 1, page_size: int | None = None):
        self.account_id = account_id
        # smaller pages than the connector asks for, to exercise paging
        self.page_size = page_size
        self.companies: dict[int, dict] = {}
        self.people: dict[int, dict] = {}
        self.opportunities: dict[int, dict] = {}
        self.activities: dict[int, dict] = {}
        self.activity_types = {k: [dict(t) for t in v] for k, v in ACTIVITY_TYPES.items()}
        # still in copper but missing from searches, like a record skipped by a page shift
        self.hidden: set[str] = set()
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None
        # (path, n): the nth call of path and every call after it fail
        self.fail_from: tuple[str, int] | None = None
        self.fail_paths: dict[str, Exception] = {}
        # paths answered with this payload instead
        self.answers: dict[str, object] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self._clock = 1_700_000_000

    # -- seeding -----------------------------------------------------------
    def _stamp(self) -> int:
        self._clock += 60
        return self._clock

    def add_company(self, company_id: int, name: str, **fields) -> dict:
        self.companies[company_id] = {
            "id": company_id,
            "name": name,
            "assignee_id": 100,
            "contact_type_id": 21,
            "details": None,
            "email_domain": f"{name.split()[0].lower()}.example",
            "tags": [],
            "custom_fields": [],
            "phone_numbers": [{"number": "+1 555 0100", "category": "work"}],
            "interaction_count": 3,
            "date_created": self._stamp(),
            "date_modified": self._clock,
            **fields,
        }
        return self.companies[company_id]

    def add_person(self, person_id: int, name: str, **fields) -> dict:
        self.people[person_id] = {
            "id": person_id,
            "name": name,
            "title": "CTO",
            "company_id": None,
            "company_name": None,
            "assignee_id": 100,
            "contact_type_id": 21,
            "details": None,
            "tags": [],
            "custom_fields": [],
            # contact channels the connector must never render
            "emails": [{"email": f"{person_id}@example.com", "category": "work"}],
            "phone_numbers": [{"number": "+1 555 0100", "category": "mobile"}],
            "interaction_count": 5,
            "date_created": self._stamp(),
            "date_modified": self._clock,
            **fields,
        }
        return self.people[person_id]

    def add_opportunity(self, opportunity_id: int, name: str, **fields) -> dict:
        self.opportunities[opportunity_id] = {
            "id": opportunity_id,
            "name": name,
            "assignee_id": 100,
            "close_date": "1/31/2027",
            "company_id": None,
            "company_name": None,
            "customer_source_id": 31,
            "details": None,
            "loss_reason_id": None,
            "pipeline_id": 1,
            "pipeline_stage_id": 11,
            "primary_contact_id": None,
            "priority": "High",
            "status": "Open",
            "tags": [],
            "monetary_unit": "USD",
            "monetary_value": 5000,
            "custom_fields": [],
            "interaction_count": 2,
            "date_created": self._stamp(),
            "date_modified": self._clock,
            **fields,
        }
        return self.opportunities[opportunity_id]

    def add_activity(
        self, activity_id: int, details: str, parent: tuple[str, int], type_id: int = 0, **fields
    ) -> dict:
        category = "system" if type_id == 1 else "user"
        self.activities[activity_id] = {
            "id": activity_id,
            "parent": {"id": parent[1], "type": parent[0]},
            "type": {"id": type_id, "category": category},
            "user_id": 100,
            "details": details,
            "activity_date": 1_761_000_000,
            "old_value": None,
            "new_value": None,
            "date_created": self._stamp(),
            "date_modified": self._clock,
            **fields,
        }
        return self.activities[activity_id]

    # -- the api -----------------------------------------------------------
    def count(self, path: str) -> int:
        return sum(1 for _, called, _ in self.calls if called == path)

    def request(self, method: str, path: str, *, body=None, check_active=None):
        body = dict(body or {})
        self.calls.append((method, path, body))
        if path in self.fail_paths:
            raise self.fail_paths[path]
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise self.fail_with
        if self.fail_from is not None and self.count(self.fail_from[0]) >= self.fail_from[1]:
            self.fail_after = len(self.calls) - 1
            raise self.fail_with
        if path in self.answers:
            return self.answers[path]
        static = {
            "/account": {"id": self.account_id, "name": "Acme Sales"},
            "/custom_field_definitions": CUSTOM_FIELDS,
            "/contact_types": CONTACT_TYPES,
            "/pipelines": PIPELINES,
            "/customer_sources": CUSTOMER_SOURCES,
            "/loss_reasons": LOSS_REASONS,
            "/activity_types": self.activity_types,
        }
        if method == "GET" and path in static:
            return static[path]
        if method == "POST" and path == "/users/search":
            return self._page(USERS, body)
        if method == "POST" and path in _SEARCHES:
            kind = _SEARCHES[path]
            records = [
                r for r in getattr(self, kind).values() if f"{kind}:{r['id']}" not in self.hidden
            ]
            if kind == "activities":
                wanted = {(t["id"], t["category"]) for t in body.get("activity_types") or []}
                if wanted:
                    records = [
                        r for r in records if (r["type"]["id"], r["type"]["category"]) in wanted
                    ]
            else:
                assert (body.get("sort_by"), body.get("sort_direction")) == ("date_created", "asc")
                records.sort(key=lambda r: r["date_created"])
            return self._page(records, body)
        if method == "GET":
            for kind in ("companies", "people", "opportunities", "activities"):
                prefix = f"/{kind}/"
                if path.startswith(prefix):
                    record = getattr(self, kind).get(int(path[len(prefix) :]))
                    if record is None:
                        raise CopperNotFoundError("Copper record not found (HTTP 404)")
                    return dict(record)
        raise AssertionError(f"unexpected call {method} {path}")

    def _page(self, records: list[dict], body: dict) -> list[dict]:
        size = self.page_size or body.get("page_size", 20)
        page = body.get("page_number", 1)
        return [dict(r) for r in records[(page - 1) * size : page * size]]
