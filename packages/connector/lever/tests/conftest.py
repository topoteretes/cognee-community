"""Shared fakes for the Lever connector tests.

``FakeLeverSession`` stands in for a ``requests`` session talking to the Lever
v1 API, so no network traffic or live API key is needed.
"""

from __future__ import annotations

import pytest

API = "https://api.lever.co/v1"
DAY_MS = 24 * 60 * 60 * 1000


def posting(
    posting_id,
    *,
    updated_at,
    text="Backend Engineer",
    state="published",
    confidentiality="non-confidential",
    description="Build APIs.",
    lists=None,
):
    return {
        "id": posting_id,
        "text": text,
        "state": state,
        "confidentiality": confidentiality,
        "createdAt": 0,
        "updatedAt": updated_at,
        "categories": {"team": "Platform", "location": "Bengaluru", "commitment": "Full-time"},
        "tags": ["python"],
        "workplaceType": "hybrid",
        "content": {
            "description": description,
            "lists": lists
            if lists is not None
            else [{"text": "Requirements", "content": "<li>Python</li><li>SQL</li>"}],
            "closing": "We are an equal opportunity employer.",
        },
        "urls": {"show": f"https://jobs.lever.co/acme/{posting_id}"},
    }


def opportunity(
    opportunity_id,
    *,
    updated_at,
    confidentiality="non-confidential",
    posting=None,
    name="Jane Candidate",
    contact=None,
    anonymized=False,
):
    return {
        "id": opportunity_id,
        "contact": contact,
        "isAnonymized": anonymized,
        # Candidate contact data that must never reach memory.
        "name": name,
        "emails": ["jane@example.com"],
        "phones": [{"value": "+1 555 0100"}],
        "headline": "Staff Engineer at Initech",
        "location": "Springfield",
        "confidentiality": confidentiality,
        "updatedAt": updated_at,
        "applications": [posting] if posting else [],
        "urls": {"show": f"https://hire.lever.co/candidates/{opportunity_id}"},
    }


def feedback(form_id, *, text="On-site interview", fields=None, deleted_at=None):
    return {
        "id": form_id,
        "text": text,
        "fields": fields
        if fields is not None
        else [
            {"text": "Rating", "type": "score-system", "value": "4 - Strong Hire"},
            {"text": "Notes", "type": "textarea", "value": "Great system design depth."},
        ],
        "deletedAt": deleted_at,
    }


def note(note_id, value, *, secret=False, deleted_at=None):
    return {
        "id": note_id,
        "text": "Note",
        "fields": [{"type": "note", "text": "Comment", "value": value}],
        "secret": secret,
        "deletedAt": deleted_at,
    }


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeLeverSession:
    """Minimal Lever v1 API: postings, opportunities, feedback, notes, delete feeds."""

    def __init__(
        self,
        postings=None,
        deleted_postings=None,
        opportunities=None,
        deleted_opportunities=None,
        feedback_by_opp=None,
        notes_by_opp=None,
        page_size=100,
    ):
        self.postings = postings or []
        self.deleted_postings = deleted_postings or []  # [{"id", "deletedAt"}]
        self.opportunities = opportunities or []
        self.deleted_opportunities = deleted_opportunities or []
        self.feedback_by_opp = feedback_by_opp or {}
        self.notes_by_opp = notes_by_opp or {}
        self.page_size = page_size
        self.calls: list[tuple[str, dict]] = []
        # Queue of synthetic responses (e.g. 429s) served before the real one.
        self.injected: list[FakeResponse] = []

    def get(self, url, params=None):
        params = dict(params or {})
        path = url.replace(API, "")
        self.calls.append((path, params))
        if self.injected:
            return self.injected.pop(0)

        if path == "/postings":
            items = [p for p in self.postings if _updated_since(p, params)]
        elif path == "/postings/deleted":
            items = _deleted_between(self.deleted_postings, params)
        elif path == "/opportunities":
            items = [o for o in self.opportunities if _updated_since(o, params)]
            wanted = params.get("posting_id")
            if wanted:
                items = [o for o in items if set(o["applications"]) & set(wanted)]
        elif path == "/opportunities/deleted":
            items = _deleted_between(self.deleted_opportunities, params)
        elif path.endswith("/feedback"):
            items = self.feedback_by_opp.get(path.split("/")[2], [])
        elif path.endswith("/notes"):
            items = self.notes_by_opp.get(path.split("/")[2], [])
        else:  # pragma: no cover - a test hit an unexpected endpoint
            return FakeResponse({"message": "not found"}, status_code=404)

        start = int(params.get("offset") or 0)
        page = items[start : start + self.page_size]
        has_next = start + self.page_size < len(items)
        payload = {"data": page, "hasNext": has_next}
        if has_next:
            payload["next"] = str(start + self.page_size)
        return FakeResponse(payload)

    def paths(self):
        return [path for path, _ in self.calls]


def _updated_since(record, params):
    start = params.get("updated_at_start")
    return start is None or record["updatedAt"] >= start


def _deleted_between(items, params):
    start = params.get("deleted_at_start", 0)
    end = params.get("deleted_at_end")
    return [i for i in items if i["deletedAt"] >= start and (end is None or i["deletedAt"] < end)]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retries back off with time.sleep; never actually wait in tests."""
    import cognee_community_connector_lever.lever as lever_module

    monkeypatch.setattr(lever_module, "_sleep", lambda _seconds: None)
