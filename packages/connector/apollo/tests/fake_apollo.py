"""In-memory stand-in for the Apollo REST API, shaped like its real responses."""

import math

from cognee_community_connector_apollo.apollo import ApolloNotFoundError

CONTACT_STAGES = [{"id": "cs1", "name": "Cold"}, {"id": "cs2", "name": "Interested"}]
ACCOUNT_STAGES = [{"id": "as1", "name": "Prospect"}, {"id": "as2", "name": "Customer"}]
LABELS = [{"id": "l1", "name": "Champions"}, {"id": "l2", "name": "Key Accounts"}]
# /fields prefixes ids with the modality; records key their values by the bare id
FIELDS = [
    {"id": "contact.f1", "label": "Messaging Angle"},
    {"id": "account.f2", "label": "Tier"},
]


class FakeApollo:
    def __init__(self):
        self.contacts: dict[str, dict] = {}
        self.accounts: dict[str, dict] = {}
        self.sequences: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        # still in apollo but missing from search results, like a record skipped by a page shift
        self.hidden: set[str] = set()
        self.total_override: int | None = None
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None
        self.fail_paths: dict[str, Exception] = {}
        self.calls: list[tuple[str, str]] = []
        self.rate_limit: dict[str, int | None] = {}
        self._clock = 0

    # -- seeding -----------------------------------------------------------
    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-10-01T10:{self._clock // 60:02d}:{self._clock % 60:02d}.000Z"

    def add_contact(self, contact_id: str, name: str, **fields) -> dict:
        self.contacts[contact_id] = {
            "id": contact_id,
            "name": name,
            "title": "Engineer",
            "organization_name": "Acme",
            "contact_stage_id": "cs1",
            "label_ids": [],
            "typed_custom_fields": {},
            "contact_campaign_statuses": [],
            "emailer_campaign_ids": [],
            "last_activity_date": None,
            "created_at": self._stamp(),
            "updated_at": "2026-10-01T09:00:00.000Z",
            # enrichment and channels the connector must never render
            "email": f"{contact_id}@example.com",
            "phone_numbers": [{"raw_number": "+1 555 0100"}],
            "linkedin_url": f"https://linkedin.com/in/{contact_id}-enriched",
            "headline": "Enriched headline",
            "email_status": "verified",
            "organization": {"name": "Acme", "industry": "Enriched industry"},
            **fields,
        }
        return self.contacts[contact_id]

    def add_account(self, account_id: str, name: str, **fields) -> dict:
        # apollo returns no updated_at on accounts
        self.accounts[account_id] = {
            "id": account_id,
            "name": name,
            "domain": f"{account_id}.example.com",
            "account_stage_id": "as1",
            "label_ids": [],
            "typed_custom_fields": {},
            "created_at": self._stamp(),
            "linkedin_url": f"https://linkedin.com/company/{account_id}-enriched",
            "industry": "Enriched industry",
            "estimated_num_employees": 5000,
            **fields,
        }
        return self.accounts[account_id]

    def add_sequence(self, sequence_id: str, name: str, **fields) -> dict:
        self.sequences[sequence_id] = {
            "id": sequence_id,
            "name": name,
            "active": True,
            "archived": False,
            "num_steps": 3,
            "unique_opened": 7,
            "open_rate": 0.4,
            **fields,
        }
        return self.sequences[sequence_id]

    def enroll(self, contact_id: str, sequence_id: str, *, status: str = "active") -> None:
        """Enroll a contact the way apollo does: statuses change, updated_at does not."""
        contact = self.contacts[contact_id]
        contact["contact_campaign_statuses"] = [
            {"id": f"ccs-{contact_id}", "emailer_campaign_id": sequence_id, "status": status}
        ]
        contact["emailer_campaign_ids"] = [sequence_id]
        self.events.setdefault(contact_id, []).append(
            {
                "type": "enrolled",
                "occurred_at": self._stamp(),
                "sequence_id": sequence_id,
                "sequence_name": self.sequences[sequence_id]["name"],
            }
        )

    # -- the api -----------------------------------------------------------
    def count(self, path: str) -> int:
        return sum(1 for _, called in self.calls if called == path)

    def request(self, method: str, path: str, *, body=None, params=None):
        self.calls.append((method, path))
        if path in self.fail_paths:
            raise self.fail_paths[path]
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise self.fail_with
        body = body or {}
        if path == "/contact_stages":
            return {"contact_stages": CONTACT_STAGES}
        if path == "/account_stages":
            return {"account_stages": ACCOUNT_STAGES}
        if path == "/labels":
            return LABELS
        if path == "/fields":
            return {"fields": FIELDS, "field_groups": []}
        if path == "/emailer_campaigns/search":
            return self._page("emailer_campaigns", list(self.sequences.values()), body)
        if path == "/contacts/search":
            return self._page("contacts", self._search(self.contacts, "contact", body), body)
        if path == "/accounts/search":
            return self._page("accounts", self._search(self.accounts, "account", body), body)
        if path == "/emailer_campaigns/activity_feed":
            contact_id = body["contact_id"]
            if contact_id not in self.contacts:
                raise ApolloNotFoundError("Apollo record not found (HTTP 404)")
            return {"contact_id": contact_id, "events": list(self.events.get(contact_id, []))}
        for kind, store in (("contact", self.contacts), ("account", self.accounts)):
            prefix = f"/{kind}s/"
            if method == "GET" and path.startswith(prefix):
                record = store.get(path[len(prefix) :])
                if record is None:
                    raise ApolloNotFoundError("Apollo record not found (HTTP 422)")
                return {kind: dict(record)}
        raise AssertionError(f"unexpected call {method} {path}")

    def _search(self, store: dict, kind: str, body: dict) -> list[dict]:
        stage_ids = body.get(f"{kind}_stage_ids")
        label_ids = body.get(f"{kind}_label_ids")
        records = [
            r
            for r in store.values()
            if r["id"] not in self.hidden
            and (not stage_ids or r[f"{kind}_stage_id"] in stage_ids)
            and (not label_ids or set(r["label_ids"]) & set(label_ids))
        ]
        assert body.get("sort_by_field") == f"{kind}_created_at"
        return sorted(records, key=lambda r: r["created_at"])

    def _page(self, key: str, records: list[dict], body: dict) -> dict:
        page, per_page = body.get("page", 1), body.get("per_page", 100)
        total = self.total_override if self.total_override is not None else len(records)
        chunk = records[(page - 1) * per_page : page * per_page]
        return {
            key: [dict(record) for record in chunk],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total_entries": total,
                "total_pages": math.ceil(total / per_page) if per_page else 0,
            },
        }
