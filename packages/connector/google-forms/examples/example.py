"""Runnable example for cognee-community Google Forms connector."""

import sys
import os

EXAMPLE_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(EXAMPLE_DIR)
if PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

import asyncio
from cognee_community_connector_google_forms import (
    google_forms_source,
    render_form_document,
    render_response_document,
)


def mock_demo():
    print("=== Google Forms Connector Demo ===")
    mock_form = {
        "formId": "demo_form_999",
        "info": {
            "title": "Developer Feedback 2026",
            "description": "Feedback on AI agent tool development",
        },
        "items": [
            {
                "itemId": "q1",
                "title": "How often do you run autonomous agent tasks?",
                "questionItem": {"question": {"questionId": "q1"}},
            }
        ],
    }
    mock_resp = {
        "responseId": "r_12345",
        "lastSubmittedTime": "2026-11-04T18:00:00Z",
        "answers": {
            "q1": {
                "textAnswers": {"answers": [{"value": "Daily / 24-7 autonomously"}]}
            }
        },
    }

    schema_doc = render_form_document(mock_form)
    print("\n[Rendered Schema Document]:")
    print(schema_doc["content"])

    resp_doc = render_response_document(mock_form, mock_resp)
    print("\n[Rendered Response Document]:")
    print(resp_doc["content"])
    print("\nVerification complete: Form & response documents ready for Cognee memory ingestion.")


if __name__ == "__main__":
    mock_demo()
