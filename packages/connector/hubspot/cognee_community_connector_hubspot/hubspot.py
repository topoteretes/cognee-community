"""HubSpot connector for cognee: a ``dlt`` source that turns CRM records and their
relationships into memory.

Hand the source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_hubspot import hubspot_source

    await cognee.remember(
        hubspot_source(),            # credentials from the environment
        dataset_name="hubspot",
        primary_key="id",
        write_disposition="merge",   # REQUIRED, see below
        max_rows_per_table=0,        # compare forget-on-delete against every row
    )

Design
------
* **Document path, with relationships written out by name.** The value of CRM
  data is in the links between records. cognee's relational dlt path only links
  rows through database foreign keys, which dlt does not create, so association
  tables would land as unconnected rows. Instead every record becomes one
  document that names what it is linked to ("Companies: Acme Corp (acme.com)").
  cognify then extracts "Jane Doe", "Acme Corp" and "Q4 Renewal" as entities,
  and the same names in other documents join the same graph nodes.
* **Objects**: contacts, companies, deals and tickets, plus the engagements
  notes, calls, meetings, emails and tasks. Engagements get an "About:" line
  naming the records they are attached to.
* **Incremental cursor**: per object type, the search API filtered on the
  last-modified property (``lastmodifieddate`` for contacts, which silently
  returns nothing on ``hs_lastmodifieddate``; ``hs_lastmodifieddate`` for the
  rest), sorted ascending. Search stops at 10,000 results per query, so a large
  window restarts from the last timestamp seen. Search indexing lags a little,
  so the saved cursor stays a couple of minutes behind the run's start. The
  cursors live in dlt resource state, which dlt only commits after a successful
  load.
* **Relationships stay current**: when a contact, company, deal or ticket
  changes, the records linked to it are re-rendered too, so a renamed company is
  not left under its old name in its contacts' documents.
* **Forget-on-delete**: deleting a record only archives it, and search never
  returns archived records. So the archived list (kept for 90 days) is the
  delete feed: records archived since the last run are emitted as ``_deleted``
  hard-delete rows, and cognee's ``orphan_cleanup`` forgets them. Records merged
  into another one disappear without being archived; the survivor's
  ``hs_merged_object_ids`` names them, and they are tombstoned too. With
  ``reconcile_deletes=True`` a full ID listing also catches anything older or
  removed for good (GDPR deletes).
* **Personal data**: only an allowlist of properties is read per object (no
  birth date, gender, phone or address by default); add more with
  ``extra_properties``.

.. important::
   ``write_disposition="merge"`` is mandatory. The add pipeline defaults to
   ``"replace"``, which would wipe every record from earlier syncs on the second
   run, because the cursor only returns what changed.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("hubspot_connector")

HUBSPOT_SOURCE_NAME = "hubspot"
_API_BASE = "https://api.hubapi.com"
_SEARCH_PAGE = 200  # search API maximum
_SEARCH_CAP = 10_000  # search API: no more results per query
_BATCH = 100  # batch read / association read chunk size
_SEARCH_INTERVAL = 0.22  # the search API allows 5 requests per second per account
_INDEX_LAG_MS = 120_000  # search can lag behind fresh changes; re-read the last 2 minutes

CRM_OBJECTS = ("contacts", "companies", "deals", "tickets")
ENGAGEMENTS = ("notes", "calls", "meetings", "emails", "tasks")
ALL_OBJECTS = CRM_OBJECTS + ENGAGEMENTS
DEFAULT_OBJECTS = ("contacts", "companies", "deals", "tickets", "notes")

# Contacts use the legacy name; searching them on hs_lastmodifieddate returns nothing.
_MODIFIED = {"contacts": "lastmodifieddate"}

# What is read per object. Kept short on purpose: contacts carry ~90 properties by
# default, including date of birth and gender.
DEFAULT_PROPERTIES = {
    "contacts": ["firstname", "lastname", "email", "jobtitle", "company", "lifecyclestage"],
    "companies": ["name", "domain", "industry", "city", "country", "description"],
    "deals": ["dealname", "amount", "deal_currency_code", "pipeline", "dealstage", "closedate"],
    "tickets": ["subject", "content", "hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority"],
    "notes": ["hs_note_body", "hs_timestamp"],
    "calls": ["hs_call_title", "hs_call_body", "hs_call_direction", "hs_timestamp"],
    "meetings": ["hs_meeting_title", "hs_meeting_body", "hs_meeting_outcome", "hs_timestamp"],
    "emails": ["hs_email_subject", "hs_email_text", "hs_email_direction", "hs_timestamp"],
    "tasks": ["hs_task_subject", "hs_task_body", "hs_task_status", "hs_timestamp"],
}
# Properties used to name a record when it is listed on another record's document.
_LABEL_PROPERTIES = {
    "contacts": ["firstname", "lastname", "email"],
    "companies": ["name", "domain"],
    "deals": ["dealname", "amount", "deal_currency_code", "pipeline", "dealstage"],
    "tickets": ["subject", "hs_pipeline", "hs_pipeline_stage"],
}
# Which linked records each document lists.
_LINKS = {
    "contacts": ("companies", "deals", "tickets"),
    "companies": ("contacts", "deals", "tickets"),
    "deals": ("contacts", "companies"),
    "tickets": ("contacts", "companies", "deals"),
    **dict.fromkeys(ENGAGEMENTS, CRM_OBJECTS),
}
_SINGULAR = {
    "contacts": "Contact",
    "companies": "Company",
    "deals": "Deal",
    "tickets": "Ticket",
    "notes": "Note",
    "calls": "Call",
    "meetings": "Meeting",
    "emails": "Email",
    "tasks": "Task",
}
# Record page ids in the HubSpot UI (app.hubspot.com/contacts/<portal>/record/<id>/...).
_RECORD_TYPE_ID = {"contacts": "0-1", "companies": "0-2", "deals": "0-3", "tickets": "0-5"}

_COLUMNS = {
    "_deleted": {"data_type": "bool", "hard_delete": True},
    "title": {"data_type": "text", "nullable": True},
    "content": {"data_type": "text", "nullable": True},
    "url": {"data_type": "text", "nullable": True},
}


class HubSpotAPIError(RuntimeError):
    """A HubSpot API call failed permanently."""


# ---------------------------------------------------------------------------
# HTTP client (dlt's REST client: retries 429/5xx and honours Retry-After)
# ---------------------------------------------------------------------------
class HubSpotClient:
    """Thin wrapper over dlt's ``RESTClient``: ``get(path, **params)`` and
    ``post(path, body)`` return the decoded JSON or raise :class:`HubSpotAPIError`.

    With ``refresh`` (a callable returning a new access token), a 401 mints a new
    token and retries once: OAuth access tokens last 30 minutes, and a first sync of
    a large account can run longer than that."""

    def __init__(
        self,
        access_token: str,
        base_url: str = _API_BASE,
        refresh: Callable[[], str] | None = None,
    ):
        from dlt.sources.helpers.rest_client import RESTClient
        from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

        self._rest = RESTClient(base_url=base_url, auth=BearerTokenAuth(access_token))
        self._refresh = refresh
        self._last_search = 0.0

    def get(self, path: str, **params: Any) -> dict:
        return self._send(lambda: self._rest.get(path, params=params or None), path)

    def post(self, path: str, body: dict) -> dict:
        if path.endswith("/search"):
            wait = _SEARCH_INTERVAL - (time.monotonic() - self._last_search)
            if wait > 0:
                time.sleep(wait)
            self._last_search = time.monotonic()
        return self._send(lambda: self._rest.post(path, json=body), path)

    def _send(self, request: Callable[[], Any], path: str) -> dict:
        response = request()
        if response.status_code == 401 and self._refresh is not None:
            from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

            logger.info("HubSpot: access token expired, minting a new one.")
            self._rest.auth = BearerTokenAuth(self._refresh())
            response = request()
        return self._check(response, path)

    @staticmethod
    def _check(response, path: str) -> dict:
        if response.status_code in (401, 403):
            raise HubSpotAPIError(
                f"HubSpot rejected the request to {path} (HTTP {response.status_code}). "
                "Check the token has not expired and has the read scope for this object."
            )
        if response.status_code >= 400:
            raise HubSpotAPIError(f"HubSpot request to {path} failed: HTTP {response.status_code}.")
        return response.json() if response.content else {}


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange an OAuth refresh token for a fresh access token (they last 30 minutes)."""
    from dlt.sources.helpers import requests

    response = requests.post(
        f"{_API_BASE}/oauth/v1/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise HubSpotAPIError(f"HubSpot token refresh failed: HTTP {response.status_code}.")
    payload = response.json()
    if payload.get("refresh_token") and payload["refresh_token"] != refresh_token:
        logger.warning("HubSpot returned a new refresh token; store it for the next run.")
    return payload["access_token"]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def modified_property(object_type: str) -> str:
    return _MODIFIED.get(object_type, "hs_lastmodifieddate")


def to_ms(value: str | None) -> int | None:
    """HubSpot timestamps are ISO strings (or epoch ms for some properties)."""
    if not value:
        return None
    if str(value).isdigit():
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)


def iter_changed(client, object_type: str, since_ms: int | None, properties: list[str]):
    """Yield records modified after ``since_ms``, oldest first.

    The search API returns at most 10,000 results per query, so a window that
    reaches the cap starts a new query from the last timestamp seen (``GTE``,
    skipping ids already yielded at that timestamp).
    """
    prop = modified_property(object_type)
    operator, value = ("GT", since_ms) if since_ms is not None else ("GTE", 0)
    yielded_at_boundary: set[str] = set()
    while True:
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": prop, "operator": operator, "value": str(value)}]}
            ],
            "sorts": [{"propertyName": prop, "direction": "ASCENDING"}],
            "properties": properties,
            "limit": _SEARCH_PAGE,
        }
        after = 0
        last_ms, at_last_ms = None, set()
        while True:
            if after:
                body["after"] = str(after)
            data = client.post(f"/crm/v3/objects/{object_type}/search", body)
            for record in data.get("results") or []:
                ms = to_ms((record.get("properties") or {}).get(prop))
                if ms == value and record["id"] in yielded_at_boundary:
                    continue
                if ms != last_ms:
                    last_ms, at_last_ms = ms, set()
                at_last_ms.add(record["id"])
                yield record
            nxt = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not nxt:
                return
            after = int(nxt)
            if after + _SEARCH_PAGE > _SEARCH_CAP:
                break  # the next page would pass the cap: restart the window
        if last_ms is None or (operator == "GTE" and last_ms == value):
            raise HubSpotAPIError(
                f"More than {_SEARCH_CAP} {object_type} share one modified timestamp; "
                "the search API cannot page past them."
            )
        operator, value, yielded_at_boundary = "GTE", last_ms, at_last_ms


def iter_archived(client, object_type: str):
    """Records archived (deleted) in the last 90 days, with ``archivedAt``."""
    params = {"archived": "true", "limit": 100, "properties": modified_property(object_type)}
    while True:
        data = client.get(f"/crm/v3/objects/{object_type}", **params)
        yield from data.get("results") or []
        nxt = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not nxt:
            return
        params["after"] = nxt


def list_ids(client, object_type: str) -> set[str]:
    """Every live record id (for ``reconcile_deletes``). Raises if a page fails."""
    ids: set[str] = set()
    params: dict[str, Any] = {"limit": 100, "properties": "hs_object_id"}
    while True:
        data = client.get(f"/crm/v3/objects/{object_type}", **params)
        ids.update(r["id"] for r in data.get("results") or [])
        nxt = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not nxt:
            return ids
        params["after"] = nxt


def _chunks(items: list, size: int = _BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def read_associations(client, from_type: str, to_type: str, ids: Iterable[str]) -> dict:
    """``{from_id: [to_id, ...]}`` through the v4 batch associations API."""
    links: dict[str, list[str]] = {}
    for chunk in _chunks(sorted(set(ids))):
        data = client.post(
            f"/crm/v4/associations/{from_type}/{to_type}/batch/read",
            {"inputs": [{"id": i} for i in chunk]},
        )
        for result in data.get("results") or []:
            source = str((result.get("from") or {}).get("id"))
            targets = {str(t.get("toObjectId")) for t in result.get("to") or []}
            links.setdefault(source, []).extend(sorted(targets))
    return links


def batch_read(client, object_type: str, ids: Iterable[str], properties: list[str]) -> dict:
    """``{id: record}`` for live records (archived ones are simply missing)."""
    records = {}
    for chunk in _chunks(sorted(set(ids))):
        data = client.post(
            f"/crm/v3/objects/{object_type}/batch/read",
            {"properties": properties, "inputs": [{"id": i} for i in chunk]},
        )
        for record in data.get("results") or []:
            records[str(record["id"])] = record
    return records


class _Lookups:
    """Owner names and pipeline/stage labels, each read at most once per run.
    A missing scope only drops the label (the raw value is shown instead)."""

    def __init__(self, client):
        self._client = client
        self._owners: dict[str, str] | None = None
        self._stages: dict[str, dict[str, str]] = {}

    def owner(self, owner_id) -> str | None:
        if not owner_id:
            return None
        if self._owners is None:
            self._owners = {}
            try:
                params: dict[str, Any] = {"limit": 100}
                while True:
                    data = self._client.get("/crm/v3/owners", **params)
                    for o in data.get("results") or []:
                        name = " ".join(p for p in (o.get("firstName"), o.get("lastName")) if p)
                        self._owners[str(o.get("id"))] = name or o.get("email") or str(o.get("id"))
                    nxt = ((data.get("paging") or {}).get("next") or {}).get("after")
                    if not nxt:
                        break
                    params["after"] = nxt
            except HubSpotAPIError as exc:
                logger.warning("HubSpot: owner names unavailable (%s).", exc)
        return self._owners.get(str(owner_id), f"owner {owner_id}")

    def stage(self, object_type: str, value) -> str | None:
        if not value:
            return None
        if object_type not in self._stages:
            labels: dict[str, str] = {}
            try:
                data = self._client.get(f"/crm/v3/pipelines/{object_type}")
                for pipeline in data.get("results") or []:
                    labels[str(pipeline.get("id"))] = pipeline.get("label")
                    for stage in pipeline.get("stages") or []:
                        labels[str(stage.get("id"))] = stage.get("label")
            except HubSpotAPIError as exc:
                logger.warning("HubSpot: %s pipeline labels unavailable (%s).", object_type, exc)
            self._stages[object_type] = labels
        return self._stages[object_type].get(str(value), str(value))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    _BREAKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre"})

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """Note, call and email bodies are HTML; keep the text and line breaks."""
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def record_label(object_type: str, props: dict, lookups: _Lookups | None = None) -> str:
    """How a record is named when it appears on another record's document."""
    p = props or {}
    if object_type == "contacts":
        name = " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x)
        if name and p.get("email"):
            return f"{name} <{p['email']}>"
        return name or p.get("email") or "unnamed contact"
    if object_type == "companies":
        name = p.get("name") or p.get("domain") or "unnamed company"
        return f"{name} ({p['domain']})" if p.get("name") and p.get("domain") else name
    if object_type == "deals":
        details = []
        if p.get("amount"):
            details.append(f"{p['amount']} {p.get('deal_currency_code') or ''}".strip())
        if p.get("dealstage"):
            stage = lookups.stage("deals", p["dealstage"]) if lookups else p["dealstage"]
            details.append(stage)
        name = p.get("dealname") or "unnamed deal"
        return f"{name} ({', '.join(details)})" if details else name
    if object_type == "tickets":
        stage = p.get("hs_pipeline_stage")
        if stage and lookups:
            stage = lookups.stage("tickets", stage)
        name = p.get("subject") or "untitled ticket"
        return f"{name} ({stage})" if stage else name
    return _SINGULAR.get(object_type, object_type)


def _when(value) -> str:
    ms = to_ms(value)
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if ms else ""


def _body(object_type: str, p: dict) -> tuple[str, str]:
    """(headline, text) of an engagement."""
    if object_type == "notes":
        return "", html_to_text(p.get("hs_note_body"))
    if object_type == "calls":
        return p.get("hs_call_title") or "", html_to_text(p.get("hs_call_body"))
    if object_type == "meetings":
        return p.get("hs_meeting_title") or "", html_to_text(p.get("hs_meeting_body"))
    if object_type == "emails":
        return p.get("hs_email_subject") or "", (p.get("hs_email_text") or "").strip()
    if object_type == "tasks":
        return p.get("hs_task_subject") or "", html_to_text(p.get("hs_task_body"))
    return "", ""


def _first_words(text: str, limit: int = 60) -> str:
    line = (text or "").strip().split("\n", 1)[0]
    if len(line) <= limit:
        return line
    return line[:limit].rsplit(" ", 1)[0] + "..."


def record_to_row(
    object_type: str,
    record: dict,
    links: dict[str, list[str]],
    portal_id: str | None,
    lookups: _Lookups | None = None,
    extra_properties: Iterable[str] = (),
) -> dict[str, Any]:
    """Render one record as a deterministic document row.

    ``links`` maps a linked object type to the labels of the linked records.
    Modified timestamps are left out on purpose, so an unchanged record keeps the
    same content hash and is not re-cognified.
    """
    p = record.get("properties") or {}
    rid = str(record["id"])
    singular = _SINGULAR[object_type]
    lines: list[str] = []

    if object_type in CRM_OBJECTS:
        name = record_label(object_type, p, lookups)
        title = f"{singular}: {name}"
        lines.append(title)
        if object_type == "contacts":
            if p.get("jobtitle"):
                lines.append(f"Job title: {p['jobtitle']}")
            # The free-text company field, only when no company record is linked.
            if p.get("company") and not links.get("companies"):
                lines.append(f"Company: {p['company']}")
        if object_type == "companies":
            for label, key in (("Industry", "industry"), ("City", "city"), ("Country", "country")):
                if p.get(key):
                    lines.append(f"{label}: {p[key]}")
        if object_type == "deals":
            if p.get("pipeline"):
                pipeline = lookups.stage("deals", p["pipeline"]) if lookups else p["pipeline"]
                lines.append(f"Pipeline: {pipeline}")
            if p.get("closedate"):
                lines.append(f"Close date: {_when(p['closedate'])[:10]}")
        if object_type == "tickets" and p.get("hs_ticket_priority"):
            lines.append(f"Priority: {p['hs_ticket_priority']}")
        if p.get("lifecyclestage"):
            lines.append(f"Lifecycle stage: {p['lifecyclestage']}")
        if lookups and p.get("hubspot_owner_id"):
            lines.append(f"Owner: {lookups.owner(p['hubspot_owner_id'])}")
        for key in extra_properties:
            if p.get(key) not in (None, ""):
                lines.append(f"{key}: {p[key]}")
        for linked_type in _LINKS[object_type]:
            if links.get(linked_type):
                lines.append(f"{linked_type.capitalize()}: " + "; ".join(links[linked_type]))
        text = p.get("description") or p.get("content") or ""
        content = "\n".join(lines) + (f"\n\n{text.strip()}" if text and text.strip() else "")
    else:
        headline, text = _body(object_type, p)
        about = [
            f"{_SINGULAR[t]} {label}" for t in _LINKS[object_type] for label in links.get(t, [])
        ]
        # Notes have no subject, so their title is the start of the text.
        headline = headline or _first_words(text)
        title = f"{singular}: {headline}" if headline else f"{singular} {rid}"
        lines.append(title)
        when = _when(p.get("hs_timestamp"))
        if when:
            lines.append(f"Date: {when}")
        if lookups and p.get("hubspot_owner_id"):
            lines.append(f"By: {lookups.owner(p['hubspot_owner_id'])}")
        for label, key in (
            ("Direction", "hs_call_direction"),
            ("Direction", "hs_email_direction"),
            ("Outcome", "hs_meeting_outcome"),
            ("Status", "hs_task_status"),
        ):
            if p.get(key):
                lines.append(f"{label}: {p[key]}")
        for key in extra_properties:
            if p.get(key) not in (None, ""):
                lines.append(f"{key}: {p[key]}")
        if about:
            lines.append("About: " + "; ".join(about))
        content = "\n".join(lines) + (f"\n\n{text}" if text else "")

    url = None
    if portal_id and object_type in _RECORD_TYPE_ID:
        url = (
            f"https://app.hubspot.com/contacts/{portal_id}/record/"
            f"{_RECORD_TYPE_ID[object_type]}/{rid}"
        )
    return {"id": rid, "title": title, "content": content, "url": url, "_deleted": False}


def _digest(text: str) -> str:
    """A short hash, so state can tell a label changed without storing names."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def deleted_row(row_id: str) -> dict[str, Any]:
    return {"id": str(row_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _properties_for(object_type: str, extra: dict[str, list[str]]) -> list[str]:
    props = list(DEFAULT_PROPERTIES[object_type]) + list(extra.get(object_type, []))
    props += [modified_property(object_type), "hubspot_owner_id"]
    if object_type in CRM_OBJECTS:
        props.append("hs_merged_object_ids")
    return sorted(set(props))


def sync_hubspot(
    client,
    state: dict,
    objects: Iterable[str] = DEFAULT_OBJECTS,
    extra_properties: dict[str, list[str]] | None = None,
    portal_id: str | None = None,
    reconcile_deletes: bool = False,
    now_ms: int | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(object_type, row)`` for changed records and deletions.

    ``state`` holds, per object type, the search cursor (``cursors``), the
    archived-feed cursor (``archived``) and the ids stored so far (``known``),
    plus which records each stored document names (``links``) and a hash of each
    record's label (``labels``). HubSpot drops a deleted record's associations, so
    ``links`` is how the documents that named it are found and refreshed. dlt only
    persists the state after a successful load, so a failed run starts over from the
    same point.
    """
    objects = [o for o in ALL_OBJECTS if o in set(objects)]
    extra = extra_properties or {}
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cursors: dict[str, int] = state.setdefault("cursors", {})
    archived_cursor: dict[str, int] = state.setdefault("archived", {})
    known: dict[str, list[str]] = state.setdefault("known", {})
    known_sets = {o: set(known.get(o, [])) for o in objects}
    doc_links: dict[str, list[str]] = state.setdefault("links", {})
    label_digests: dict[str, str] = state.setdefault("labels", {})
    lookups = _Lookups(client)

    # 1. changed records per object type
    changed: dict[str, dict[str, dict]] = {o: {} for o in objects}
    tombstones: dict[str, set[str]] = {o: set() for o in objects}
    for object_type in objects:
        prop = modified_property(object_type)
        newest = None
        for record in iter_changed(
            client, object_type, cursors.get(object_type), _properties_for(object_type, extra)
        ):
            rid = str(record["id"])
            changed[object_type][rid] = record
            ms = to_ms((record.get("properties") or {}).get(prop))
            newest = ms if newest is None or (ms is not None and ms > newest) else newest
            merged = (record.get("properties") or {}).get("hs_merged_object_ids") or ""
            for old in filter(None, (m.strip() for m in str(merged).split(";"))):
                if old != rid and old in known_sets[object_type]:
                    tombstones[object_type].add(old)
        if newest is not None:
            cursors[object_type] = min(newest, now_ms - _INDEX_LAG_MS)

    # 2. deletions: the archived feed, then (optionally) a full id comparison
    for object_type in objects:
        since = archived_cursor.get(object_type)
        latest = since
        for record in iter_archived(client, object_type):
            at = to_ms(record.get("archivedAt"))
            if at is None or (since is not None and at <= since):
                continue
            latest = at if latest is None or at > latest else latest
            if str(record["id"]) in known_sets[object_type]:
                tombstones[object_type].add(str(record["id"]))
        if latest is not None:
            archived_cursor[object_type] = latest
        if reconcile_deletes:
            live = list_ids(client, object_type)
            tombstones[object_type] |= known_sets[object_type] - live

    # 3. other documents are re-rendered only when what they show changed: a linked
    #    record was deleted or renamed (its label), or a link was added or removed.
    #    HubSpot bumps the modified date on its own (activity counts, rollups), and a
    #    bump alone must not ripple through every linked document.
    crm_selected = [o for o in CRM_OBJECTS if o in objects]
    rerender: dict[str, set[str]] = {o: set() for o in objects}
    touched = {f"{o}:{rid}" for o in objects for rid in tombstones[o]}
    for object_type in crm_selected:
        for rid, record in changed[object_type].items():
            props = record.get("properties") or {}
            digest = _digest(record_label(object_type, props, lookups))
            if label_digests.get(f"{object_type}:{rid}") != digest:
                label_digests[f"{object_type}:{rid}"] = digest
                touched.add(f"{object_type}:{rid}")
    for doc, named in doc_links.items():
        object_type, rid = doc.split(":", 1)
        if object_type in rerender and touched.intersection(named):
            rerender[object_type].add(rid)
    for object_type in crm_selected:
        ids = set(changed[object_type])
        if not ids:
            continue
        for linked_type in _LINKS[object_type]:
            if linked_type not in crm_selected:
                continue
            now_linked = read_associations(client, object_type, linked_type, ids)
            for rid in ids:
                before = {
                    named.split(":", 1)[1]
                    for named in doc_links.get(f"{object_type}:{rid}", [])
                    if named.startswith(f"{linked_type}:")
                }
                for target in before.symmetric_difference(now_linked.get(rid, [])):
                    if target not in changed[linked_type]:
                        rerender[linked_type].add(target)
    for object_type, ids in rerender.items():
        ids -= tombstones[object_type] | set(changed[object_type])
        if ids:
            changed[object_type].update(
                batch_read(client, object_type, ids, _properties_for(object_type, extra))
            )

    # 4. render, with the linked records named
    labels: dict[str, dict[str, str]] = {o: {} for o in CRM_OBJECTS}
    for object_type in objects:
        records = changed[object_type]
        for rid in tombstones[object_type]:
            records.pop(rid, None)
        if not records:
            continue
        links_by_type: dict[str, dict[str, list[str]]] = {}
        assoc_ids: dict[str, dict[str, list[str]]] = {}
        for linked_type in _LINKS[object_type]:
            if linked_type not in crm_selected:
                continue
            assoc = read_associations(client, object_type, linked_type, records)
            assoc_ids[linked_type] = assoc
            missing = {t for ts in assoc.values() for t in ts} - set(labels[linked_type])
            for tid, rec in batch_read(
                client, linked_type, missing, _LABEL_PROPERTIES[linked_type]
            ).items():
                labels[linked_type][tid] = record_label(
                    linked_type, rec.get("properties") or {}, lookups
                )
            links_by_type[linked_type] = {
                rid: sorted(labels[linked_type][t] for t in ts if t in labels[linked_type])
                for rid, ts in assoc.items()
            }
        for rid, record in records.items():
            links = {t: by_id.get(rid, []) for t, by_id in links_by_type.items()}
            yield (
                object_type,
                record_to_row(
                    object_type, record, links, portal_id, lookups, extra.get(object_type, ())
                ),
            )
            known_sets[object_type].add(rid)
            named = sorted(f"{t}:{i}" for t, ids in assoc_ids.items() for i in ids.get(rid, []))
            if named:
                doc_links[f"{object_type}:{rid}"] = named
            else:
                doc_links.pop(f"{object_type}:{rid}", None)

    for object_type in objects:
        for rid in sorted(tombstones[object_type]):
            known_sets[object_type].discard(rid)
            doc_links.pop(f"{object_type}:{rid}", None)
            label_digests.pop(f"{object_type}:{rid}", None)
            yield object_type, deleted_row(rid)
        known[object_type] = sorted(known_sets[object_type])

    summary = ", ".join(
        f"{o}: {len(changed[o])} rendered / {len(tombstones[o])} deleted" for o in objects
    )
    logger.info("HubSpot: %s.", summary)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _portal_id(client) -> str | None:
    try:
        return str(client.get("/account-info/v3/details").get("portalId") or "") or None
    except HubSpotAPIError:
        return None


def hubspot_source(
    access_token: str | None = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    objects: Iterable[str] = DEFAULT_OBJECTS,
    extra_properties: dict[str, list[str]] | None = None,
    reconcile_deletes: bool = False,
    portal_id: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields HubSpot records as documents.

    Args:
        access_token: An OAuth access token, a private app token or a Service Key
            (all sent as ``Bearer``). Falls back to ``HUBSPOT_ACCESS_TOKEN``.
        client_id / client_secret / refresh_token: OAuth app credentials; when all
            three are set (or ``HUBSPOT_CLIENT_ID`` / ``HUBSPOT_CLIENT_SECRET`` /
            ``HUBSPOT_REFRESH_TOKEN``), a fresh access token is minted per run.
        objects: Any of contacts, companies, deals, tickets, notes, calls,
            meetings, emails, tasks.
        extra_properties: Extra properties to read per object type, e.g.
            ``{"contacts": ["phone"]}``. Nothing beyond the defaults is read
            otherwise.
        reconcile_deletes: Also list every id to catch records removed for good
            or archived more than 90 days ago (one request per 100 records).
        portal_id: The HubSpot account id, for record links and state scoping;
            looked up when omitted.
        client: Object with ``get(path, **params)`` and ``post(path, body)``; a
            test injection point.

    Returns:
        A dlt source with one resource writing one table per object type
        (``hubspot_contacts``, ...), each with ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The HubSpot connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    objects = tuple(objects)
    unknown = set(objects) - set(ALL_OBJECTS)
    if unknown:
        raise ValueError(f"Unknown HubSpot objects {sorted(unknown)}; choose from {ALL_OBJECTS}.")
    if client is None:
        client_id = client_id or os.environ.get("HUBSPOT_CLIENT_ID")
        client_secret = client_secret or os.environ.get("HUBSPOT_CLIENT_SECRET")
        refresh_token = refresh_token or os.environ.get("HUBSPOT_REFRESH_TOKEN")
        token = access_token or os.environ.get("HUBSPOT_ACCESS_TOKEN")
        refresh = None
        if client_id and client_secret and refresh_token:

            def refresh() -> str:
                return refresh_access_token(client_id, client_secret, refresh_token)

            token = refresh()
        if not token:
            raise ValueError(
                "HubSpot credentials required: an access token (HUBSPOT_ACCESS_TOKEN) or "
                "OAuth app credentials (HUBSPOT_CLIENT_ID, HUBSPOT_CLIENT_SECRET, "
                "HUBSPOT_REFRESH_TOKEN)."
            )
        client = HubSpotClient(token, refresh=refresh)
    portal_id = portal_id or os.environ.get("HUBSPOT_PORTAL_ID") or _portal_id(client)

    @dlt.resource(
        name=HUBSPOT_SOURCE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_COLUMNS,
    )
    def hubspot_records():
        for object_type, row in sync_hubspot(
            client,
            dlt.current.resource_state(),
            objects=objects,
            extra_properties=extra_properties,
            portal_id=portal_id,
            reconcile_deletes=reconcile_deletes,
        ):
            yield dlt.mark.with_table_name(row, f"hubspot_{object_type}")

    @dlt.source(name=HUBSPOT_SOURCE_NAME)
    def _hubspot():
        return hubspot_records

    source = _hubspot()
    # CRM records are rendered as text with their relationships: route them
    # through cognify (document mode).
    setattr(source, DOCUMENT_SOURCE_ATTR, HUBSPOT_SOURCE_NAME)
    # Keep the cursors per HubSpot account (and, in cognee, per dataset).
    setattr(source, PIPELINE_SCOPE_ATTR, f"hubspot:{portal_id or 'default'}")
    return source
