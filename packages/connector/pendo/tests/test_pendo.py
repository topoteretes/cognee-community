from typing import Any

import pytest

from cognee_community_connector_pendo.pendo import (
    PENDO_SOURCE_NAME,
    PENDO_TABLE_NAME,
    _format_feedback_to_row,
    _format_guide_to_row,
    _format_nps_to_row,
    pendo_source,
)


class FakePendoClient:
    def __init__(
        self,
        guides: list[dict[str, Any]] | None = None,
        feedback: list[dict[str, Any]] | None = None,
        nps: list[dict[str, Any]] | None = None,
    ):
        self._guides = guides or []
        self._feedback = feedback or []
        self._nps = nps or []

    def list_guides(self) -> list[dict[str, Any]]:
        return self._guides

    def list_feedback(self) -> list[dict[str, Any]]:
        return self._feedback

    def list_nps_responses(self) -> list[dict[str, Any]]:
        return self._nps


def test_format_guide_to_row():
    guide = {
        "id": "g_101",
        "name": "New User Onboarding Checklist",
        "state": "published",
        "segment": {"name": "Trial Accounts"},
        "lastUpdatedAt": "2026-10-04T15:00:00Z",
        "steps": [
            {"name": "Welcome Modal", "content": "Click here to connect your repository."},
            {"name": "Step 2", "content": "Configure your vector index settings."},
        ],
    }

    row = _format_guide_to_row(guide)
    assert row is not None
    assert row["id"] == "pendo_guide_g_101"
    assert "New User Onboarding Checklist" in row["title"]
    assert "Trial Accounts" in row["text"]
    assert "Click here to connect your repository." in row["text"]
    assert "Configure your vector index settings." in row["text"]
    assert row["resource_type"] == "guide"


def test_format_feedback_to_row():
    feedback = {
        "id": "fb_555",
        "title": "Add support for dark mode in dashboard",
        "description": "Our team works at night and desperately needs a dark theme option.",
        "status": "in_review",
        "priority": "high",
        "visitorId": "dev_99",
        "accountId": "acme_corp",
        "lastUpdatedAt": "2026-10-05T09:00:00Z",
    }

    row = _format_feedback_to_row(feedback)
    assert row is not None
    assert row["id"] == "pendo_feedback_fb_555"
    assert "Add support for dark mode" in row["title"]
    assert "acme_corp" in row["text"]
    assert "dark theme option" in row["text"]
    assert row["resource_type"] == "feedback"


def test_format_nps_to_row_with_comment():
    nps = {
        "id": "nps_777",
        "score": 10,
        "comment": "The graph memory retrieval is blindingly fast and accurate!",
        "visitorId": "user_42",
        "accountId": "enterprise_inc",
        "createdAt": "2026-10-05T11:00:00Z",
    }

    row = _format_nps_to_row(nps)
    assert row is not None
    assert row["id"] == "pendo_nps_777"
    assert "Promoter (9-10)" in row["text"]
    assert "blindingly fast and accurate!" in row["text"]
    assert row["resource_type"] == "nps"


def test_format_nps_to_row_skips_empty_comments():
    nps_empty = {
        "id": "nps_888",
        "score": 8,
        "comment": "   ",
        "visitorId": "user_11",
    }
    assert _format_nps_to_row(nps_empty) is None


def test_pendo_source_integration_with_fake_client():
    fake_guides = [{"id": "g1", "name": "Feature Tour", "lastUpdatedAt": "2026-10-01T00:00:00Z"}]
    fake_feedback = [
        {
            "id": "f1",
            "title": "Export CSV",
            "description": "Need CSV download",
            "lastUpdatedAt": "2026-10-02T00:00:00Z",
        }
    ]
    fake_nps = [
        {"id": "n1", "score": 9, "comment": "Great product!", "createdAt": "2026-10-03T00:00:00Z"}
    ]

    client = FakePendoClient(guides=fake_guides, feedback=fake_feedback, nps=fake_nps)
    src = pendo_source(client=client)

    assert (
        getattr(src, "cognee_document_source", None) == PENDO_SOURCE_NAME
        or getattr(src, "_cognee_document_source", None) == PENDO_SOURCE_NAME
    )

    rows = list(src.resources[PENDO_TABLE_NAME]())
    assert len(rows) == 3
    types = {r["resource_type"] for r in rows}
    assert types == {"guide", "feedback", "nps"}


def test_pendo_source_resource_flags():
    fake_guides = [{"id": "g1", "name": "Feature Tour"}]
    fake_feedback = [{"id": "f1", "title": "Export CSV", "description": "Need CSV"}]
    fake_nps = [{"id": "n1", "score": 9, "comment": "Great!"}]

    client = FakePendoClient(guides=fake_guides, feedback=fake_feedback, nps=fake_nps)
    src = pendo_source(
        include_guides=True,
        include_feedback=False,
        include_nps=False,
        client=client,
    )

    rows = list(src.resources[PENDO_TABLE_NAME]())
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "guide"


def test_pendo_source_incremental_filter():
    fake_guides = [
        {"id": "g1", "name": "Old Guide", "lastUpdatedAt": "2026-09-01T00:00:00Z"},
        {"id": "g2", "name": "New Guide", "lastUpdatedAt": "2026-10-04T00:00:00Z"},
    ]
    client = FakePendoClient(guides=fake_guides)
    src = pendo_source(
        since="2026-10-01T00:00:00Z",
        include_feedback=False,
        include_nps=False,
        client=client,
    )

    rows = list(src.resources[PENDO_TABLE_NAME]())
    assert len(rows) == 1
    assert rows[0]["id"] == "pendo_guide_g2"


def test_pendo_source_missing_key_raises():
    with pytest.raises(ValueError, match="Pendo integration key required"):
        pendo_source(integration_key=None, client=None)
