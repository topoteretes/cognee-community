import os
import time
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("typeform_connector")

TYPEFORM_TABLE_NAME = "typeform_responses"
TYPEFORM_SOURCE_NAME = "typeform"
TYPEFORM_API_BASE = "https://api.typeform.com"
_MAX_RETRIES = 5

_EXTRA_HINT = 'The Typeform connector requires dlt and httpx: pip install "dlt[sqlalchemy]" httpx'


def typeform_source(
    token: str | None = None,
    form_ids: list[str] | None = None,
    since: str | None = None,
    client: Any = None,
):
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = token or os.environ.get("TYPEFORM_API_KEY") or os.environ.get("TYPEFORM_TOKEN")
    if client is None and not resolved_token:
        raise ValueError(
            "Typeform API token required: pass token= or set TYPEFORM_API_KEY / TYPEFORM_TOKEN."
        )

    api_client = client or TypeformClient(token=resolved_token)

    @dlt.resource(name=TYPEFORM_TABLE_NAME, primary_key="id", write_disposition="replace")
    def typeform_responses():
        count = 0
        if form_ids is not None:
            target_forms = form_ids
        else:
            target_forms = [f["id"] for f in api_client.list_forms()]

        for form_id in target_forms:
            form_def = api_client.get_form(form_id)
            if not form_def:
                continue

            form_title = form_def.get("title", f"Form {form_id}")
            fields_map = _extract_fields_map(form_def)

            for response in api_client.list_responses(form_id, since=since):
                response_row = _format_response_to_row(form_id, form_title, fields_map, response)
                if response_row:
                    count += 1
                    yield response_row

        logger.info("Typeform: synced %d response(s).", count)

    @dlt.source(name=TYPEFORM_SOURCE_NAME)
    def _typeform():
        return typeform_responses

    source = _typeform()
    setattr(source, DOCUMENT_SOURCE_ATTR, TYPEFORM_SOURCE_NAME)
    return source


class TypeformClient:
    def __init__(self, token: str, base_url: str = TYPEFORM_API_BASE):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-typeform",
        }

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(_MAX_RETRIES):
            try:
                with httpx.Client(timeout=30.0) as http:
                    response = http.request(method, url, headers=self.headers, params=params)
                    if response.status_code == 429:
                        retry_after = float(response.headers.get("Retry-After", 2**attempt))
                        time.sleep(retry_after)
                        continue
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403, 404) or attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
        return {}

    def list_forms(self, page_size: int = 200) -> list[dict[str, Any]]:
        forms: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request("GET", "/forms", params={"page": page, "page_size": page_size})
            items = data.get("items", [])
            forms.extend(items)
            page_count = data.get("page_count", 1)
            if page >= page_count or not items:
                break
            page += 1
        return forms

    def get_form(self, form_id: str) -> dict[str, Any]:
        return self._request("GET", f"/forms/{form_id}")

    def list_responses(
        self, form_id: str, since: str | None = None, page_size: int = 100
    ) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        after = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if since:
                params["since"] = since
            if after:
                params["after"] = after

            data = self._request("GET", f"/forms/{form_id}/responses", params=params)
            items = data.get("items", [])
            responses.extend(items)

            if len(items) < page_size:
                break
            after = items[-1].get("token") or items[-1].get("response_id")
            if not after:
                break
        return responses


def _extract_fields_map(form_def: dict[str, Any]) -> dict[str, str]:
    fields_map: dict[str, str] = {}
    for field in form_def.get("fields", []):
        field_id = field.get("id")
        title = field.get("title", f"Field {field_id}")
        if field_id:
            fields_map[field_id] = title
        for sub_field in field.get("properties", {}).get("fields", []):
            sub_id = sub_field.get("id")
            sub_title = sub_field.get("title", f"Subfield {sub_id}")
            if sub_id:
                fields_map[sub_id] = f"{title} - {sub_title}"
    return fields_map


def _format_answer_value(answer: dict[str, Any]) -> str:
    answer_type = answer.get("type", "")
    if answer_type == "text":
        return str(answer.get("text", ""))
    if answer_type == "choice":
        choice = answer.get("choice", {})
        return str(choice.get("label") or choice.get("other") or "")
    if answer_type == "choices":
        choices = answer.get("choices", {})
        labels = choices.get("labels", [])
        other = choices.get("other")
        if other:
            labels.append(other)
        return ", ".join(labels)
    if answer_type == "number":
        return str(answer.get("number", ""))
    if answer_type == "boolean":
        return "Yes" if answer.get("boolean") else "No"
    if answer_type == "date":
        return str(answer.get("date", ""))
    if answer_type == "email":
        return str(answer.get("email", ""))
    if answer_type == "url":
        return str(answer.get("url", ""))
    if answer_type == "file_url":
        return str(answer.get("file_url", ""))
    if answer_type == "payment":
        payment = answer.get("payment", {})
        return f"{payment.get('amount', '')} {payment.get('currency', '')}".strip()
    return str(answer.get(answer_type, ""))


def _format_response_to_row(
    form_id: str, form_title: str, fields_map: dict[str, str], response: dict[str, Any]
) -> dict[str, Any] | None:
    response_id = response.get("response_id") or response.get("token")
    if not response_id:
        return None

    submitted_at = response.get("submitted_at") or response.get("landed_at") or ""
    answers = response.get("answers", [])

    lines = [
        f"# Typeform Submission: {form_title}",
        f"- **Form ID:** {form_id}",
        f"- **Response ID:** {response_id}",
        f"- **Submitted At:** {submitted_at}",
        "",
        "## Responses",
    ]

    for answer in answers:
        field_info = answer.get("field", {})
        field_id = field_info.get("id")
        question_text = fields_map.get(field_id, field_info.get("ref", f"Question ({field_id})"))
        answer_text = _format_answer_value(answer)
        if answer_text:
            lines.append(f"### {question_text}")
            lines.append(f"{answer_text}\n")

    full_text = "\n".join(lines).strip()
    doc_id = f"typeform_{form_id}_{response_id}"

    return {
        "id": doc_id,
        "title": f"{form_title} Submission ({response_id[:8]})",
        "text": full_text,
        "url": f"https://admin.typeform.com/form/{form_id}/results",
        "form_id": form_id,
        "response_id": response_id,
        "submitted_at": submitted_at,
    }
