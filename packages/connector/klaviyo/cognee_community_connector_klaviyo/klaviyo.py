"""Klaviyo connector for cognee: a ``dlt`` source that turns email marketing into memory.

One document per campaign (its messages, subjects and email copy), per flow (its
trigger and every step) and per email template, synced incrementally with
forget-on-delete. Hand the source to :func:`cognee.remember`::

    await cognee.remember(
        klaviyo_source(),  # KLAVIYO_API_KEY
        dataset_name="klaviyo",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

The README describes what is stored, how changes and deletions are found, and
what a run does when the API fails part of the way through.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("klaviyo_connector")

# Default dlt resource / staging-table name, and the document-source tag for its rows.
KLAVIYO_TABLE_NAME = "klaviyo_documents"
KLAVIYO_SOURCE_NAME = "klaviyo"

RESOURCES = ("campaigns", "flows", "templates")
DEFAULT_RESOURCES = RESOURCES

API_BASE = "https://a.klaviyo.com/api"
# Klaviyo versions its API by date and changes response shapes between
# revisions (campaigns were restructured in 2026-10-15), so every request pins
# this one. Revisions are supported for two years after release.
API_REVISION = "2026-10-15"
_API_HOST = urlsplit(API_BASE).netloc

# Per request: a generous bound for one API call, so a stalled connection fails and is retried.
_TIMEOUT_SECONDS = 30
# Five attempts per request, with 1, 2, 4, 8 s between them, or Klaviyo's
# Retry-After on a 429.
_MAX_RETRIES = 5
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
# The longest single wait, and the most a run waits in all. cognee holds its dlt
# staging lock during a sync, so a longer Retry-After, or a wait past the
# budget, ends the run instead (the next run starts over from the saved state).
_MAX_WAIT_SECONDS = 60
_MAX_TOTAL_WAIT_SECONDS = 120
# Pages per listing before it is treated as broken.
_MAX_LIST_PAGES = 10_000
# How far before the cursor the ``updated`` window starts: a record saved in the
# same second as the cursor may not have been visible to the previous run yet.
# The fingerprints keep the overlap from re-ingesting anything.
_CURSOR_OVERLAP = timedelta(seconds=60)
# Records that fail to load (5xx or no answer, after retries) in a row before the
# run is treated as an outage and stopped.
_OUTAGE_STREAK = 3
# Campaign message statuses whose content can still change. Klaviyo does not
# let a sent campaign be edited, so only these are read again on every run.
_EDITABLE_STATUSES = ("draft", "scheduled")

_STAT_KEYS = ("listed", "rendered", "unchanged", "deleted", "kept", "failed")
# Bumped whenever the text a record renders to changes shape. A run that finds
# another version in the state renders every record once; only the ones whose
# text came out different are re-ingested.
_RENDER_VERSION = 1


class KlaviyoAPIError(RuntimeError):
    """An error answer from Klaviyo (``status``), or no answer at all (``status`` 0).

    The message names only the status and the endpoint, never a record id, the
    key or a response body. Klaviyo's own error code is on ``detail``.
    """

    def __init__(self, status: int, endpoint: str, detail: str = ""):
        answer = f"HTTP {status}" if status else "no answer"
        super().__init__(f"Klaviyo {endpoint}: {answer}")
        self.status = status
        self.endpoint = endpoint
        self.detail = detail


class KlaviyoResponseError(RuntimeError):
    """Klaviyo answered ``200`` with a body that is not of the expected shape.

    Never read as an empty listing: it aborts the run before anything is deleted.
    """


# ---------------------------------------------------------------------------
# Resource kinds
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Kind:
    name: str  # selection name, state key and node set
    prefix: str  # document id prefix
    path: str  # collection path; a record is ``{path}/{id}``
    updated: str  # the update-time attribute, also the filter and sort field
    page_size: int  # Klaviyo's maximum for this collection
    # Sparse fieldsets for the full listing, so the deletion sweep stays light.
    list_params: tuple[tuple[str, str], ...]
    # ...and for the listing of changed records, when it differs.
    changed_params: tuple[tuple[str, str], ...] = ()

    @property
    def item_endpoint(self) -> str:
        return f"{self.path}/{{id}}"


_CAMPAIGN_LIST_PARAMS = (
    ("fields[campaign]", "definition.archived,updated_at"),
    # The messages' statuses tell which campaigns can still be edited.
    ("include", "campaign-messages"),
    ("fields[campaign-message]", "definition.status"),
)
# A template's copy comes with the listing, so a changed template needs no
# second request.
_TEMPLATE_CONTENT = (("fields[template]", "name,editor_type,html,text,updated"),)

_KINDS = {
    "campaigns": _Kind(
        "campaigns", "campaign", "/campaigns", "updated_at", 100, _CAMPAIGN_LIST_PARAMS
    ),
    "flows": _Kind(
        "flows", "flow", "/flows", "updated", 50, (("fields[flow]", "archived,updated"),)
    ),
    "templates": _Kind(
        "templates",
        "template",
        "/templates",
        "updated",
        10,
        (("fields[template]", "updated"),),
        _TEMPLATE_CONTENT,
    ),
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _check_api_key(value: Any) -> str:
    """The key, or ``ValueError`` without ever echoing it.

    A pasted key often ends in a newline; requests then raises an error whose
    message holds the whole header, key included.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("Klaviyo private API key required: pass api_key= or set KLAVIYO_API_KEY.")
    if any(char.isspace() or not char.isprintable() for char in value):
        raise ValueError("The Klaviyo API key contains whitespace or control characters")
    if not value.startswith("pk_"):
        # The six-character public key (site id) cannot read anything.
        raise ValueError("Use a Klaviyo private API key (it starts with pk_)")
    return value


class _Client:
    """One run's access to the API: the session, the key, the waits."""

    def __init__(self, session: Any, api_key: str, check_active: Callable[[], None] | None = None):
        self.session = session
        self.headers = {
            "Authorization": f"Klaviyo-API-Key {api_key}",
            "revision": API_REVISION,
            "accept": "application/vnd.api+json",
        }
        self.check_active = check_active
        self.waited = 0.0  # seconds slept this run
        self.requests = 0

    def check(self) -> None:
        if self.check_active is not None:
            self.check_active()

    def can_wait(self, seconds: float) -> bool:
        """Whether a wait fits in the single-wait cap and the run's budget."""
        return seconds <= _MAX_WAIT_SECONDS and self.waited + seconds <= _MAX_TOTAL_WAIT_SECONDS

    def wait(self, seconds: float) -> None:
        self.check()
        self.waited += seconds
        time.sleep(seconds)

    def get(
        self,
        path: str,
        params: Any = None,
        *,
        endpoint: str,
        ok: tuple[int, ...] = (200,),
    ) -> tuple[int, Any]:
        """GET an API path (or a ``links.next`` URL) and return ``(status, json)``.

        ``endpoint`` is the label errors and logs use (``/campaigns/{id}``), so
        a record id never ends up in a message. Rate limits (429), server errors
        (5xx) and network failures are retried up to ``_MAX_RETRIES`` times; a
        wait over ``_MAX_WAIT_SECONDS``, or past the run's
        ``_MAX_TOTAL_WAIT_SECONDS``, is not taken. Every other status outside
        ``ok`` raises :class:`KlaviyoAPIError`.
        """
        url = path if path.startswith("https://") else f"{API_BASE}{path}"
        attempt = 0
        while True:
            last_attempt = attempt == _MAX_RETRIES - 1
            self.check()
            self.requests += 1
            try:
                response = self.session.get(
                    url, params=params, headers=self.headers, timeout=_TIMEOUT_SECONDS
                )
            except requests.RequestException as exc:
                # Only the type: some requests errors carry the request, headers included.
                delay = _backoff(attempt)
                if last_attempt or not self.can_wait(delay):
                    raise KlaviyoAPIError(0, endpoint, type(exc).__name__) from None
                logger.warning(
                    "Klaviyo: %s on %s; retrying in %.0fs.", type(exc).__name__, endpoint, delay
                )
                self.wait(delay)
                attempt += 1
                continue

            status = response.status_code
            if status in ok:
                return status, _json_body(response, endpoint) if status == 200 else None
            if status in _TRANSIENT_STATUSES and not last_attempt:
                delay = _retry_delay(response.headers, attempt)
                if not self.can_wait(delay):
                    raise KlaviyoAPIError(status, endpoint, f"asked to wait {delay:.0f}s")
                logger.warning(
                    "Klaviyo: HTTP %d on %s; retrying in %.0fs.", status, endpoint, delay
                )
                self.wait(delay)
                attempt += 1
                continue
            raise KlaviyoAPIError(status, endpoint, _error_code(response))


def _json_body(response: Any, endpoint: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise KlaviyoResponseError(
            f"Klaviyo {endpoint}: HTTP {response.status_code} without JSON"
        ) from None


def _backoff(attempt: int) -> float:
    """1, 2, 4, 8 s plus up to a second of jitter, as Klaviyo asks of clients."""
    return 2**attempt + random.random()


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait before a retry: ``Retry-After`` when Klaviyo sends one (on 429)."""
    try:
        return max(0.0, float(_header(headers, "Retry-After")))
    except (TypeError, ValueError):
        return _backoff(attempt)


def _error_code(response: Any) -> str:
    """Klaviyo's error ``code`` (``not_found``, ``permission_denied``), or nothing.

    Never the title, the detail or the raw body: they can echo request data back.
    """
    try:
        errors = response.json().get("errors")
        code = errors[0].get("code") if isinstance(errors, list) and errors else None
    except (ValueError, AttributeError, TypeError):
        return ""
    return code[:100] if isinstance(code, str) else ""


def _header(headers: Any, name: str) -> Any:
    """A response header, case-insensitively, from a mapping or ``requests`` headers."""
    if not headers:
        return None
    if name in headers:
        return headers[name]
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


# ---------------------------------------------------------------------------
# JSON:API
# ---------------------------------------------------------------------------
def _check_resource(record: Any, endpoint: str) -> dict:
    if not isinstance(record, dict) or record.get("id") in (None, ""):
        raise KlaviyoResponseError(f"Klaviyo {endpoint}: an entry without an id")
    return record


def _document(body: Any, endpoint: str) -> tuple[dict, list[dict]]:
    """``(data, included)`` of a single-resource document."""
    if not isinstance(body, dict):
        raise KlaviyoResponseError(f"Klaviyo {endpoint}: a {type(body).__name__}, not an object")
    data = _check_resource(body.get("data"), endpoint)
    return data, _included(body, endpoint)


def _included(body: dict, endpoint: str) -> list[dict]:
    included = body.get("included")
    if included is None:
        return []
    if not isinstance(included, list):
        raise KlaviyoResponseError(f"Klaviyo {endpoint}: 'included' is not a list")
    return [r for r in included if isinstance(r, dict) and r.get("id") not in (None, "")]


def _list(
    client: _Client, path: str, params: list[tuple[str, str]], *, endpoint: str
) -> tuple[list[dict], list[dict]]:
    """Every record of a collection and everything it included. Never a partial list.

    Paging follows ``links.next``, which Klaviyo builds with a cursor, so a
    record added or removed while the listing is paged does not shift the
    others. A next link to another host, a cursor that repeats, a body without a
    ``data`` list, or an entry without an id raises, so a broken page can never
    pass for a short (and deletion-triggering) listing.
    """
    records: dict[str, dict] = {}
    included: dict[tuple[str, str], dict] = {}
    seen: set[str] = set()
    url: str = path
    page_params: Any = params
    for _ in range(_MAX_LIST_PAGES):
        _, body = client.get(url, page_params, endpoint=endpoint)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise KlaviyoResponseError(f"Klaviyo {endpoint}: a page without a 'data' list")
        for record in body["data"]:
            record = _check_resource(record, endpoint)
            # A record edited while the listing is paged by update time moves to
            # a later page and is seen twice; the later copy is the newer one.
            records.pop(str(record["id"]), None)
            records[str(record["id"])] = record
        for record in _included(body, endpoint):
            included[(str(record.get("type")), str(record["id"]))] = record
        links = body.get("links") if isinstance(body.get("links"), dict) else {}
        next_url = links.get("next")
        if not next_url:
            return list(records.values()), list(included.values())
        _check_next_link(next_url, endpoint)
        if next_url in seen:
            raise KlaviyoResponseError(f"Klaviyo {endpoint}: the next page link repeated")
        seen.add(next_url)
        url, page_params = next_url, None  # the link already carries every parameter
    raise KlaviyoResponseError(f"Klaviyo {endpoint}: more than {_MAX_LIST_PAGES} pages")


def _check_next_link(next_url: Any, endpoint: str) -> None:
    """A ``links.next`` URL must stay on Klaviyo's API: the key is sent with it."""
    parts = urlsplit(next_url) if isinstance(next_url, str) else None
    if not parts or parts.scheme != "https" or parts.netloc != _API_HOST:
        raise KlaviyoResponseError(f"Klaviyo {endpoint}: a next page link off the API host")
    if not parts.path.startswith("/api/"):
        raise KlaviyoResponseError(f"Klaviyo {endpoint}: a next page link off the API")


def _attributes(record: dict) -> dict:
    attributes = record.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _definition(record: dict) -> dict:
    """``attributes.definition`` (where 2026-10-15 keeps a campaign's own fields)."""
    definition = _attributes(record).get("definition")
    return definition if isinstance(definition, dict) else {}


def _related_ids(record: dict, relationship: str) -> list[str]:
    relationships = record.get("relationships")
    related = relationships.get(relationship) if isinstance(relationships, dict) else None
    data = related.get("data") if isinstance(related, dict) else None
    if not isinstance(data, list):
        return []
    return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]


def _of_type(included: list[dict], kind: str) -> dict[str, dict]:
    return {str(r["id"]): r for r in included if r.get("type") == kind}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
}  # fmt: skip
_SKIPPED_TAGS = ("script", "style", "head", "title")


class _TextExtractor(HTMLParser):
    """Plain text from email HTML (layout tables, inline styles and all)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIPPED_TAGS:
            self.skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


# Klaviyo's template language is Django-style. Block tags ({% unsubscribe %},
# {% if ... %}) are markup for the sender, not content; a variable keeps its
# name but loses its filters ({{ first_name|default:'there' }} -> {{ first_name }}).
_BLOCK_TAG = re.compile(r"\{%.*?%\}", re.S)
_VARIABLE = re.compile(r"\{\{\s*([^|}]+?)\s*(?:\|[^}]*)?\}\}")


def clean_template_tags(text: str) -> str:
    """Email copy without Klaviyo's template tags, keeping variable names."""
    text = _BLOCK_TAG.sub("", text)
    return _VARIABLE.sub(lambda m: "{{ " + m.group(1) + " }}", text)


def html_to_text(value: Any) -> str:
    """Readable text from email HTML (tags dropped, entities decoded, template tags cleaned)."""
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return _tidy(clean_template_tags("".join(parser.parts)))


def _tidy(text: str) -> str:
    lines = (re.sub(r"[ \t\r\f\v\u00a0\u200c]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line and line != "-").strip()


def template_text(template: dict | None) -> str:
    """A template's body: its HTML as text, else its plain-text version.

    The HTML is what recipients see. The plain-text version is kept apart and
    is not regenerated when the HTML changes (seen on a live account), so it
    is only a fallback.
    """
    if not template:
        return ""
    attributes = _attributes(template)
    body = html_to_text(attributes.get("html"))
    if body:
        return body
    text = attributes.get("text")
    return _tidy(clean_template_tags(text)) if isinstance(text, str) else ""


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _lines(pairs: Iterable[tuple[str, Any]]) -> list[str]:
    return [f"{label}: {_text(value)}" for label, value in pairs if _text(value)]


def _body_lines(label: str, body: str) -> list[str]:
    return [f"{label}:", body] if body else []


def render_campaign(
    campaign: dict,
    messages: list[dict],
    variations: dict[str, list[dict]],
    templates: dict[str, dict | None],
) -> dict[str, Any]:
    """A campaign as a document: what it sends, to whom by channel, and the copy.

    ``variations`` maps a message id to its variations (subject, preview text,
    template), ``templates`` a template id to the template. Sender addresses,
    audiences, tracking settings and modification times are left out on
    purpose: the first are contact data, the last change without the content
    changing.
    """
    definition = _definition(campaign)
    name = _text(definition.get("name")) or f"Campaign {campaign['id']}"
    lines = [f"Campaign: {name}"]
    lines += _lines(
        (
            ("Description", definition.get("description")),
            ("Archived", "yes" if definition.get("archived") is True else ""),
        )
    )
    for message in messages:
        message_def = _definition(message)
        lines += ["", f"Message: {_text(message_def.get('name')) or message['id']}"]
        send_options = message_def.get("send_options")
        send_time = send_options.get("send_time") if isinstance(send_options, dict) else None
        lines += _lines((("Status", message_def.get("status")), ("Send time", send_time)))
        message_variations = variations.get(str(message["id"])) or []
        for variation in message_variations:
            variation_def = _definition(variation)
            details = variation_def.get("details")
            details = details if isinstance(details, dict) else {}
            if len(message_variations) > 1:
                lines.append(f"Variation: {_text(variation_def.get('name')) or variation['id']}")
            lines += _channel_lines(details, templates)
    return _row("campaign", campaign["id"], f"Campaign: {name}", lines)


def _channel_lines(details: dict, templates: dict[str, dict | None]) -> list[str]:
    channel = _text(details.get("channel"))
    lines = _lines(
        (
            ("Channel", channel),
            ("Subject", details.get("subject")),
            ("Preview text", details.get("preview_text")),
            ("From", details.get("from_label")),
            ("Title", details.get("title")),
        )
    )
    template = templates.get(_text(details.get("template_id")))
    if channel == "email" or template:
        lines += _body_lines("Body", template_text(template))
    else:  # SMS and push carry their text inline
        lines += _body_lines("Body", _tidy(clean_template_tags(_text(details.get("body")))))
    return lines


def render_flow(
    flow: dict, actions: list[dict], templates: dict[str, dict | None]
) -> dict[str, Any]:
    """A flow as a document: its trigger and every step, in order, with the email copy."""
    attributes = _attributes(flow)
    name = _text(attributes.get("name")) or f"Flow {flow['id']}"
    lines = [f"Flow: {name}"]
    lines += _lines(
        (
            ("Status", attributes.get("status")),
            ("Trigger", attributes.get("trigger_type")),
            ("Archived", "yes" if attributes.get("archived") is True else ""),
        )
    )
    steps = [step for step in (_step_lines(action, templates) for action in actions) if step]
    if steps:
        lines += ["", "Steps:"]
        for number, step in enumerate(steps, start=1):
            lines.append(f"{number}. {step[0]}")
            # Every line of a multi-line body is indented under its step.
            lines += [f"   {line}" for item in step[1:] for line in item.split("\n")]
    return _row("flow", flow["id"], f"Flow: {name}", lines)


_STEP_NAMES = {
    "send-email": "Email",
    "send-sms": "SMS",
    "send-mobile-push": "Push notification",
    "send-whatsapp": "WhatsApp message",
    "send-whatsapp-with-ac": "WhatsApp message",
    "send-internal-alert": "Internal alert",
    "send-webhook": "Webhook",
    "conditional-split": "Conditional split",
    "trigger-split": "Trigger split",
    "multi-branch-split": "Multi-branch split",
    "action-output-split": "Split on a previous step",
    "ab-test": "A/B test",
    "content-experiment": "Content experiment",
    "update-profile": "Update profile",
    "list-update": "Update list",
    "back-in-stock-delay": "Wait until back in stock",
    "target-date": "Wait until a date",
    "code": "Custom code",
    "internal-service": "Internal step",
}


def _data(action: dict) -> dict:
    data = action.get("data")
    return data if isinstance(data, dict) else {}


def _message(action: dict) -> tuple[str, dict]:
    """A step's ``(type, message)``. An A/B test or content experiment sends the
    message of its ``main_action``."""
    kind = _text(action.get("type"))
    data = _data(action)
    main = data.get("main_action")
    if kind in ("ab-test", "content-experiment") and isinstance(main, dict):
        return _message(main)
    message = data.get("message")
    return kind, message if isinstance(message, dict) else {}


def _step_lines(action: dict, templates: dict[str, dict | None]) -> list[str]:
    """One flow step as lines: a heading, then its copy. Empty for an unknown shape."""
    kind = _text(action.get("type"))
    data = _data(action)
    if kind in ("time-delay", "countdown-delay"):
        value, unit = _text(data.get("value")), _text(data.get("unit"))
        return [f"Wait {value} {unit}".strip() if value else "Wait"]
    heading = _STEP_NAMES.get(kind)
    if heading is None:
        return [kind.replace("-", " ").capitalize()] if kind else []
    sent_kind, message = _message(action)
    if not message:
        return [heading]
    if sent_kind != kind and sent_kind in _STEP_NAMES:
        heading = f"{heading} of {_STEP_NAMES[sent_kind].lower()}"
    kind = sent_kind
    message_name = _text(message.get("name"))
    lines = [f"{heading}: {message_name}" if message_name else heading]
    lines += _lines(
        (
            ("Subject", message.get("subject_line")),
            ("Preview text", message.get("preview_text")),
            ("From", message.get("from_label")),
            ("Title", message.get("title")),
        )
    )
    if kind == "send-webhook":
        return lines  # a webhook's body is a payload for a machine
    template = templates.get(_text(message.get("template_id")))
    body = template_text(template) or _tidy(clean_template_tags(_text(message.get("body"))))
    return lines + _body_lines("Body", body)


def render_template(template: dict) -> dict[str, Any]:
    """An email template as a document: its name and its copy as plain text."""
    attributes = _attributes(template)
    name = _text(attributes.get("name")) or f"Template {template['id']}"
    lines = [f"Email template: {name}"]
    lines += _lines((("Editor", attributes.get("editor_type")),))
    body = template_text(template)
    if body:
        lines += ["", body]
    return _row("template", template["id"], f"Email template: {name}", lines)


_KIND_BY_PREFIX = {kind.prefix: kind.name for kind in _KINDS.values()}


def _row(prefix: str, record_id: Any, title: str, lines: Iterable[str]) -> dict[str, Any]:
    """A document row. Only stable fields: every column feeds cognee's content hash."""
    return {
        "id": f"{prefix}:{record_id}",
        "title": title,
        "content": "\n".join(lines).strip(),
        "_deleted": False,
        dlt_utils.NODE_SET_COLUMN: [_KIND_BY_PREFIX[prefix]],
    }


def _deleted_row(row_id: str) -> dict[str, Any]:
    return {"id": row_id, "_deleted": True}


def _fingerprint(row: dict[str, Any]) -> str:
    text = "\x1f".join(str(row.get(field) or "") for field in ("title", "content"))
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Flow definitions
# ---------------------------------------------------------------------------
def _ordered_actions(definition: dict) -> list[dict]:
    """A flow's actions in the order a profile meets them.

    Walks from ``entry_action_id`` along each action's links (``next``, then
    ``next_if_true`` / ``next_if_false`` for splits), depth first; actions the
    walk does not reach are appended in their listed order, so none is lost.
    """
    actions = [a for a in definition.get("actions") or [] if isinstance(a, dict)]
    by_id = {str(a.get("id")): a for a in actions if a.get("id") not in (None, "")}
    ordered: list[dict] = []
    seen: set[str] = set()
    pending = [str(definition.get("entry_action_id") or "")]
    while pending:
        action_id = pending.pop()
        action = by_id.get(action_id)
        if action is None or action_id in seen:
            continue
        seen.add(action_id)
        ordered.append(action)
        links = action.get("links") if isinstance(action.get("links"), dict) else {}
        # Pushed in reverse, so "next" and the true branch are walked first.
        for key in ("next_if_false", "next_if_true", "next"):
            if links.get(key) not in (None, ""):
                pending.append(str(links[key]))
    return ordered + [a for a in actions if str(a.get("id")) not in seen]


def _flow_template_ids(actions: list[dict]) -> list[str]:
    ids = []
    for action in actions:
        kind, message = _message(action)
        if _text(message.get("template_id")) and kind != "send-webhook":
            ids.append(_text(message["template_id"]))
    return ids


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
@dataclass
class _Options:
    include_archived: bool = False


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _updated(kind: _Kind, record: dict) -> datetime | None:
    return _parse_time(_attributes(record).get(kind.updated))


def _archived(kind: _Kind, record: dict) -> bool:
    if kind.name == "campaigns":
        return _definition(record).get("archived") is True
    return _attributes(record).get("archived") is True


def _in_scope(kind: _Kind, record: dict, options: _Options) -> bool:
    """Whether a listed or fetched record belongs to the selection."""
    return options.include_archived or not _archived(kind, record)


# What rendering a record gives: the row, the template ids it used, and the
# record as it was rendered (its update time is the record's new stamp).
_Rendered = tuple[dict[str, Any], list[str], dict]


class _Run:
    """One sync run over the selected kinds. State is written by :meth:`commit` only."""

    def __init__(
        self,
        client: _Client,
        state: dict,
        kinds: list[_Kind],
        options: _Options,
        stats: dict,
        now: datetime | None = None,
    ):
        self.client = client
        self.state = state
        self.kinds = kinds
        self.options = options
        self.stats = stats
        self.now = now or datetime.now(timezone.utc)
        self.plans: dict[str, dict[str, Any]] = {}
        self.templates: dict[str, dict | None] = {}  # fetched this run, by id
        self.template_cursor: str | None = state.get("template_cursor")
        # Each template's update time when its owners were last told about it.
        self.template_stamps: dict[str, str] = dict(state.get("template_stamps") or {})

    # -- 1. list -------------------------------------------------------------
    def list_all(self) -> None:
        """List every selected kind and confirm deletions, before a single row.

        A listed record is due when its update time differs from the one it
        had when it was last rendered. The cursor's overlap lists the newest
        records again on every run; their unchanged stamps keep them from being
        fetched again.
        """
        changed_templates = self._watch_templates()
        touched = self._note_templates(changed_templates or [])
        for kind in self.kinds:
            saved = self.state.get(kind.name) or {}
            known: dict[str, str] = dict(saved.get("known") or {})
            stamps: dict[str, str] = dict(saved.get("stamps") or {})
            uses: dict[str, list[str]] = {k: list(v) for k, v in (saved.get("uses") or {}).items()}
            retry = [str(i) for i in saved.get("retry") or []]
            cursor = saved.get("cursor")
            if kind.name == "templates" and cursor and changed_templates is not None:
                # The watch listing already holds the changed templates, copy included.
                changed, cursor_after, included = changed_templates, self.template_cursor, []
            else:
                changed, cursor_after, included = self._list_changed(kind, cursor)
                if kind.name == "templates":
                    self._note_templates(changed)
            listed = changed
            if cursor:  # the changed listing is partial; the deletion sweep needs all ids
                listed, included = self._list_kind(kind, kind.list_params)
            current = {str(r["id"]): r for r in listed if _in_scope(kind, r, self.options)}
            self.stats["listed"] += len(current)
            # Renders: what changed, campaigns that can still be edited, owners of
            # a template that changed, and what failed last run.
            due = [
                str(r["id"])
                for r in changed
                if stamps.get(str(r["id"])) != _attributes(r).get(kind.updated)
            ]
            due += self._editable_campaigns(kind, current, included)
            moved = touched
            if kind.name == "flows" and cursor:
                moved = touched | self._moved_flow_templates(uses)
            due += [owner for owner, used in uses.items() if moved & set(used)]
            due += retry
            # In scope but never rendered: e.g. archived records once
            # include_archived is turned on, whose update times are long past.
            due += [record_id for record_id in current if record_id not in known]
            if self.state.get("render_version", _RENDER_VERSION) != _RENDER_VERSION:
                due += list(current)  # documents look different now; fingerprints skip the rest
            missing = sorted(set(known) - set(current))
            gone = self._confirm_gone(kind, missing)
            # A record the listing left out but that is still there is read in
            # full: whatever it was due for (a template edit) must not be lost.
            present = set(missing) - set(gone)
            due += sorted(present)
            self.plans[kind.name] = {
                "known": known,
                "stamps": stamps,
                "uses": uses,
                "due": [
                    i
                    for i in dict.fromkeys(due)
                    if (i in current or i in present) and i not in gone
                ],
                "listed": {str(r["id"]): r for r in changed},
                "gone": gone,
                "cursor": cursor_after or cursor,
            }
            if kind.name == "templates":
                self.template_cursor = _later(self.template_cursor, cursor_after)

    def _note_templates(self, records: list[dict]) -> set[str]:
        """Record the templates' update times; return the ids whose time moved."""
        touched = set()
        for record in records:
            template_id, updated = str(record["id"]), _attributes(record).get("updated")
            if not isinstance(updated, str):
                continue
            if self.template_stamps.get(template_id) != updated:
                touched.add(template_id)
                self.template_stamps[template_id] = updated
        return touched

    def _list_kind(
        self, kind: _Kind, fields: Iterable[tuple[str, str]], extra: Iterable[tuple[str, str]] = ()
    ) -> tuple[list, list]:
        params = [*fields, ("page[size]", str(kind.page_size)), *extra]
        return _list(self.client, kind.path, params, endpoint=kind.path)

    def _list_changed(self, kind: _Kind, cursor: str | None) -> tuple[list[dict], str | None, list]:
        """Records updated since the cursor (everything on a first run), and the new cursor."""
        since = _parse_time(cursor)
        records, included = self._list_kind(
            kind, kind.changed_params or kind.list_params, _since_params(kind, since)
        )
        return records, _newest(kind, records, since), included

    def _watch_templates(self) -> list[dict] | None:
        """Templates updated since the last run; ``None`` when there is no last run yet.

        An edit to an email's template does not have to move the campaign's or
        flow's own update time, so the campaigns and flows that use a changed
        template are rendered again. With the templates kind selected, the
        listing carries the copy too and doubles as that kind's changed listing.
        """
        kind = _KINDS["templates"]
        selected = any(k.name == "templates" for k in self.kinds)
        since = _parse_time(self.template_cursor)
        if since is None:
            if not selected:
                # Nothing to compare against yet, only a starting point: the
                # newest template's update time (the templates kind, when
                # selected, sets it from its own first listing). One page of
                # one record, without following the next link.
                _, body = self.client.get(
                    kind.path,
                    [*kind.list_params, ("sort", f"-{kind.updated}"), ("page[size]", "1")],
                    endpoint=kind.path,
                )
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, list):
                    raise KlaviyoResponseError(f"Klaviyo {kind.path}: a page without a 'data' list")
                newest = [_check_resource(record, kind.path) for record in data[:1]]
                self.template_cursor = _newest(kind, newest, None) or _stamp(self.now)
                self._note_templates(newest)
            return None
        fields = kind.changed_params if selected else kind.list_params
        records, _ = self._list_kind(kind, fields, _since_params(kind, since))
        self.template_cursor = _newest(kind, records, since)
        return records

    def _editable_campaigns(self, kind: _Kind, current: dict, included: list[dict]) -> list[str]:
        """Campaigns with a draft or scheduled message, whose copy can still change."""
        if kind.name != "campaigns":
            return []
        messages = _of_type(included, "campaign-message")
        editable = []
        for campaign_id, campaign in current.items():
            for message_id in _related_ids(campaign, "campaign-messages"):
                status = _definition(messages.get(message_id) or {}).get("status")
                if status in _EDITABLE_STATUSES:
                    editable.append(campaign_id)
                    break
        return editable

    def _confirm_gone(self, kind: _Kind, missing: list[str]) -> list[str]:
        """Of the ids that left the listing, the ones Klaviyo confirms are gone.

        A record counts as gone when it answers 404, or when it answers archived
        while archived records are out of scope. A record that still answers in
        scope stays. Any other failure aborts the run, so a flaky listing can
        never cause a deletion.
        """
        gone = []
        for record_id in missing:
            status, body = self.client.get(
                f"{kind.path}/{record_id}", endpoint=kind.item_endpoint, ok=(200, 404)
            )
            if status == 404:
                gone.append(record_id)
                continue
            record, _ = _document(body, kind.item_endpoint)
            if not _in_scope(kind, record, self.options):
                gone.append(record_id)
            else:
                logger.warning(
                    "Klaviyo: a %s missing from the listing is still in scope; kept.", kind.prefix
                )
        return gone

    # -- 2. render -----------------------------------------------------------
    def render(self) -> Iterator[dict[str, Any]]:
        for kind in self.kinds:
            plan = self.plans[kind.name]
            kept: list[str] = []
            streak = 0
            for record_id in plan["due"]:
                try:
                    rendered = self._render(kind, record_id, plan["listed"].get(record_id))
                    if rendered is None:  # deleted since the listing; next run confirms it
                        continue
                    row, used, record = rendered
                except KlaviyoAPIError as exc:
                    if exc.status in (401, 403):
                        raise  # access is broken for everything, not this record
                    streak += 1
                    if streak >= _OUTAGE_STREAK:
                        raise
                    logger.warning(
                        "Klaviyo: a %s failed to load (%s); retried next run.", kind.prefix, exc
                    )
                    kept.append(record_id)
                    self.stats["failed"] += 1
                    continue
                streak = 0
                if kind.name != "templates":
                    plan["uses"][record_id] = used
                # The update time of the copy that was rendered, not of a listing
                # read earlier or later: an edit in between must stay "changed".
                updated = _attributes(record).get(kind.updated)
                if isinstance(updated, str):
                    plan["stamps"][record_id] = updated
                fingerprint = _fingerprint(row)
                if plan["known"].get(record_id) == fingerprint:
                    self.stats["unchanged"] += 1
                    continue
                plan["known"][record_id] = fingerprint
                self.stats["rendered"] += 1
                yield row
            plan["kept"] = kept
            self.stats["kept"] += len(kept)

    def _render(self, kind: _Kind, record_id: str, listed: dict | None) -> _Rendered | None:
        """``(row, template ids used, the record as rendered)``, or ``None`` once it is gone."""
        if kind.name == "templates":
            template = listed if listed and "html" in _attributes(listed) else None
            template = template or self._template(record_id)
            return (render_template(template), [], template) if template else None
        if kind.name == "campaigns":
            return self._render_campaign(record_id)
        return self._render_flow(record_id)

    def _fetch(self, kind: _Kind, record_id: str, params: Any = None) -> tuple[dict, list] | None:
        status, body = self.client.get(
            f"{kind.path}/{record_id}", params, endpoint=kind.item_endpoint, ok=(200, 404)
        )
        if status == 404:
            return None
        record, included = _document(body, kind.item_endpoint)
        return (record, included) if _in_scope(kind, record, self.options) else None

    def _render_campaign(self, campaign_id: str) -> _Rendered | None:
        fetched = self._fetch(_KINDS["campaigns"], campaign_id, [("include", "campaign-messages")])
        if fetched is None:
            return None
        campaign, included = fetched
        by_id = _of_type(included, "campaign-message")
        message_ids = _related_ids(campaign, "campaign-messages") or sorted(by_id)
        messages, variations, used = [], {}, []
        for message_id in message_ids:
            status, body = self.client.get(
                f"/campaign-messages/{message_id}",
                [("include", "campaign-variations")],
                endpoint="/campaign-messages/{id}",
                ok=(200, 404),
            )
            if status == 404:  # removed from the campaign since it was read
                continue
            message, message_included = _document(body, "/campaign-messages/{id}")
            messages.append(message)
            found = _of_type(message_included, "campaign-variation")
            ordered = [found[i] for i in _related_ids(message, "campaign-variations") if i in found]
            variations[message_id] = ordered or [found[i] for i in sorted(found)]
            for variation in variations[message_id]:
                details = _definition(variation).get("details")
                if isinstance(details, dict) and _text(details.get("template_id")):
                    used.append(_text(details["template_id"]))
        templates = {template_id: self._template(template_id) for template_id in used}
        row = render_campaign(campaign, messages, variations, templates)
        return row, sorted(set(used)), campaign

    def _render_flow(self, flow_id: str) -> _Rendered | None:
        fetched = self._fetch(_KINDS["flows"], flow_id, [("additional-fields[flow]", "definition")])
        if fetched is None:
            return None
        flow, _ = fetched
        definition = _attributes(flow).get("definition")
        if isinstance(definition, dict):
            actions = _ordered_actions(definition)
        else:
            actions = self._flow_actions(flow_id)
        used = _flow_template_ids(actions)
        templates = {template_id: self._template(template_id) for template_id in used}
        return render_flow(flow, actions, templates), sorted(set(used)), flow

    def _flow_actions(self, flow_id: str) -> list[dict]:
        """A flow's actions from ``/flows/{id}/flow-actions``, when the flow has no definition."""
        records, _ = _list(
            self.client,
            f"/flows/{flow_id}/flow-actions",
            [("page[size]", "50")],
            endpoint="/flows/{id}/flow-actions",
        )
        actions = [_definition(record) for record in records]
        return [action for action in actions if action]

    def _template(self, template_id: str) -> dict | None:
        """A template by id, once per run; ``None`` once it is deleted.

        A template seen for the first time gets its update time noted, so a
        later edit is told apart from this copy. One already noted keeps its
        time: only the template listing and :meth:`_moved_flow_templates` move
        it, after they marked the template's owners for rendering. Moving it
        here, for an edit made since those ran, would hide that edit from the
        owners not rendered this run.
        """
        if template_id not in self.templates:
            status, body = self.client.get(
                f"/templates/{template_id}", endpoint="/templates/{id}", ok=(200, 404)
            )
            template = None if status == 404 else _document(body, "/templates/{id}")[0]
            if template_id not in self.template_stamps:
                updated = _attributes(template).get("updated") if template else ""
                if isinstance(updated, str):
                    self.template_stamps[template_id] = updated
            self.templates[template_id] = template
        return self.templates[template_id]

    def _moved_flow_templates(self, uses: dict[str, list[str]]) -> set[str]:
        """The templates used by flow emails whose update time moved since the last render.

        A flow email has its own copy of a template. Klaviyo's template listing
        leaves these copies out, and editing one does not move the flow's own
        update time, so each is asked for its update time on every run: one
        light request per flow email (templates allow 750 requests a minute).
        """
        moved = set()
        for template_id in sorted({t for used in uses.values() for t in used}):
            status, body = self.client.get(
                f"/templates/{template_id}",
                [("fields[template]", "updated")],
                endpoint="/templates/{id}",
                ok=(200, 404),
            )
            if status == 404:
                # Gone: render the flow without it, once ("" marks it as seen gone).
                if self.template_stamps.get(template_id) != "":
                    moved.add(template_id)
                    self.template_stamps[template_id] = ""
                continue
            moved |= self._note_templates([_document(body, "/templates/{id}")[0]])
        return moved

    # -- 3. delete, 4. commit -----------------------------------------------
    def deletions(self) -> Iterator[dict[str, Any]]:
        for kind in self.kinds:
            plan = self.plans[kind.name]
            for record_id in plan["gone"]:
                plan["known"].pop(record_id, None)
                plan["uses"].pop(record_id, None)
                plan["stamps"].pop(record_id, None)
                self.stats["deleted"] += 1
                yield _deleted_row(f"{kind.prefix}:{record_id}")
        # A kind that was synced before but is no longer selected left the
        # selection: its documents are forgotten like deleted ones.
        selected = {kind.name for kind in self.kinds}
        for name in sorted(set(self.state) - selected):
            if name not in _KINDS:
                continue
            for record_id in sorted((self.state.get(name) or {}).get("known") or {}):
                self.stats["deleted"] += 1
                yield _deleted_row(f"{_KINDS[name].prefix}:{record_id}")

    def commit(self) -> None:
        for name in [n for n in self.state if n in _KINDS and n not in self.plans]:
            del self.state[name]
        for name, plan in self.plans.items():
            entry: dict[str, Any] = {
                "cursor": plan["cursor"],
                "known": plan["known"],
                "retry": plan.get("kept", []),
            }
            entry["stamps"] = {k: v for k, v in plan["stamps"].items() if k in plan["known"]}
            if name != "templates":
                entry["uses"] = {k: v for k, v in plan["uses"].items() if k in plan["known"]}
            self.state[name] = entry
        self.state["template_cursor"] = self.template_cursor
        self.state["template_stamps"] = self.template_stamps
        self.state["render_version"] = _RENDER_VERSION


def _newest(kind: _Kind, records: list[dict], since: datetime | None) -> str | None:
    stamps = [s for s in (_updated(kind, r) for r in records) if s]
    newest = max(stamps, default=None)
    if since is not None:
        newest = max(newest, since) if newest else since
    return _stamp(newest) if newest else None


def _later(first: str | None, second: str | None) -> str | None:
    stamps = [s for s in (_parse_time(first), _parse_time(second)) if s]
    return _stamp(max(stamps)) if stamps else None


def _since_params(kind: _Kind, since: datetime | None) -> list[tuple[str, str]]:
    """Sort by update time and, after a first run, keep what changed since ``since``.

    The window opens ``_CURSOR_OVERLAP`` early; the fingerprints keep the
    overlap from re-ingesting anything.
    """
    params = [("sort", kind.updated)]
    if since is not None:
        start = _stamp(since - _CURSOR_OVERLAP)
        params.append(("filter", f"greater-or-equal({kind.updated},{start})"))
    return params


def sync_account(
    client: _Client,
    state: dict,
    *,
    resources: Iterable[str] = DEFAULT_RESOURCES,
    include_archived: bool = False,
    stats: dict[str, int] | None = None,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, then hard-delete markers.

    1. *List.* Every selected kind is listed, and every id that left a listing
       is confirmed gone with a GET. A failure here propagates before a single row.
    2. *Render.* Each new, changed or still-editable record becomes a document;
       one whose text did not change is skipped. A record that fails to load is
       kept for the next run (three in a row stop the run as an outage).
    3. *Delete.* A hard-delete marker for every confirmed deletion.
    4. *Commit.* Only now is ``state`` written.
    """
    kinds = _selected_kinds(resources)
    stats = {} if stats is None else stats
    for key in _STAT_KEYS:
        stats.setdefault(key, 0)
    run = _Run(client, state, kinds, _Options(include_archived=include_archived), stats, now)
    run.list_all()
    yield from run.render()
    yield from run.deletions()
    run.commit()
    logger.info(
        "Klaviyo: %d document(s) rendered, %d unchanged, %d deletion(s), %d kept for retry.",
        stats["rendered"],
        stats["unchanged"],
        stats["deleted"],
        stats["kept"],
    )


def _selected_kinds(resources: Iterable[str]) -> list[_Kind]:
    if isinstance(resources, str):
        resources = [resources]
    names = list(dict.fromkeys(resources))
    if not names:
        raise ValueError("Select at least one of: " + ", ".join(RESOURCES))
    unknown = [name for name in names if name not in _KINDS]
    if unknown:
        raise ValueError(
            f"Unknown Klaviyo resource(s) {unknown}; choose from {', '.join(RESOURCES)}"
        )
    return [_KINDS[name] for name in names]


def _account_id(client: _Client) -> str:
    """The id of the account the key belongs to (``/accounts`` answers exactly one)."""
    _, body = client.get("/accounts", endpoint="/accounts")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != 1:
        raise KlaviyoResponseError("Klaviyo /accounts: not exactly one account")
    return str(_check_resource(data[0], "/accounts")["id"])


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def klaviyo_source(
    api_key: str | None = None,
    resources: Iterable[str] = DEFAULT_RESOURCES,
    *,
    include_archived: bool = False,
    service: Any = None,
    session: Any = None,
    resource_name: str = KLAVIYO_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource that yields Klaviyo documents for ``remember``.

    Args:
        api_key: A private API key with read access to accounts, campaigns,
            flows and templates. Falls back to ``KLAVIYO_API_KEY``.
        resources: Any of ``campaigns``, ``flows``, ``templates`` (all by default).
        include_archived: Keep archived campaigns and flows. By default an
            archived one is out of scope, and archiving it forgets it.
        service: Pre-built ``requests``-like session, mainly for tests; when
            omitted one is built for each run and closed after it. ``session`` is
            accepted as an alias.
        resource_name: dlt resource (table) name, which also scopes the sync
            state. Give each account its own name when several share a dataset.
        check_active: Optional host authorization checkpoint, called before every
            request and wait and around every row; it stops the sync by raising.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"``
        and a ``_deleted`` hard-delete column. ``resource.cognee_sync_stats``
        holds the last run's counts.
    """
    if not all(
        hasattr(dlt_utils, attr)
        for attr in ("PIPELINE_SCOPE_ATTR", "guarded_rows", "NODE_SET_COLUMN")
    ):
        raise RuntimeError(
            "The Klaviyo connector needs cognee's dlt_utils.PIPELINE_SCOPE_ATTR, "
            "guarded_rows and NODE_SET_COLUMN (cognee>=1.6.3)."
        )
    _check_resource_name(resource_name)
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both")
    service = service if service is not None else session
    kinds = _selected_kinds(resources)  # fail on a bad selection now, not mid-run
    if not isinstance(include_archived, bool):
        raise ValueError("include_archived must be True or False")
    key = _check_api_key(api_key or os.environ.get("KLAVIYO_API_KEY") or "")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def klaviyo_documents():
        stats.clear()
        http = service if service is not None else requests.Session()
        try:
            client = _Client(http, key, check_active)
            yield from dlt_utils.guarded_rows(_rows(client), check_active)
        finally:
            if service is None:
                http.close()

    def _rows(client: _Client) -> Iterator[dict[str, Any]]:
        state = dlt.current.resource_state()
        # One resource_name holds one account's state; a second account under
        # the same name would see the first account's records as deleted.
        account = _account_id(client)
        if state.get("account_id") not in (None, account):
            raise ValueError(
                f"resource_name {resource_name!r} already syncs another Klaviyo account; "
                "give each account its own resource_name"
            )
        state["account_id"] = account
        yield from sync_account(
            client,
            state,
            resources=[kind.name for kind in kinds],
            include_archived=include_archived,
            stats=stats,
        )

    resource = klaviyo_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(resource, DOCUMENT_SOURCE_ATTR, KLAVIYO_SOURCE_NAME)
    # Give the sync state its own dlt pipeline, apart from other sources.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that
    scope, and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share
    a table.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > 64:
        raise ValueError("resource_name must be at most 64 characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")
