"""DLT source for Attio CRM records, notes and list entries (full-snapshot sync + forget-on-delete).

Reads the workspace's own object model at runtime (people, companies, deals and any custom
object), renders each record, note and list entry to a short text document, and yields them
as a dlt resource for cognee's ingestion pipeline.

Like the Notion connector, Attio rows are ingested as *normal documents*: the source declares
``cognee_document_source = "attio"``, so each row flows through the standard cognify
entity-extraction pipeline instead of the deterministic dlt-row path.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging with exactly
the rows currently visible to the access token each run. Deletions propagate for free: a
record, note or entry deleted in Attio no longer appears in the listings, so it is absent from
the snapshot and cognee's existing ``orphan_cleanup`` removes it from the graph and vector
stores. Unchanged rows keep a stable content-hash ``data_id``, so they are not re-ingested or
re-cognified. Attio records carry no documented ``updated_at`` filter and there is no delete
feed, so a cursor-based sync could not see deletions, hence the snapshot model.
"""

import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("attio_connector")

# dlt resource / staging-table name for Attio rows.
ATTIO_TABLE_NAME = "attio_records"
ATTIO_SOURCE_NAME = "attio"

BASE_URL = "https://api.attio.com/v2"
# page sizes: Attio lists notes in pages of at most 50; records and entries default to 500
NOTES_PAGE_SIZE = 50
RECORDS_PAGE_SIZE = 500
# a runaway listing (a server that ignores ``offset``) must fail instead of looping forever
_MAX_PAGES = 10_000
# (connect, read) seconds; Attio documents no expected latency
_TIMEOUT = (10, 30)
# attempts per request on 429, 5xx or network errors
_RETRIES = 5
# longest wait honoured from a Retry-After header
_MAX_RETRY_WAIT = 60.0


class AttioSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class AttioAPIError(AttioSourceError):
    """Attio answered with an error, or could not be reached."""


class AttioAuthError(AttioSourceError):
    """Attio rejected the access token (HTTP 401)."""


class AttioAccessError(AttioSourceError):
    """The access token lacks the scope for an endpoint (HTTP 403)."""


class AttioClient:
    """Minimal synchronous REST client for Attio's v2 API.

    Rate-limit (429), server (5xx) and network errors are retried with backoff, honouring
    ``Retry-After``. Tests can pass any object with the same ``request`` method instead.
    """

    def __init__(self, api_key: str, *, sleep: Callable[[float], None] = time.sleep):
        key = (api_key or "").strip()
        # reject early and without echoing the key: requests would put it in its exception
        if not key or not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError("The Attio access token is empty or has an invalid format")
        self._api_key = key
        self._sleep = sleep

    def __repr__(self) -> str:
        return "AttioClient(api_key=<redacted>)"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = requests.request(
                    method,
                    BASE_URL + path,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=_TIMEOUT,
                )
            except requests.RequestException as exc:
                if last:
                    raise AttioAPIError(f"Attio request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue
            status = response.status_code
            if (status == 429 or status >= 500) and not last:
                self._sleep(_retry_after(response.headers, attempt))
                continue
            return self._parse(response, path)
        raise AttioAPIError("Attio request failed")  # unreachable; keeps the type checker honest

    @staticmethod
    def _parse(response: Any, path: str) -> dict[str, Any]:
        status = response.status_code
        if status == 401:
            raise AttioAuthError("Attio rejected the access token (HTTP 401)")
        if status == 403:
            raise AttioAccessError(
                f"The Attio access token lacks the scope for {_endpoint(path)} (HTTP 403)"
            )
        if status != 200:
            raise AttioAPIError(f"Attio request failed for {_endpoint(path)}: HTTP {status}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise AttioAPIError(f"Attio sent an unreadable answer for {_endpoint(path)}")
        return payload


def _endpoint(path: str) -> str:
    """The path without the workspace-specific slug, so messages stay generic."""
    parts = path.strip("/").split("/")
    return "/" + "/".join([*parts[:1], "{id}", *parts[2:]]) if len(parts) > 2 else path


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before a retry: Retry-After (seconds or HTTP date), else backoff."""
    raw = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    wait: float | None = None
    if raw:
        try:
            wait = float(raw)
        except (TypeError, ValueError):
            try:
                moment = parsedate_to_datetime(str(raw))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
                wait = (moment - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError):
                wait = None
    if wait is None:
        wait = float(2**attempt)
    return min(max(wait, 0.0), _MAX_RETRY_WAIT)


def attio_source(
    api_key: str | None = None,
    objects: list[str] | None = None,
    lists: list[str] | None = None,
    include_notes: bool = True,
    client: Any = None,
):
    """Create a dlt source that yields Attio records, notes and list entries as documents.

    Args:
        api_key: Attio access token. Falls back to ``ATTIO_API_KEY``.
        objects: Object slugs to ingest (for example ``["people", "companies"]``). When
            omitted, every object in the workspace is ingested, custom objects included.
        lists: List slugs whose entries to ingest. Off by default.
        include_notes: Also ingest the notes attached to the selected objects' records.
        client: Pre-built client (mainly a test-injection point); when omitted one is built
            from the token above.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    import dlt

    if client is None:
        resolved_key = api_key or os.environ.get("ATTIO_API_KEY")
        if not resolved_key:
            raise ValueError("Attio access token required: pass api_key= or set ATTIO_API_KEY.")
        client = AttioClient(resolved_key)

    selected_objects = list(objects) if objects else None
    selected_lists = list(lists) if lists else []

    @dlt.resource(name=ATTIO_TABLE_NAME, primary_key="id", write_disposition="replace")
    def attio_rows():
        # Full-snapshot sync: each run replaces staging with exactly the rows currently
        # visible to the token. A failure is NOT swallowed: because staging is authoritative
        # (replace), a row missing from a partial snapshot would be forgotten as if deleted.
        # Letting the error abort the run leaves staging, and memory, untouched.
        count = 0
        for row in iter_rows(client, selected_objects, selected_lists, include_notes):
            count += 1
            yield row
        logger.info("Attio: synced %d row(s).", count)

    @dlt.source(name=ATTIO_SOURCE_NAME)
    def _attio():
        return attio_rows

    source = _attio()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, ATTIO_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _paginate(
    client: Any,
    method: str,
    path: str,
    *,
    limit: int,
    params: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield every item of an Attio limit/offset listing.

    A page shorter than ``limit`` is not taken as the last one (a server may cap ``limit``
    below what was asked); the listing ends on the first empty page.
    """
    offset = 0
    for _ in range(_MAX_PAGES):
        if method == "POST":
            payload = client.request(method, path, body={"limit": limit, "offset": offset})
        else:
            payload = client.request(
                method, path, params={**(params or {}), "limit": limit, "offset": offset}
            )
        items = payload.get("data")
        if not isinstance(items, list):
            raise AttioAPIError(f"Attio sent an unreadable listing for {_endpoint(path)}")
        if not items:
            return
        yield from items
        offset += len(items)
    raise AttioAPIError(f"Attio listing for {_endpoint(path)} did not end")


def _listing(client: Any, path: str) -> list[dict[str, Any]]:
    """The items of an Attio listing that is not paged (objects, lists)."""
    items = client.request("GET", path).get("data")
    if not isinstance(items, list):
        raise AttioAPIError(f"Attio sent an unreadable listing for {_endpoint(path)}")
    return items


def iter_rows(
    client: Any,
    objects: list[str] | None = None,
    lists: list[str] | None = None,
    include_notes: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield one document row per record, note and list entry. Pure of dlt, so tests drive it."""
    workspace_objects = {
        obj["api_slug"]: obj for obj in _listing(client, "/objects") if obj.get("api_slug")
    }
    selected = _select(objects, workspace_objects, "object")
    attributes = {slug: _attributes(client, "objects", slug) for slug in selected}

    # Records are read in full first: a record may reference another one (a deal's company),
    # and the reference renders as that record's name.
    records: dict[str, list[dict[str, Any]]] = {}
    names: dict[tuple[str, str], str] = {}
    for slug in selected:
        records[slug] = list(
            _paginate(client, "POST", f"/objects/{slug}/records/query", limit=RECORDS_PAGE_SIZE)
        )
        for record in records[slug]:
            record_id = _record_id(record)
            title, _ = _title(record.get("values"), attributes[slug], _NO_NAMES)
            if record_id and title:
                names[(slug, record_id)] = title

    for slug in selected:
        singular = workspace_objects[slug].get("singular_noun") or slug
        for record in records[slug]:
            row = _record_row(record, slug, singular, attributes[slug], names)
            if row:
                yield row

    if include_notes:
        yield from _note_rows(client, set(selected), workspace_objects, names)

    if lists:
        workspace_lists = {
            item["api_slug"]: item for item in _listing(client, "/lists") if item.get("api_slug")
        }
        for slug in _select(lists, workspace_lists, "list"):
            yield from _entry_rows(client, slug, workspace_lists[slug], names)


def _select(wanted: list[str] | None, available: dict[str, Any], kind: str) -> list[str]:
    """The slugs to ingest: all of them, or the wanted ones after checking they exist."""
    if not wanted:
        return list(available)
    missing = [slug for slug in wanted if slug not in available]
    if missing:
        raise ValueError(
            f"Unknown Attio {kind} slug(s) {missing}; this workspace has {sorted(available)}"
        )
    return list(dict.fromkeys(wanted))


# system attributes that say nothing about the record: its own id, when it was created and a
# logo link. They would only add noise to the extracted graph (and a UUID could even become
# the title of an unnamed record).
_SKIPPED_ATTRIBUTES = frozenset({"record_id", "created_at", "logo_url"})


def _attributes(client: Any, target: str, slug: str) -> list[dict[str, Any]]:
    """The attributes worth ingesting of an object or list, in the order Attio shows them."""
    found = _paginate(client, "GET", f"/{target}/{slug}/attributes", limit=100)
    return [
        attr
        for attr in found
        if attr.get("api_slug")
        and not attr.get("is_archived")
        and attr["api_slug"] not in _SKIPPED_ATTRIBUTES
    ]


# ---------------------------------------------------------------------------
# Rendering: deterministic, first-party fields only, nothing volatile.
# ---------------------------------------------------------------------------

# a name lookup that knows nothing, used while the names themselves are being collected
_NO_NAMES: dict[tuple[str, str], str] = {}


def _record_id(record: dict[str, Any]) -> str | None:
    ident = record.get("id")
    return ident.get("record_id") if isinstance(ident, dict) else None


def _record_row(
    record: dict[str, Any],
    slug: str,
    singular: str,
    attributes: list[dict[str, Any]],
    names: dict[tuple[str, str], str],
) -> dict[str, Any] | None:
    record_id = _record_id(record)
    if not record_id:
        return None
    values = record.get("values")
    title, title_slug = _title(values, attributes, names)
    lines = _lines(values, attributes, names, skip=title_slug)
    if not title and not lines:
        return None
    title = title or f"Untitled {singular}"
    return {
        # the slug keeps ids unique across objects, which share nothing but the uuid format
        "id": f"{slug}:{record_id}",
        "url": record.get("web_url"),
        "title": title,
        "content": "\n".join([f"Type: {singular}", *lines]),
    }


def _title(
    values: Any,
    attributes: list[dict[str, Any]],
    names: dict[tuple[str, str], str],
) -> tuple[str, str | None]:
    """A record's display name and the attribute it came from.

    Prefers the ``name`` attribute (people, companies), then any personal-name attribute,
    then the first text attribute with a value.
    """
    if not isinstance(values, dict):
        return "", None
    ordered = [a for a in attributes if a["api_slug"] == "name"]
    ordered += [a for a in attributes if a.get("type") == "personal-name"]
    ordered += [a for a in attributes if a.get("type") == "text"]
    for attr in ordered:
        text = _render_attribute(values.get(attr["api_slug"]), names)
        if text:
            return text, attr["api_slug"]
    return "", None


def _lines(
    values: Any,
    attributes: list[dict[str, Any]],
    names: dict[tuple[str, str], str],
    skip: str | None = None,
) -> list[str]:
    """``Title: value`` lines for every attribute that has a value."""
    if not isinstance(values, dict):
        return []
    lines = []
    for attr in attributes:
        if attr["api_slug"] == skip:
            continue
        text = _render_attribute(values.get(attr["api_slug"]), names)
        if text:
            lines.append(f"{attr.get('title') or attr['api_slug']}: {text}")
    return lines


def _render_attribute(entries: Any, names: dict[tuple[str, str], str]) -> str:
    """All current values of one attribute, joined; empty when there is nothing to show."""
    if not isinstance(entries, list):
        return ""
    rendered = (_render_value(entry, names) for entry in entries if isinstance(entry, dict))
    return ", ".join(text for text in rendered if text)


def _render_value(value: dict[str, Any], names: dict[tuple[str, str], str]) -> str:
    """One Attio attribute value as text.

    Actor references and interactions are left out: the first is a bare member id, the
    second is derived data that changes without the record changing.
    """
    kind = value.get("attribute_type")
    if kind in ("text", "number", "date", "timestamp", "rating"):
        return _text(value.get("value"))
    if kind == "checkbox":
        return "Yes" if value.get("value") else "No"
    if kind == "currency":
        return f"{_text(value.get('currency_value'))} {_text(value.get('currency_code'))}".strip()
    if kind == "status":
        return _text((value.get("status") or {}).get("title"))
    if kind == "select":
        return _text((value.get("option") or {}).get("title"))
    if kind == "email-address":
        return _text(value.get("email_address"))
    if kind == "phone-number":
        return _text(value.get("original_phone_number") or value.get("phone_number"))
    if kind == "domain":
        return _text(value.get("domain"))
    if kind == "personal-name":
        return _text(value.get("full_name"))
    if kind == "location":
        parts = ("line_1", "line_2", "line_3", "line_4", "locality", "region", "postcode")
        place = [_text(value.get(part)) for part in parts]
        return ", ".join([*(p for p in place if p), _text(value.get("country_code"))]).strip(", ")
    if kind == "record-reference":
        return names.get(
            (_text(value.get("target_object")), _text(value.get("target_record_id"))), ""
        )
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


# ---------------------------------------------------------------------------
# Notes and list entries
# ---------------------------------------------------------------------------


def _note_rows(
    client: Any,
    selected: set[str],
    workspace_objects: dict[str, dict[str, Any]],
    names: dict[tuple[str, str], str],
) -> Iterator[dict[str, Any]]:
    for note in _paginate(client, "GET", "/notes", limit=NOTES_PAGE_SIZE):
        parent_object = _text(note.get("parent_object"))
        if parent_object not in selected:
            continue
        ident = note.get("id")
        note_id = ident.get("note_id") if isinstance(ident, dict) else None
        body = _text(note.get("content_plaintext"))
        title = _text(note.get("title"))
        if not note_id or not (body or title):
            continue
        parent = names.get((parent_object, _text(note.get("parent_record_id"))), "")
        singular = workspace_objects.get(parent_object, {}).get("singular_noun") or parent_object
        lines = [f"Note on {singular}: {parent}" if parent else f"Note on a {singular}"]
        lines.append(body or title)
        yield {
            "id": f"note:{note_id}",
            "url": None,
            "title": title or "Note",
            "content": "\n".join(lines),
        }


def _entry_rows(
    client: Any,
    slug: str,
    attio_list: dict[str, Any],
    names: dict[tuple[str, str], str],
) -> Iterator[dict[str, Any]]:
    attributes = _attributes(client, "lists", slug)
    list_name = _text(attio_list.get("name")) or slug
    for entry in _paginate(client, "POST", f"/lists/{slug}/entries/query", limit=RECORDS_PAGE_SIZE):
        ident = entry.get("id")
        entry_id = ident.get("entry_id") if isinstance(ident, dict) else None
        if not entry_id:
            continue
        parent = names.get(
            (_text(entry.get("parent_object")), _text(entry.get("parent_record_id")))
        )
        lines = _lines(entry.get("entry_values"), attributes, names)
        if parent:
            lines.insert(0, f"Record: {parent}")
        yield {
            "id": f"entry:{slug}:{entry_id}",
            "url": None,
            "title": f"{list_name}: {parent}" if parent else list_name,
            "content": "\n".join([f"List: {list_name}", *lines]),
        }
