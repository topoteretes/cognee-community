"""Braze connector for cognee.

The connector exports campaign and Canvas definitions, including their message
content, as normal cognee documents.  It uses Braze's ``last_edit.time[gt]``
filter to fetch changed records while keeping a lightweight set of upstream IDs
for forget-on-delete reconciliation.
"""

from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Iterable, Iterator
from typing import Any, Literal

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("braze_connector")

BRAZE_SOURCE_NAME = "braze"
BRAZE_TABLE_NAME = "braze_content"

ContentType = Literal["campaigns", "canvases"]

_PAGE_SIZE = 100
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_CONTENT_CONFIG = {
    "campaigns": {
        "list_path": "/campaigns/list",
        "details_path": "/campaigns/details",
        "response_key": "campaigns",
        "id_param": "campaign_id",
        "singular": "campaign",
    },
    "canvases": {
        "list_path": "/canvas/list",
        "details_path": "/canvas/details",
        "response_key": "canvases",
        "id_param": "canvas_id",
        "singular": "canvas",
    },
}

_INSTANCE_ENDPOINTS = {
    **{f"us-{number:02d}": f"https://rest.iad-{number:02d}.braze.com" for number in range(1, 9)},
    "us-10": "https://rest.us-10.braze.com",
    "eu-01": "https://rest.fra-01.braze.eu",
    "eu-02": "https://rest.fra-02.braze.eu",
    "au-01": "https://rest.au-01.braze.com",
    "id-01": "https://rest.id-01.braze.com",
    "jp-01": "https://rest.jp-01.braze.com",
    "kr-01": "https://rest.kr-01.braze.com",
}

_EXTRA_HINT = (
    "The Braze connector requires dlt and requests. Install with "
    '"uv sync" from packages/connector/braze.'
)


def _resolve_endpoint(rest_endpoint: str | None = None, cluster: str | None = None) -> str:
    """Resolve a configured Braze instance to its HTTPS REST endpoint."""
    if rest_endpoint and cluster:
        raise ValueError("Pass either rest_endpoint or cluster, not both.")

    value = rest_endpoint
    cluster_value = cluster
    if value is None and cluster_value is None:
        value = os.environ.get("BRAZE_REST_ENDPOINT")
        if value is None:
            cluster_value = os.environ.get("BRAZE_CLUSTER")
    if not value and cluster_value:
        normalized_cluster = cluster_value.strip().lower()
        value = _INSTANCE_ENDPOINTS.get(normalized_cluster)
        if value is None:
            if normalized_cluster.startswith("http") or "." in normalized_cluster:
                value = normalized_cluster
            elif normalized_cluster.startswith("fra-"):
                value = f"https://rest.{normalized_cluster}.braze.eu"
            elif normalized_cluster.startswith(("iad-", "us-", "au-", "id-", "jp-", "kr-")):
                value = f"https://rest.{normalized_cluster}.braze.com"
            else:
                raise ValueError(
                    f"Unknown Braze cluster {cluster_value!r}; pass the full rest_endpoint shown "
                    "under Settings > APIs and Identifiers > API Keys."
                )

    if not value:
        raise ValueError(
            "Braze REST endpoint required: pass rest_endpoint= or cluster=, or set "
            "BRAZE_REST_ENDPOINT/BRAZE_CLUSTER."
        )

    value = value.strip().rstrip("/")
    if not value.startswith(("https://", "http://")):
        value = f"https://{value}"
    return value


def _make_session(api_key: str) -> Any:
    """Build a requests session authenticated with the Braze REST API key."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError(_EXTRA_HINT) from exc

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "cognee-community-connector-braze/0.1.0",
        }
    )
    return session


def _api_get(
    session: Any,
    rest_endpoint: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET one Braze API response and validate its JSON shape."""
    response = session.get(f"{rest_endpoint}{path}", params=params or {}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Braze {path} returned a non-object response.")
    return payload


def _iter_summaries(
    session: Any,
    rest_endpoint: str,
    content_type: ContentType,
    *,
    last_edited_after: str | None = None,
    include_archived: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield campaign/Canvas summaries across Braze's page-number pagination."""
    config = _CONTENT_CONFIG[content_type]
    page = 0
    while True:
        params: dict[str, Any] = {
            "page": page,
            "include_archived": include_archived,
            "sort_direction": "asc",
        }
        if last_edited_after:
            params["last_edit.time[gt]"] = last_edited_after

        payload = _api_get(session, rest_endpoint, config["list_path"], params)
        records = payload.get(config["response_key"], [])
        if not isinstance(records, list):
            raise ValueError(
                f"Braze {config['list_path']} response field "
                f"{config['response_key']!r} is not a list."
            )
        yield from records
        if len(records) < _PAGE_SIZE:
            return
        page += 1


def _clean_text(value: Any) -> str:
    """Turn a scalar/structured message field into searchable plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()
        return re.sub(r"\s+([.,!?;:])", r"\1", text)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _message_sections(detail: dict[str, Any], content_type: ContentType) -> list[str]:
    """Render campaign messages or Canvas step messages as Markdown sections."""
    sections: list[str] = []
    message_groups: list[tuple[str, dict[str, Any]]] = []

    if content_type == "campaigns":
        messages = detail.get("messages") or {}
        if isinstance(messages, dict):
            message_groups.append(("Messages", messages))
    else:
        for step in detail.get("steps") or []:
            if not isinstance(step, dict):
                continue
            messages = step.get("messages") or {}
            if isinstance(messages, dict) and messages:
                step_name = step.get("name") or step.get("id") or "Unnamed step"
                message_groups.append((f"Step: {step_name}", messages))

    for group_name, messages in message_groups:
        sections.append(f"## {group_name}")
        for variation_id, message in messages.items():
            if not isinstance(message, dict):
                continue
            display_name = message.get("name") or variation_id
            sections.append(f"### {display_name}")
            sections.append(f"Variation ID: {variation_id}")
            for field, value in message.items():
                text = _clean_text(value)
                if text:
                    label = field.replace("_", " ").title()
                    sections.append(f"{label}: {text}")
    return sections


def _detail_to_row(
    content_type: ContentType,
    summary: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    """Flatten one Braze details response into a document-mode dlt row."""
    config = _CONTENT_CONFIG[content_type]
    source_id = str(summary.get("id") or "")
    name = detail.get("name") or summary.get("name") or source_id
    singular = config["singular"]

    lines = [f"# Braze {singular}: {name}", f"Braze ID: {source_id}"]
    for field in (
        "description",
        "created_at",
        "updated_at",
        "archived",
        "draft",
        "enabled",
        "schedule_type",
        "channels",
        "tags",
        "teams",
    ):
        value = detail.get(field, summary.get(field))
        text = _clean_text(value)
        if text:
            lines.append(f"{field.replace('_', ' ').title()}: {text}")

    lines.extend(_message_sections(detail, content_type))
    return {
        # Namespace IDs because campaign and Canvas identifiers occupy separate
        # Braze domains but share this connector's one document table.
        "id": f"{singular}:{source_id}",
        "source_id": source_id,
        "content_type": singular,
        "title": str(name),
        "last_edited": summary.get("last_edited") or detail.get("updated_at") or "",
        "content": "\n\n".join(lines),
        "_deleted": False,
    }


def _deleted_row(content_type: ContentType, source_id: str) -> dict[str, Any]:
    singular = _CONTENT_CONFIG[content_type]["singular"]
    return {"id": f"{singular}:{source_id}", "_deleted": True}


def _selected_ids(
    content_type: ContentType,
    campaign_ids: set[str] | None,
    canvas_ids: set[str] | None,
) -> set[str] | None:
    return campaign_ids if content_type == "campaigns" else canvas_ids


def sync_braze_content(
    session: Any,
    rest_endpoint: str,
    state: dict[str, Any],
    *,
    content_types: tuple[ContentType, ...] = ("campaigns", "canvases"),
    campaign_ids: set[str] | None = None,
    canvas_ids: set[str] | None = None,
    include_archived: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield only new/changed Braze records plus hard-delete markers.

    Every run performs a lightweight unfiltered list sweep for current IDs.
    After the first run, a second list request uses Braze's
    ``last_edit.time[gt]`` cursor and only those records have their details
    downloaded.  Comparing the sweep with ``known_ids`` detects deletions.
    State is advanced only after all detail requests finish successfully.
    """
    for content_type in content_types:
        config = _CONTENT_CONFIG[content_type]
        type_state = state.setdefault(content_type, {})
        known_ids = set(type_state.get("known_ids", []))
        cursor = str(type_state.get("last_edited", ""))
        allowed_ids = _selected_ids(content_type, campaign_ids, canvas_ids)

        current_summaries = list(
            _iter_summaries(
                session,
                rest_endpoint,
                content_type,
                include_archived=include_archived,
            )
        )
        if allowed_ids is not None:
            current_summaries = [
                item for item in current_summaries if str(item.get("id")) in allowed_ids
            ]
        current_by_id = {
            str(item["id"]): item for item in current_summaries if item.get("id") is not None
        }
        current_ids = set(current_by_id)

        if cursor:
            changed_summaries = list(
                _iter_summaries(
                    session,
                    rest_endpoint,
                    content_type,
                    last_edited_after=cursor,
                    include_archived=include_archived,
                )
            )
            changed_ids = {
                str(item["id"])
                for item in changed_summaries
                if item.get("id") is not None
                and (allowed_ids is None or str(item["id"]) in allowed_ids)
            }
        else:
            changed_ids = current_ids

        # A newly selected/restored item must be fetched even if its timestamp
        # is at or behind the cursor boundary.
        ids_to_fetch = (changed_ids | (current_ids - known_ids)) & current_ids
        newest_cursor = cursor
        changed_count = 0
        for source_id in current_by_id:
            summary = current_by_id[source_id]
            last_edited = str(summary.get("last_edited") or "")
            if last_edited > newest_cursor:
                newest_cursor = last_edited
            if source_id not in ids_to_fetch:
                continue
            detail = _api_get(
                session,
                rest_endpoint,
                config["details_path"],
                {config["id_param"]: source_id},
            )
            yield _detail_to_row(content_type, summary, detail)
            changed_count += 1

        deleted_ids = known_ids - current_ids
        for source_id in sorted(deleted_ids):
            yield _deleted_row(content_type, source_id)

        type_state["known_ids"] = sorted(current_ids)
        type_state["last_edited"] = newest_cursor
        logger.info(
            "Braze: synced %d changed %s, reconciled %d deletion(s).",
            changed_count,
            content_type,
            len(deleted_ids),
        )


def braze_source(
    *,
    api_key: str | None = None,
    rest_endpoint: str | None = None,
    cluster: str | None = None,
    content_types: Iterable[ContentType] = ("campaigns", "canvases"),
    campaign_ids: Iterable[str] | None = None,
    canvas_ids: Iterable[str] | None = None,
    include_archived: bool = False,
    session: Any = None,
):
    """Create a dlt source for Braze campaigns and Canvases.

    ``content_types`` selects which object families to ingest. Optional ID
    collections further restrict the selected family. Credentials fall back to
    ``BRAZE_API_KEY`` and the endpoint falls back to
    ``BRAZE_REST_ENDPOINT``/``BRAZE_CLUSTER``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    selected_types = tuple(dict.fromkeys(content_types))
    invalid_types = set(selected_types) - set(_CONTENT_CONFIG)
    if invalid_types:
        raise ValueError(f"Unsupported Braze content_types: {sorted(invalid_types)!r}")
    if not selected_types:
        raise ValueError("Select at least one Braze content type: campaigns or canvases.")

    resolved_endpoint = _resolve_endpoint(rest_endpoint, cluster)
    resolved_key = api_key or os.environ.get("BRAZE_API_KEY")
    if session is None and not resolved_key:
        raise ValueError("Braze REST API key required: pass api_key= or set BRAZE_API_KEY.")

    selected_campaign_ids = None if campaign_ids is None else {str(item) for item in campaign_ids}
    selected_canvas_ids = None if canvas_ids is None else {str(item) for item in canvas_ids}

    @dlt.resource(
        name=BRAZE_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def braze_content():
        client = session or _make_session(str(resolved_key))
        resource_state = dlt.current.resource_state()
        yield from sync_braze_content(
            client,
            resolved_endpoint,
            resource_state,
            content_types=selected_types,
            campaign_ids=selected_campaign_ids,
            canvas_ids=selected_canvas_ids,
            include_archived=include_archived,
        )

    @dlt.source(name=BRAZE_SOURCE_NAME)
    def _braze():
        return braze_content

    source = _braze()
    setattr(source, DOCUMENT_SOURCE_ATTR, BRAZE_SOURCE_NAME)
    return source
