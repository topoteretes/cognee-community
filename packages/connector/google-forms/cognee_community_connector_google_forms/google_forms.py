"""Google Forms connector for cognee — a ``dlt`` source that turns forms and responses into memory.

Sync one or more Google Forms into cognee, incrementally and with
forget-on-deletion. The resource produced here is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_google_forms import google_forms_source

    await cognee.remember(
        google_forms_source(form_ids=["1FAIpQLSc..."]),
        dataset_name="customer_feedback",
        primary_key="id",
        write_disposition="merge",
    )
"""

import html
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
import logging

try:
    from cognee.modules.integrations.google import GoogleAuthError, GoogleSettings
    from cognee.shared.logging_utils import get_logger
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
    logger = get_logger("google_forms_connector")
except ImportError:
    logger = logging.getLogger("google_forms_connector")
    DOCUMENT_SOURCE_ATTR = "document_source"
    class GoogleSettings:
        def __init__(self, **kwargs):
            pass
        def get_credentials(self):
            return None
    class GoogleAuthError(Exception):
        pass

try:
    from pydantic_settings import SettingsConfigDict, BaseSettings
except ImportError:
    class BaseSettings:
        pass
    def SettingsConfigDict(**kwargs):
        return {}

try:
    import dlt
except ImportError:
    class _DltShim:
        @staticmethod
        def source(*args, **kwargs):
            return lambda fn: fn
        @staticmethod
        def resource(*args, **kwargs):
            return lambda fn: fn
        class current:
            @staticmethod
            def resource_state():
                return {}
    dlt = _DltShim()

FORMS_BODY_READONLY_SCOPE = "https://www.googleapis.com/auth/forms.body.readonly"
FORMS_RESPONSES_READONLY_SCOPE = "https://www.googleapis.com/auth/forms.responses.readonly"
DEFAULT_SCOPES = [FORMS_BODY_READONLY_SCOPE, FORMS_RESPONSES_READONLY_SCOPE]

ID_PREFIX = "gform:"
NUM_RETRIES = 5


class GoogleFormsSettings(GoogleSettings):
    """cognee's Google OAuth settings, read from ``GOOGLE_FORMS_*``."""

    model_config = SettingsConfigDict(
        env_prefix="GOOGLE_FORMS_",
        env_file=".env",
        extra="ignore",
    )


def render_form_document(form_data: dict[str, Any]) -> dict[str, Any]:
    """Formats form metadata and questions into a readable document."""
    form_id = form_data.get("formId", "")
    info = form_data.get("info", {})
    title = info.get("title", "Untitled Form")
    description = info.get("description", "")
    items = form_data.get("items", [])

    content_lines = [
        f"# Form: {title}",
        f"**Form ID:** `{form_id}`",
    ]
    if description:
        content_lines.append(f"**Description:** {description}")

    content_lines.append("\n## Questions:")
    question_map = {}
    for idx, item in enumerate(items, 1):
        q_item = item.get("questionItem", {})
        question = q_item.get("question", {})
        q_id = question.get("questionId", item.get("itemId", f"q{idx}"))
        q_title = item.get("title", f"Question {idx}")
        q_desc = item.get("description", "")

        q_type = "Text"
        if "choiceQuestion" in question:
            c_type = question["choiceQuestion"].get("type", "CHOICE")
            options = [o.get("value", "") for o in question["choiceQuestion"].get("options", [])]
            q_type = f"Choice ({c_type}): {', '.join(options)}"
        elif "scaleQuestion" in question:
            sq = question["scaleQuestion"]
            q_type = f"Scale ({sq.get('low', 1)} to {sq.get('high', 5)})"

        line = f"{idx}. **{q_title}** [{q_type}]"
        if q_desc:
            line += f" - {q_desc}"
        content_lines.append(line)
        question_map[q_id] = q_title

    doc_text = "\n".join(content_lines)
    doc_id = f"{ID_PREFIX}{form_id}:schema"

    return {
        "id": doc_id,
        "name": f"Google Form: {title}",
        "content": doc_text,
        "text": doc_text,
        "type": "document",
        "form_id": form_id,
        "is_schema": True,
        "_deleted": False,
        DOCUMENT_SOURCE_ATTR: "google-forms",
    }


def render_response_document(
    form_data: dict[str, Any],
    response_data: dict[str, Any],
    include_respondent_email: bool = False,
) -> dict[str, Any]:
    """Formats a single form response into a structured document linking questions to answers."""
    form_id = form_data.get("formId", "")
    info = form_data.get("info", {})
    form_title = info.get("title", "Untitled Form")
    response_id = response_data.get("responseId", "")
    submitted_time = response_data.get("lastSubmittedTime") or response_data.get("createTime", "")
    respondent_email = response_data.get("respondentEmail", "")

    # Build question lookup
    items = form_data.get("items", [])
    q_lookup = {}
    for item in items:
        q_item = item.get("questionItem", {})
        question = q_item.get("question", {})
        q_id = question.get("questionId", item.get("itemId", ""))
        if q_id:
            q_lookup[q_id] = item.get("title", "Question")

    content_lines = [
        f"# Response to: {form_title}",
        f"**Form ID:** `{form_id}`",
        f"**Response ID:** `{response_id}`",
        f"**Submitted At:** `{submitted_time}`",
    ]
    if include_respondent_email and respondent_email:
        content_lines.append(f"**Respondent:** {respondent_email}")

    content_lines.append("\n## Answers:")
    answers = response_data.get("answers", {})
    for q_id, ans_obj in answers.items():
        q_title = q_lookup.get(q_id, f"Question ({q_id})")
        text_answers = ans_obj.get("textAnswers", {}).get("answers", [])
        ans_values = [a.get("value", "") for a in text_answers if a.get("value")]
        ans_text = ", ".join(ans_values) if ans_values else "(No answer provided)"
        content_lines.append(f"- **{q_title}**: {ans_text}")

    doc_text = "\n".join(content_lines)
    doc_id = f"{ID_PREFIX}{form_id}:resp:{response_id}"

    return {
        "id": doc_id,
        "name": f"Form Response: {form_title} [{response_id}]",
        "content": doc_text,
        "text": doc_text,
        "type": "document",
        "form_id": form_id,
        "response_id": response_id,
        "submitted_at": submitted_time,
        "is_schema": False,
        "_deleted": False,
        DOCUMENT_SOURCE_ATTR: "google-forms",
    }


def sync_google_forms(
    service: Any,
    form_ids: list[str],
    include_respondent_email: bool = False,
    state: dict[str, Any] | None = None,
):
    """Core generator that fetches forms and responses and yields normalized records."""
    if state is None:
        state = {}

    for form_id in form_ids:
        try:
            form_data = service.forms().get(formId=form_id).execute(num_retries=NUM_RETRIES)
        except Exception as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status == 404:
                logger.warning(f"Form {form_id} was deleted (404). Emitting tombstone.")
                yield {
                    "id": f"{ID_PREFIX}{form_id}:schema",
                    "_deleted": True,
                    DOCUMENT_SOURCE_ATTR: "google-forms",
                }
                continue
            raise

        # Emit form schema document
        yield render_form_document(form_data)

        # Handle incremental responses
        cursor_key = f"cursor_{form_id}"
        prev_cursor = state.get(cursor_key)
        params: dict[str, Any] = {"formId": form_id}
        if prev_cursor:
            params["filter"] = f"timestamp >= {prev_cursor}"

        latest_timestamp = prev_cursor
        seen_responses = set()

        try:
            resp_list = service.forms().responses().list(**params).execute(num_retries=NUM_RETRIES)
            responses = resp_list.get("responses", [])
            for r in responses:
                r_id = r.get("responseId")
                seen_responses.add(r_id)
                sub_time = r.get("lastSubmittedTime") or r.get("createTime", "")
                if prev_cursor and sub_time <= prev_cursor:
                    continue
                if sub_time and (latest_timestamp is None or sub_time > latest_timestamp):
                    latest_timestamp = sub_time
                yield render_response_document(form_data, r, include_respondent_email)

            if latest_timestamp:
                state[cursor_key] = latest_timestamp

        except Exception as e:
            logger.error(f"Failed to list responses for form {form_id}: {e}")
            raise


@dlt.source(name="google_forms")
def google_forms_source(
    form_ids: list[str],
    service: Any = None,
    include_respondent_email: bool = False,
):
    """Produces the dlt resource for Google Forms schemas and responses."""
    if service is None:
        # Build live Google Forms API client if not provided
        from googleapiclient.discovery import build
        settings = GoogleFormsSettings()
        service = build("forms", "v1", credentials=settings.get_credentials())

    @dlt.resource(name="google_forms_data", primary_key="id", write_disposition="merge")
    def forms_resource():
        state = dlt.current.resource_state()
        yield from sync_google_forms(
            service=service,
            form_ids=form_ids,
            include_respondent_email=include_respondent_email,
            state=state,
        )

    return forms_resource
