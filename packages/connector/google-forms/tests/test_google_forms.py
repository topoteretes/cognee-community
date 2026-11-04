import sys
import os

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(TEST_DIR)
if PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

import pytest
from cognee_community_connector_google_forms.google_forms import (
    render_form_document,
    render_response_document,
    sync_google_forms,
)
from fake_forms import FakeFormsService


@pytest.fixture
def fake_service():
    service = FakeFormsService()
    service.add_form(
        form_id="form_123",
        title="Customer Satisfaction Survey",
        description="Quarterly feedback from enterprise users",
        questions=[
            {"id": "q1", "title": "How satisfied are you with cognee?", "type": "choiceQuestion"},
            {"id": "q2", "title": "What feature do you need most?", "type": "textQuestion"},
        ],
    )
    service.add_response(
        form_id="form_123",
        response_id="resp_001",
        answers={
            "q1": ["Very Satisfied"],
            "q2": ["Google Forms and Sheets connectors"],
        },
        timestamp="2026-11-04T10:00:00Z",
        respondent_email="user1@example.com",
    )
    return service


def test_render_form_document(fake_service):
    form_data = fake_service.forms().get(formId="form_123").execute()
    doc = render_form_document(form_data)

    assert doc["id"] == "gform:form_123:schema"
    assert doc["is_schema"] is True
    assert doc["_deleted"] is False
    assert "Customer Satisfaction Survey" in doc["content"]
    assert "How satisfied are you with cognee?" in doc["content"]
    assert "What feature do you need most?" in doc["content"]


def test_render_response_document(fake_service):
    form_data = fake_service.forms().get(formId="form_123").execute()
    resp_list = fake_service.forms().responses().list(formId="form_123").execute()
    resp_data = resp_list["responses"][0]

    doc = render_response_document(form_data, resp_data, include_respondent_email=False)

    assert doc["id"] == "gform:form_123:resp:resp_001"
    assert doc["is_schema"] is False
    assert doc["_deleted"] is False
    assert "user1@example.com" not in doc["content"]  # email turned off by default
    assert "How satisfied are you with cognee?" in doc["content"]
    assert "Very Satisfied" in doc["content"]
    assert "Google Forms and Sheets connectors" in doc["content"]


def test_render_response_document_with_email(fake_service):
    form_data = fake_service.forms().get(formId="form_123").execute()
    resp_list = fake_service.forms().responses().list(formId="form_123").execute()
    resp_data = resp_list["responses"][0]

    doc = render_response_document(form_data, resp_data, include_respondent_email=True)
    assert "user1@example.com" in doc["content"]


def test_sync_google_forms_initial_run(fake_service):
    state = {}
    records = list(sync_google_forms(fake_service, ["form_123"], state=state))

    assert len(records) == 2  # 1 schema + 1 response
    assert records[0]["is_schema"] is True
    assert records[1]["is_schema"] is False
    assert state.get("cursor_form_123") == "2026-11-04T10:00:00Z"


def test_sync_google_forms_incremental(fake_service):
    state = {"cursor_form_123": "2026-11-04T10:00:00Z"}

    # Add a newer response
    fake_service.add_response(
        form_id="form_123",
        response_id="resp_002",
        answers={"q1": ["Satisfied"], "q2": ["Faster indexing"]},
        timestamp="2026-11-04T11:00:00Z",
    )

    records = list(sync_google_forms(fake_service, ["form_123"], state=state))

    # Schema is always emitted + only the new response
    assert len(records) == 2
    assert records[0]["is_schema"] is True
    assert records[1]["response_id"] == "resp_002"
    assert state.get("cursor_form_123") == "2026-11-04T11:00:00Z"


def test_sync_google_forms_deleted_form_tombstone(fake_service):
    fake_service.delete_form("form_123")
    state = {}
    records = list(sync_google_forms(fake_service, ["form_123"], state=state))

    assert len(records) == 1
    assert records[0]["id"] == "gform:form_123:schema"
    assert records[0]["_deleted"] is True
