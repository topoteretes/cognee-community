"""Pendo data-source connector for cognee.

The connector turns guide copy, Pendo Listen feedback, free-form guide-poll
feedback, and NPS free text into normal cognee documents. Product-usage
counters and numeric-only survey answers are intentionally excluded: the words
written by customers and guide authors are the knowledge surface, not the
analytics around them.

Pendo does not expose a deletion feed for these objects.  Each run therefore
reads the current IDs, compares them with IDs persisted in dlt resource state,
and emits hard-delete tombstones for objects that disappeared.  Live objects
are still incremental: only a new ID or an object newer than the per-kind
cursor is yielded to dlt.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("pendo_connector")

PENDO_SOURCE_NAME = "pendo"
PENDO_TABLE_NAME = "pendo_content"
PENDO_DEFAULT_BASE_URL = "https://app.pendo.io"
PENDO_CONTENT_TYPES = frozenset({"guides", "feedback", "nps"})

_MAX_RETRIES = 5
_SURVEY_PAGE_SIZE = 500


@dataclass(frozen=True)
class _PendoConfig:
    content_types: frozenset[str]
    app_id: str | None
    guide_ids: frozenset[str]
    nps_survey_ids: tuple[str, ...]
    start_time_ms: int


class PendoClient:
    """Small authenticated client for the Pendo Engage API.

    ``http_client`` is injectable to keep unit tests offline.  A real client is
    created lazily so importing the connector never opens a network connection.
    """

    def __init__(
        self,
        integration_key: str,
        *,
        base_url: str = PENDO_DEFAULT_BASE_URL,
        timeout: float = 60.0,
        http_client: Any = None,
    ) -> None:
        if not integration_key:
            raise ValueError("A Pendo integration key is required.")

        self.base_url = base_url.rstrip("/")
        if http_client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - dependency packaging guard
                raise ImportError(
                    "The Pendo connector requires httpx. Install this package's dependencies."
                ) from exc

            http_client = httpx.Client(
                headers={
                    "x-pendo-integration-key": integration_key,
                    "accept": "application/json",
                },
                timeout=timeout,
            )
        self._http = http_client

    def _request_json(self, method: str, path: str, **kwargs) -> Any:
        """Request JSON with bounded retries for rate limits and server errors."""
        url = f"{self.base_url}{path}"
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._http.request(method, url, **kwargs)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status in (429, 500, 502, 503, 504) or status is None
                if attempt == _MAX_RETRIES - 1 or not retryable:
                    raise
                headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
                retry_after = headers.get("retry-after") or headers.get("Retry-After")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = float(2**attempt)
                logger.warning(
                    "Pendo request failed (%s); retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    def list_guides(self, *, app_id: str | None = None) -> list[dict[str, Any]]:
        """Return all guides, enriched with Pendo's summarized text fields."""
        params: dict[str, Any] = {"expand": "*"}
        if app_id:
            params = {"appId": app_id}

        raw_guides = _as_items(self._request_json("GET", "/api/v1/guide", params=params))

        # The summarized representation contains guideName/guideDescription/
        # guideContent, while the full representation carries lastUpdatedAt.
        summary_params = dict(params)
        summary_params["summarizeContent"] = "true"
        summaries = _as_items(self._request_json("GET", "/api/v1/guide", params=summary_params))
        summary_by_id = {_entity_id(item): item for item in summaries if _entity_id(item)}

        merged: list[dict[str, Any]] = []
        for guide in raw_guides:
            guide_id = _entity_id(guide)
            combined = dict(guide)
            if guide_id in summary_by_id:
                combined.update(summary_by_id[guide_id])
            merged.append(combined)
        return merged

    def list_poll_responses(
        self,
        *,
        start_time_ms: int = 0,
        guide_ids: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Return a complete poll-event inventory from the aggregation API."""
        selected_guides = set(guide_ids)
        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        source: dict[str, Any] = {
            "pollEvents": None,
            "timeSeries": {
                "period": "dayRange",
                "first": start_time_ms,
                "last": "now()",
            },
        }
        payload = {
            "response": {"mimeType": "application/json"},
            "request": {
                "name": "cognee_pendo_feedback",
                "pipeline": [{"source": source}, {"sort": ["browserTime"]}],
            },
        }
        page = _as_items(self._request_json("POST", "/api/v1/aggregation", json=payload))
        for event in page:
            if selected_guides and str(event.get("guideId") or "") not in selected_guides:
                continue
            event_id = _poll_event_id(event)
            if event_id not in seen:
                seen.add(event_id)
                events.append(event)
        return events

    def list_feedback(self, *, app_id: str | None = None) -> list[dict[str, Any]]:
        """Return the current Pendo Listen feedback snapshot via ``vocItems``."""
        source_params: dict[str, Any] = {
            "type": "feedback",
            # This is Pendo's documented expansion expression for every app in
            # a multi-app subscription. A concrete app id narrows the scope.
            "appId": [app_id] if app_id else ['expandAppIds("*")'],
        }
        pipeline: list[dict[str, Any]] = [
            {"source": {"vocItems": source_params}},
            {"sort": ["lastUpdatedAt"]},
        ]
        payload = {
            "response": {"mimeType": "application/json"},
            "request": {"name": "cognee_pendo_feedback", "pipeline": pipeline},
        }
        return _as_items(self._request_json("POST", "/api/v1/aggregation", json=payload))

    def list_survey_responses(
        self,
        survey_id: str,
        *,
        start_time_ms: int = 0,
    ) -> list[dict[str, Any]]:
        """Return Sentiment responses across 180-day date windows and cursors."""
        today = datetime.now(UTC).date()
        if start_time_ms:
            current = datetime.fromtimestamp(start_time_ms / 1000, tz=UTC).date()
        else:
            # Pendo requires ranges of at most 180 days.  Keep the default
            # practical; callers wanting older NPS can pass start_time_ms=1.
            current = today - timedelta(days=179)

        responses: list[dict[str, Any]] = []
        while current <= today:
            window_end = min(current + timedelta(days=179), today)
            after_key: str | None = None
            while True:
                params: dict[str, Any] = {
                    "startDate": current.isoformat(),
                    "endDate": window_end.isoformat(),
                    "includeAnonymous": "true",
                    "limit": _SURVEY_PAGE_SIZE,
                }
                if after_key:
                    params["afterKey"] = after_key
                payload = self._request_json(
                    "GET", f"/api/v1/survey/{survey_id}/responses", params=params
                )
                survey = payload.get("survey") or {}
                for response in payload.get("responses") or []:
                    item = dict(response)
                    item["surveyId"] = survey.get("id") or survey_id
                    item["surveyName"] = survey.get("name") or "NPS survey"
                    item["surveyType"] = survey.get("type")
                    responses.append(item)
                if not payload.get("hasMore"):
                    break
                next_key = payload.get("afterKey")
                if not next_key or next_key == after_key:
                    raise RuntimeError("Pendo Sentiment pagination did not advance.")
                after_key = next_key
            current = window_end + timedelta(days=1)
        return responses


def pendo_source(
    integration_key: str | None = None,
    *,
    content_types: Iterable[str] = ("guides", "feedback", "nps"),
    app_id: str | None = None,
    guide_ids: Iterable[str] | None = None,
    nps_survey_ids: Iterable[str] | None = None,
    start_time_ms: int | None = None,
    base_url: str | None = None,
    client: Any = None,
):
    """Return a dlt resource containing selected Pendo knowledge.

    Args:
        integration_key: Pendo Engage integration key. Falls back to
            ``PENDO_INTEGRATION_KEY``. Not needed when injecting ``client``.
        content_types: Any of ``guides``, ``feedback``, and ``nps``.
        app_id: Optional Pendo application scope.
        guide_ids: Optional guide scope, also applied to guide poll responses.
        nps_survey_ids: Optional modern Sentiment survey IDs. NPS guide polls
            are discovered automatically; these IDs add Sentiment responses.
        start_time_ms: Earliest poll/survey response timestamp to inspect.
        base_url: Regional Pendo UI origin, for example
            ``https://app.eu.pendo.io``.
        client: Pre-built client, primarily for tests.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Pendo connector requires dlt. Install with: pip install "dlt[sqlalchemy]".'
        ) from exc

    selected = frozenset(str(value).strip().lower() for value in content_types)
    invalid = selected - PENDO_CONTENT_TYPES
    if invalid:
        raise ValueError(
            f"Unsupported Pendo content_types: {sorted(invalid)}. "
            f"Choose from {sorted(PENDO_CONTENT_TYPES)}."
        )
    if not selected:
        raise ValueError("Select at least one Pendo content type.")

    resolved_start = (
        start_time_ms if start_time_ms is not None else int(os.getenv("PENDO_START_TIME_MS", "0"))
    )
    if resolved_start < 0:
        raise ValueError("start_time_ms must be zero or a positive epoch-millisecond value.")

    config = _PendoConfig(
        content_types=selected,
        app_id=app_id or os.getenv("PENDO_APP_ID"),
        guide_ids=frozenset(guide_ids or ()),
        nps_survey_ids=tuple(nps_survey_ids or ()),
        start_time_ms=resolved_start,
    )

    @dlt.resource(
        name=PENDO_TABLE_NAME,
        write_disposition="merge",
        primary_key="id",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def pendo_content():
        api = client
        if api is None:
            key = integration_key or os.getenv("PENDO_INTEGRATION_KEY")
            if not key:
                raise ValueError(
                    "Pendo integration key required: pass integration_key= or set "
                    "PENDO_INTEGRATION_KEY."
                )
            api = PendoClient(
                key,
                base_url=base_url or os.getenv("PENDO_BASE_URL", PENDO_DEFAULT_BASE_URL),
            )
        yield from _iter_rows(api, config, dlt.current.resource_state())

    resource = pendo_content()
    setattr(resource, DOCUMENT_SOURCE_ATTR, PENDO_SOURCE_NAME)
    return resource


def _iter_rows(client: Any, config: _PendoConfig, state: dict) -> Iterator[dict[str, Any]]:
    """Yield new/updated rows and deletions, then advance per-kind state."""
    rows_by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in PENDO_CONTENT_TYPES}

    # Sentiment limits one request to 180 days. When the caller leaves the
    # start at zero, pin the first run's boundary in state instead of using a
    # moving window: otherwise a live response would look deleted on day 181.
    if config.start_time_ms:
        sentiment_start_time_ms = config.start_time_ms
        state["sentiment_start_time_ms"] = sentiment_start_time_ms
    else:
        default_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=179)
        sentiment_start_time_ms = state.setdefault(
            "sentiment_start_time_ms", int(default_start.timestamp() * 1000)
        )

    # Guides are also fetched when poll responses are selected so NPS guide
    # responses can be distinguished from general qualitative feedback.
    needs_guides = bool(config.content_types & {"guides", "feedback", "nps"})
    raw_guides = client.list_guides(app_id=config.app_id) if needs_guides else []
    if config.guide_ids:
        raw_guides = [g for g in raw_guides if _entity_id(g) in config.guide_ids]

    guide_by_id = {_entity_id(guide): guide for guide in raw_guides if _entity_id(guide)}
    nps_guide_ids = {guide_id for guide_id, guide in guide_by_id.items() if _is_nps_guide(guide)}

    if "guides" in config.content_types:
        for guide in raw_guides:
            row = _guide_to_entity(guide)
            if row:
                rows_by_kind["guides"].append(row)

    if config.content_types & {"feedback", "nps"}:
        if "feedback" in config.content_types:
            for feedback in client.list_feedback(app_id=config.app_id):
                row = _feedback_to_entity(feedback)
                if row:
                    rows_by_kind["feedback"].append(row)

        poll_events = client.list_poll_responses(
            start_time_ms=config.start_time_ms,
            guide_ids=config.guide_ids,
        )
        for event in poll_events:
            guide_id = str(event.get("guideId") or "")
            kind = "nps" if guide_id in nps_guide_ids or _looks_like_nps(event) else "feedback"
            if kind not in config.content_types:
                continue
            row = _poll_to_entity(event, guide_by_id.get(guide_id), kind)
            if row:
                rows_by_kind[kind].append(row)

    if "nps" in config.content_types:
        for survey_id in config.nps_survey_ids:
            for response in client.list_survey_responses(
                survey_id, start_time_ms=sentiment_start_time_ms
            ):
                row = _survey_to_entity(response)
                if row:
                    rows_by_kind["nps"].append(row)

    known_by_kind = state.setdefault("known_ids", {})
    cursors = state.setdefault("last_updated_at", {})

    # Changing the user's selection is authoritative: content no longer
    # selected is forgotten instead of lingering in the graph forever.
    for deselected in sorted(PENDO_CONTENT_TYPES - config.content_types):
        for row_id in known_by_kind.get(deselected, []):
            yield {"id": row_id, "_deleted": True}
        known_by_kind[deselected] = []
        cursors.pop(deselected, None)

    for kind in sorted(config.content_types):
        # A duplicate can arise when an NPS response is available through both
        # pollEvents and Sentiment. Prefer the later representation by ID.
        entities = {entity["id"]: entity for entity in rows_by_kind[kind]}
        previous_ids = set(known_by_kind.get(kind, []))
        current_ids = set(entities)
        cursor = _timestamp_ms(cursors.get(kind))

        for deleted_id in sorted(previous_ids - current_ids):
            yield {"id": deleted_id, "_deleted": True}

        max_seen = cursor
        emitted = 0
        for entity_id in sorted(current_ids):
            entity = entities[entity_id]
            updated_at = entity.pop("_last_updated_at")
            max_seen = max(max_seen, updated_at)
            if entity_id not in previous_ids or updated_at > cursor:
                emitted += 1
                yield entity

        known_by_kind[kind] = sorted(current_ids)
        cursors[kind] = max_seen
        logger.info(
            "Pendo %s: %d current, %d changed, %d deleted.",
            kind,
            len(current_ids),
            emitted,
            len(previous_ids - current_ids),
        )


def _guide_to_entity(guide: Mapping[str, Any]) -> dict[str, Any] | None:
    guide_id = _entity_id(guide)
    if not guide_id:
        return None
    title = _first_text(guide, "guideName", "name", "title") or f"Pendo guide {guide_id}"
    description = _first_text(guide, "guideDescription", "description")
    guide_content = _first_text(guide, "guideContent", "content")
    content = "\n\n".join(part for part in (description, guide_content) if part).strip()
    if not content:
        content = title
    return {
        "id": f"guide:{guide_id}",
        "title": title,
        "content": content,
        "url": guide.get("guidePermalink") or guide.get("url"),
        "_deleted": False,
        "_last_updated_at": _timestamp_ms(
            guide.get("lastUpdatedAt") or guide.get("last_updated_at") or guide.get("createdAt")
        ),
    }


def _poll_to_entity(
    event: Mapping[str, Any],
    guide: Mapping[str, Any] | None,
    kind: str,
) -> dict[str, Any] | None:
    text = _free_text_response(event)
    if not text:
        return None
    event_id = _poll_event_id(event)
    guide_name = _first_text(guide or {}, "guideName", "name", "title")
    title_prefix = "NPS response" if kind == "nps" else "Pendo feedback"
    title = f"{title_prefix}: {guide_name}" if guide_name else title_prefix
    return {
        "id": f"{kind}:{event_id}",
        "title": title,
        "content": text,
        "url": (guide or {}).get("guidePermalink") or (guide or {}).get("url"),
        "_deleted": False,
        "_last_updated_at": _timestamp_ms(
            event.get("lastUpdatedAt")
            or event.get("browserTime")
            or event.get("submittedAt")
            or event.get("time")
        ),
    }


def _feedback_to_entity(feedback: Mapping[str, Any]) -> dict[str, Any] | None:
    feedback_id = _entity_id(feedback)
    if not feedback_id:
        return None

    parts: list[str] = []
    title = ""
    for field in feedback.get("formData") or []:
        if not isinstance(field, Mapping):
            continue
        content = _clean_text(field.get("content"))
        if not content or _numeric_only(content):
            continue
        question = _clean_text(field.get("title") or field.get("label"))
        if question.lower() == "title" and not title:
            title = content
        parts.append(f"{question}\n{content}" if question else content)

    # Some aggregation projections return flattened formData arrays rather
    # than the object array. Keep that valid response shape ingestible too.
    if not parts:
        contents = feedback.get("formDataContent") or []
        titles = feedback.get("formDataTitle") or []
        for index, value in enumerate(contents):
            content = _clean_text(value)
            if not content or _numeric_only(content):
                continue
            question = _clean_text(titles[index]) if index < len(titles) else ""
            if question.lower() == "title" and not title:
                title = content
            parts.append(f"{question}\n{content}" if question else content)

    if not parts:
        return None
    if not title:
        title = _first_text(feedback, "title", "name") or "Pendo feedback"

    url = _clean_text(feedback.get("submittedFromUrl")) or None
    if not url:
        for link in feedback.get("linksData") or []:
            if isinstance(link, Mapping) and _clean_text(link.get("linkUrl")):
                url = _clean_text(link.get("linkUrl"))
                break

    return {
        "id": f"feedback:{feedback_id}",
        "title": title,
        "content": "\n\n".join(parts),
        "url": url,
        "_deleted": False,
        "_last_updated_at": _timestamp_ms(
            feedback.get("lastUpdatedAt") or feedback.get("createdAt")
        ),
    }


def _survey_to_entity(response: Mapping[str, Any]) -> dict[str, Any] | None:
    texts: list[str] = []
    for answer in response.get("answers") or []:
        if not isinstance(answer, Mapping):
            continue
        question_type = str(answer.get("questionType") or "").upper()
        text = _clean_text(answer.get("text"))
        if question_type == "OPEN_ANSWER" and text and not _numeric_only(text):
            question = _clean_text(answer.get("questionText"))
            texts.append(f"{question}\n{text}" if question else text)
    if not texts:
        return None

    response_id = str(response.get("id") or "")
    if not response_id:
        response_id = _stable_hash(
            response.get("surveyId"), response.get("visitorId"), response.get("submittedAt"), texts
        )
    survey_name = _clean_text(response.get("surveyName")) or "NPS survey"
    return {
        "id": f"nps:{response_id}",
        "title": f"NPS response: {survey_name}",
        "content": "\n\n".join(texts),
        "url": None,
        "_deleted": False,
        "_last_updated_at": _timestamp_ms(
            response.get("lastUpdatedAt") or response.get("submittedAt")
        ),
    }


def _free_text_response(event: Mapping[str, Any]) -> str:
    for key in ("pollResponse", "response", "text", "answer"):
        value = event.get(key)
        if isinstance(value, str):
            text = _clean_text(value)
            if text and not _numeric_only(text):
                return text
    return ""


def _is_nps_guide(guide: Mapping[str, Any]) -> bool:
    values = [
        guide.get("type"),
        guide.get("kind"),
        guide.get("guideType"),
        guide.get("emailState"),
        guide.get("guideName"),
        guide.get("name"),
    ]
    return any("nps" in str(value).lower() for value in values if value is not None) or bool(
        guide.get("recurrence") or guide.get("emailConfiguration")
    )


def _looks_like_nps(event: Mapping[str, Any]) -> bool:
    return any(
        "nps" in str(event.get(key) or "").lower()
        for key in ("surveyType", "pollType", "guideType", "typeName")
    )


def _poll_event_id(event: Mapping[str, Any]) -> str:
    explicit = event.get("id") or event.get("responseId")
    if explicit:
        return str(explicit)
    return _stable_hash(
        event.get("guideId"),
        event.get("pollId"),
        event.get("visitorId"),
        event.get("accountId"),
        event.get("browserTime") or event.get("submittedAt") or event.get("time"),
    )


def _stable_hash(*parts: Any) -> str:
    material = "\x1f".join(repr(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _entity_id(item: Mapping[str, Any]) -> str:
    value = item.get("id") or item.get("guideId")
    return str(value) if value is not None else ""


def _as_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _timestamp_ms(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _first_text(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = _clean_text(item.get(key))
        if text:
            return text
    return ""


def _numeric_only(text: str) -> bool:
    try:
        float(text.strip())
        return True
    except ValueError:
        return False


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
    except Exception:  # pragma: no cover - HTMLParser is intentionally forgiving
        return value.strip()
    return " ".join(parser.parts).strip()


__all__ = ["PendoClient", "pendo_source"]
