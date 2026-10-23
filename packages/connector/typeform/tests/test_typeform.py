from typing import Any

import pytest

from cognee_community_connector_typeform.typeform import (
    TYPEFORM_SOURCE_NAME,
    TYPEFORM_TABLE_NAME,
    _extract_fields_map,
    _format_answer_value,
    _format_response_to_row,
    typeform_source,
)


class FakeTypeformClient:
    def __init__(self, forms: list[dict[str, Any]], responses: dict[str, list[dict[str, Any]]]):
        self._forms = forms
        self._responses = responses

    def list_forms(self, page_size: int = 200) -> list[dict[str, Any]]:
        return self._forms

    def get_form(self, form_id: str) -> dict[str, Any]:
        for form in self._forms:
            if form["id"] == form_id:
                return form
        return {}

    def list_responses(
        self, form_id: str, since: str | None = None, page_size: int = 100
    ) -> list[dict[str, Any]]:
        all_resp = self._responses.get(form_id, [])
        if since:
            return [r for r in all_resp if r.get("submitted_at", "") > since]
        return all_resp


def test_extract_fields_map():
    form_def = {
        "id": "form_1",
        "title": "Customer Feedback",
        "fields": [
            {"id": "q1", "title": "What is your name?", "type": "short_text"},
            {
                "id": "q2",
                "title": "Contact Group",
                "type": "group",
                "properties": {
                    "fields": [
                        {"id": "q2_sub1", "title": "Email Address", "type": "email"},
                        {"id": "q2_sub2", "title": "Phone Number", "type": "phone_number"},
                    ]
                },
            },
        ],
    }
    fields_map = _extract_fields_map(form_def)
    assert fields_map["q1"] == "What is your name?"
    assert fields_map["q2"] == "Contact Group"
    assert fields_map["q2_sub1"] == "Contact Group - Email Address"
    assert fields_map["q2_sub2"] == "Contact Group - Phone Number"


def test_format_answer_value_types():
    assert _format_answer_value({"type": "text", "text": "Alice"}) == "Alice"
    assert (
        _format_answer_value({"type": "choice", "choice": {"label": "Enterprise"}}) == "Enterprise"
    )
    assert (
        _format_answer_value({"type": "choices", "choices": {"labels": ["Option A", "Option B"]}})
        == "Option A, Option B"
    )
    assert _format_answer_value({"type": "number", "number": 42}) == "42"
    assert _format_answer_value({"type": "boolean", "boolean": True}) == "Yes"
    assert _format_answer_value({"type": "boolean", "boolean": False}) == "No"
    assert _format_answer_value({"type": "date", "date": "2026-10-05"}) == "2026-10-05"
    assert (
        _format_answer_value({"type": "email", "email": "test@example.com"}) == "test@example.com"
    )
    assert _format_answer_value({"type": "url", "url": "https://cognee.ai"}) == "https://cognee.ai"
    assert (
        _format_answer_value({"type": "file_url", "file_url": "https://api.typeform.com/files/123"})
        == "https://api.typeform.com/files/123"
    )
    assert (
        _format_answer_value({"type": "payment", "payment": {"amount": "100", "currency": "USD"}})
        == "100 USD"
    )


def test_format_response_to_row():
    fields_map = {
        "f_name": "Full Name",
        "f_feedback": "Your Experience",
    }
    raw_response = {
        "response_id": "resp_001_abc",
        "submitted_at": "2026-10-05T12:00:00Z",
        "answers": [
            {"field": {"id": "f_name"}, "type": "text", "text": "Jane Doe"},
            {"field": {"id": "f_feedback"}, "type": "text", "text": "Cognee memory is phenomenal!"},
        ],
    }

    row = _format_response_to_row("form_xyz", "Product Survey", fields_map, raw_response)
    assert row is not None
    assert row["id"] == "typeform_form_xyz_resp_001_abc"
    assert "Product Survey" in row["title"]
    assert "Jane Doe" in row["text"]
    assert "Cognee memory is phenomenal!" in row["text"]
    assert row["form_id"] == "form_xyz"
    assert row["response_id"] == "resp_001_abc"
    assert row["submitted_at"] == "2026-10-05T12:00:00Z"


def test_typeform_source_integration_with_fake_client():
    fake_forms = [
        {
            "id": "form_1",
            "title": "Onboarding Survey",
            "fields": [
                {"id": "q1", "title": "Role", "type": "choice"},
            ],
        }
    ]
    fake_responses = {
        "form_1": [
            {
                "response_id": "r1",
                "submitted_at": "2026-10-01T00:00:00Z",
                "answers": [
                    {"field": {"id": "q1"}, "type": "choice", "choice": {"label": "Engineer"}}
                ],
            },
            {
                "response_id": "r2",
                "submitted_at": "2026-10-03T00:00:00Z",
                "answers": [
                    {"field": {"id": "q1"}, "type": "choice", "choice": {"label": "Founder"}}
                ],
            },
        ]
    }

    client = FakeTypeformClient(forms=fake_forms, responses=fake_responses)
    src = typeform_source(client=client)

    assert (
        getattr(src, "cognee_document_source", None) == TYPEFORM_SOURCE_NAME
        or getattr(src, "_cognee_document_source", None) == TYPEFORM_SOURCE_NAME
    )

    rows = list(src.resources[TYPEFORM_TABLE_NAME]())
    assert len(rows) == 2
    assert rows[0]["id"] == "typeform_form_1_r1"
    assert rows[1]["id"] == "typeform_form_1_r2"
    assert "Engineer" in rows[0]["text"]
    assert "Founder" in rows[1]["text"]


def test_typeform_source_incremental_filter():
    fake_forms = [
        {
            "id": "form_1",
            "title": "Onboarding Survey",
            "fields": [{"id": "q1", "title": "Role", "type": "text"}],
        }
    ]
    fake_responses = {
        "form_1": [
            {
                "response_id": "r1",
                "submitted_at": "2026-10-01T00:00:00Z",
                "answers": [{"field": {"id": "q1"}, "type": "text", "text": "Old Response"}],
            },
            {
                "response_id": "r2",
                "submitted_at": "2026-10-04T00:00:00Z",
                "answers": [{"field": {"id": "q1"}, "type": "text", "text": "New Response"}],
            },
        ]
    }

    client = FakeTypeformClient(forms=fake_forms, responses=fake_responses)
    src = typeform_source(since="2026-10-02T00:00:00Z", client=client)

    rows = list(src.resources[TYPEFORM_TABLE_NAME]())
    assert len(rows) == 1
    assert rows[0]["id"] == "typeform_form_1_r2"
    assert "New Response" in rows[0]["text"]


def test_typeform_source_missing_token_raises():
    with pytest.raises(ValueError, match="Typeform API token required"):
        typeform_source(token=None, client=None)
