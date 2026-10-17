"""Freshdesk connector for cognee — a ``dlt`` source that turns a helpdesk into memory.

Sync tickets (with their conversations) and published solution articles into
cognee, incrementally and with forget-on-deletion.  The resource produced here
is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_freshdesk import freshdesk_source

    await cognee.remember(
        freshdesk_source(domain="acme", api_key="..."),
        dataset_name="support",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by row id
        max_rows_per_table=0,        # 0 = no row cap (helpdesks exceed the default 50)
    )

Design
------
* **Auth** — an API key sent as HTTP Basic ``key:X``; both the classic keys and
  the newer ``fwapi_`` keys work this way.
* **Tickets: incremental** — ``updated_since`` is inclusive, so the cursor is the
  newest ``updated_at`` seen plus the ids sitting exactly on it, which are
  skipped next run unless they changed again.  A new reply or note moves the
  ticket's ``updated_at``, so conversations ride on the same cursor.
* **Tickets: deletes** — a deleted or spammed ticket drops out of the normal
  ``updated_since`` listing entirely, so every run also lists
  ``filter=deleted`` and ``filter=spam`` since the cursor and forgets those.  A
  restored ticket comes back through the normal listing.
* **Articles: snapshot** — solutions have no change feed, so every run walks
  categories, folders and every level of subfolders.  Only published articles
  are kept; an article that is gone, or no longer published, is forgotten.
* **Rate limits** — 429 waits for ``Retry-After``, 5xx backs off, and requests
  slow down when ``X-Ratelimit-Remaining`` runs low.  Every request has a timeout.
* **Errors never delete** — every API call happens before the first row is
  yielded and state is written after the last, so a failed run changes
  nothing.  Only a confirmed 404 is read as "gone".

Limitations
-----------
* Freshdesk's API returns the latest *saved* text of a published article, which
  can include edits that were never published.  The connector cannot tell.
* Attachments are not ingested.  Articles are read in the default language only.
"""

import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("freshdesk_connector")

PAGE_SIZE = 100
# List endpoints stop at page 300 (noted in dlt's verified Freshdesk source).
# Tickets are listed oldest-first, so the listing restarts from the last
# updated_at instead of paging further.
MAX_PAGES = 300
# Older than Freshdesk itself.  The default ticket list only covers the last
# 30 days, so the first run always passes updated_since explicitly.
EPOCH = "2000-01-01T00:00:00Z"
MAX_FOLDER_DEPTH = 10
PUBLISHED = 2
ID_PREFIX = "freshdesk:"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 5
# Slow down once this few requests are left in the current rate-limit window.
RATE_LIMIT_RESERVE = 3
USER_AGENT = "cognee-community-connector-freshdesk/0.1"

TICKET_STATUSES = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
TICKET_PRIORITIES = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
TICKET_SOURCES = {
    1: "Email",
    2: "Portal",
    3: "Phone",
    7: "Chat",
    9: "Feedback widget",
    10: "Outbound email",
}


class FreshdeskError(Exception):
    """A Freshdesk API error that the client did not recover from."""

    def __init__(self, status: int, path: str):
        super().__init__(f"Freshdesk API returned HTTP {status} for {path}")
        self.status = status
        self.path = path


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def normalize_domain(domain: str) -> str:
    """``acme``, ``acme.freshdesk.com`` or ``https://acme.freshdesk.com/`` -> host."""
    value = (domain or "").strip().lower()
    if "://" in value:
        value = urlparse(value).netloc
    value = value.strip("/").split("/")[0]
    if not value:
        raise ValueError("Freshdesk domain is empty. Pass the part before .freshdesk.com.")
    return value if "." in value else f"{value}.freshdesk.com"


def _header_int(headers, name: str) -> int | None:
    # Freshdesk sends rate-limit counters both as "48" and as "38.0".
    try:
        return int(float(headers.get(name)))
    except (TypeError, ValueError):
        return None


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return match.group(1) if match else None


class FreshdeskClient:
    """Minimal Freshdesk v2 client: auth, paging, retries and polite pacing."""

    def __init__(
        self,
        domain: str,
        api_key: str,
        *,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_pages: int = MAX_PAGES,
    ):
        self.host = normalize_domain(domain)
        self.base_url = f"https://{self.host}/api/v2"
        self._auth = (api_key, "X")
        self._session = session or requests.Session()
        self._sleep = sleep
        self.max_pages = max_pages

    def _get(self, url: str, params: dict | None, path: str):
        status = None
        for attempt in range(MAX_RETRIES + 1):
            response = self._session.get(
                url,
                params=params,
                auth=self._auth,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            status = response.status_code
            if status != 429 and status < 500:
                self._pace(response.headers)
                return response
            if attempt == MAX_RETRIES:
                break
            if status == 429:
                wait = _header_int(response.headers, "Retry-After") or 60
            else:
                wait = min(2**attempt, 30)
            logger.info("Freshdesk: HTTP %s on %s, retrying in %ss", status, path, wait)
            self._sleep(wait)
        raise FreshdeskError(status, path)

    def _pace(self, headers) -> None:
        remaining = _header_int(headers, "X-Ratelimit-Remaining")
        total = _header_int(headers, "X-Ratelimit-Total")
        if remaining is not None and total and remaining <= RATE_LIMIT_RESERVE:
            # Spread what is left over the minute instead of running into 429s.
            self._sleep(60 / total)

    def list_all(self, path: str, params: dict | None = None) -> tuple[list[dict], bool]:
        """Every item of a paged listing.  Returns (items, truncated_at_page_limit)."""
        query = {"per_page": PAGE_SIZE, **(params or {})}
        url: str | None = self.base_url + path
        items: list[dict] = []
        pages = 0
        while url:
            response = self._get(url, query, path)
            if response.status_code >= 400:
                raise FreshdeskError(response.status_code, path)
            items.extend(response.json())
            pages += 1
            url = _next_link(response.headers.get("link"))
            query = None  # the next link already carries every parameter
            if url and pages >= self.max_pages:
                return items, True
        return items, False


# ---------------------------------------------------------------------------
# Sync state machine (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Config:
    tickets: bool = True
    conversations: bool = True
    private_notes: bool = False
    articles: bool = True


def _iter_rows(client: FreshdeskClient, config: _Config, state: dict, stats: dict | None = None):
    """Yield one row per new or changed ticket or article, plus hard-delete tombstones.

    ``state`` is a plain dict standing in for dlt's resource state, so this is
    directly unit-testable.  Every API call happens before the first yield,
    and the state is only written after the last one, so a failed run changes
    nothing.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(tickets=0, articles=0, skipped=0, deleted=0)

    rows: list[dict] = []
    deletes: list[str] = []
    new_state: dict = {}

    if config.tickets:
        new_state["tickets"] = _sync_tickets(
            client, config, state.get("tickets", {}), rows, deletes, stats
        )
    else:
        # Tickets were switched off: they are out of scope now.
        for ticket_id in state.get("tickets", {}).get("known", []):
            _forget(client, "ticket", ticket_id, deletes, stats)

    if config.articles:
        new_state["articles"] = _sync_articles(
            client, state.get("articles", {}), rows, deletes, stats
        )
    else:
        for article_id in sorted(state.get("articles", {}).get("known", {})):
            _forget(client, "article", article_id, deletes, stats)

    for row_id in deletes:
        yield {"id": row_id, "_deleted": True}
    yield from rows

    state.clear()
    state.update(new_state)
    logger.info("Freshdesk: sync stats %s", stats)


def _sync_tickets(client, config: _Config, old: dict, rows, deletes, stats) -> dict:
    """One ticket round. Returns the new ticket state."""
    known: set[int] = set(old.get("known", []))
    render = [config.conversations, config.private_notes]
    # First run, or the rendering options changed: list everything again and
    # reconcile like a snapshot.
    full = not old.get("cursor") or old.get("render") != render
    since = EPOCH if full else old["cursor"]
    on_cursor = set() if full else set(old.get("on_cursor", []))

    listed = _list_tickets(client, since)
    gone: set[int] = set()
    if not full:
        for name in ("deleted", "spam"):
            items, truncated = client.list_all("/tickets", {"filter": name, "updated_since": since})
            if truncated:
                raise RuntimeError(
                    f"More than {client.max_pages} pages of {name} tickets since the last "
                    "sync; run a full resync instead."
                )
            gone |= {t["id"] for t in items}

    present: set[int] = set()
    for ticket in listed:
        ticket_id = ticket["id"]
        present.add(ticket_id)
        if ticket_id in gone:
            continue
        if ticket_id in on_cursor and ticket["updated_at"] == since:
            stats["skipped"] += 1  # already synced on the cursor boundary last run
            continue
        conversations: list[dict] = []
        if config.conversations:
            try:
                conversations, _ = client.list_all(f"/tickets/{ticket_id}/conversations")
            except FreshdeskError as exc:
                if exc.status != 404:
                    raise
                # Deleted between the listing and now: the deleted pass forgets it.
                continue
            if not config.private_notes:
                conversations = [c for c in conversations if not c.get("private")]
        known.add(ticket_id)
        stats["tickets"] += 1
        rows.append(_ticket_row(client.host, ticket, conversations))

    if full:
        # A full listing has every live ticket, so anything known but missing
        # was deleted, spammed or purged meanwhile.
        gone |= known - present
    for ticket_id in sorted(gone & known):
        known.discard(ticket_id)
        _forget(client, "ticket", ticket_id, deletes, stats)

    # Only the normal listing moves the cursor.  A ticket deleted after that
    # listing can carry a later updated_at than a ticket edited in between,
    # and jumping to it would skip the edit forever.
    cursor = max((t["updated_at"] for t in listed), default=since)
    return {
        "cursor": cursor,
        "on_cursor": sorted({t["id"] for t in listed if t["updated_at"] == cursor}),
        "known": sorted(known),
        "render": render,
    }


def _list_tickets(client: FreshdeskClient, since: str) -> list[dict]:
    """Every live ticket updated at or after ``since``, past the page limit."""
    tickets: dict[int, dict] = {}
    while True:
        items, truncated = client.list_all(
            "/tickets",
            {
                "updated_since": since,
                "order_by": "updated_at",
                "order_type": "asc",
                # Costs the same as description alone and names the requester.
                "include": "requester,description",
            },
        )
        for ticket in items:
            tickets[ticket["id"]] = ticket
        if not truncated:
            return list(tickets.values())
        last = max(t["updated_at"] for t in items)
        if last == since:
            raise RuntimeError(
                "A whole page-limited listing shares one updated_at; cannot page further."
            )
        since = last


def _sync_articles(client, old: dict, rows, deletes, stats) -> dict:
    """Walk every published article. Returns the new article state."""
    known: dict[str, str] = dict(old.get("known", {}))
    present: dict[str, str] = {}
    for article in _walk_articles(client):
        if article.get("status") != PUBLISHED:
            stats["skipped"] += 1
            continue
        article_id = str(article["id"])
        present[article_id] = article["updated_at"]
        if known.get(article_id) == article["updated_at"]:
            continue
        stats["articles"] += 1
        rows.append(_article_row(client.host, article))
    for article_id in sorted(set(known) - set(present)):
        _forget(client, "article", article_id, deletes, stats)
    return {"known": present}


def _walk_articles(client: FreshdeskClient) -> Iterator[dict]:
    for category in _list_or_empty(client, "/solutions/categories"):
        seen: set[int] = set()
        for folder in _list_or_empty(client, f"/solutions/categories/{category['id']}/folders"):
            yield from _walk_folder(client, folder, seen, depth=1)


def _walk_folder(client, folder: dict, seen: set[int], depth: int) -> Iterator[dict]:
    if folder["id"] in seen:
        return
    if depth > MAX_FOLDER_DEPTH:
        # Skipping would forget every article below, so stop instead.
        raise RuntimeError(f"Freshdesk folders nest deeper than {MAX_FOLDER_DEPTH} levels.")
    seen.add(folder["id"])
    yield from _list_or_empty(client, f"/solutions/folders/{folder['id']}/articles")
    # The category listing only returns top-level folders; articles in
    # subfolders are reachable only through this call.
    for sub in _list_or_empty(client, f"/solutions/folders/{folder['id']}/subfolders"):
        yield from _walk_folder(client, sub, seen, depth + 1)


def _list_or_empty(client: FreshdeskClient, path: str) -> list[dict]:
    """A listing, or [] when its parent was deleted meanwhile (confirmed 404)."""
    try:
        items, truncated = client.list_all(path)
    except FreshdeskError as exc:
        if exc.status != 404:
            raise
        return []
    if truncated:
        raise RuntimeError(f"More than {client.max_pages} pages under {path}.")
    return items


def _forget(client, kind: str, item_id, deletes: list[str], stats: dict) -> None:
    stats["deleted"] += 1
    deletes.append(_row_id(client.host, kind, item_id))


def _row_id(host: str, kind: str, item_id) -> str:
    # The host keeps two helpdesks synced into one dataset apart.
    return f"{ID_PREFIX}{host}:{kind}:{item_id}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _ticket_row(host: str, ticket: dict, conversations: list[dict]) -> dict:
    subject = ticket.get("subject") or "(no subject)"
    lines = [f"Freshdesk ticket #{ticket['id']}: {subject}"]
    facts = [
        f"Status: {_label(TICKET_STATUSES, ticket.get('status'))}",
        f"Priority: {_label(TICKET_PRIORITIES, ticket.get('priority'))}",
        f"Source: {_label(TICKET_SOURCES, ticket.get('source'))}",
    ]
    if ticket.get("type"):
        facts.append(f"Type: {ticket['type']}")
    lines.append(". ".join(facts))
    requester = _person(ticket.get("requester") or {})
    if requester:
        lines.append(f"Requester: {requester}")
    if ticket.get("tags"):
        lines.append("Tags: " + ", ".join(ticket["tags"]))
    lines.append(f"Created: {ticket.get('created_at')}. Updated: {ticket.get('updated_at')}")
    content = "\n".join(lines)
    description = (ticket.get("description_text") or "").strip()
    if description:
        content += f"\n\n{description}"
    for conversation in sorted(conversations, key=lambda c: c.get("created_at") or ""):
        body = (conversation.get("body_text") or "").strip()
        if not body:
            continue
        if conversation.get("private"):
            who = "Private note"
        elif conversation.get("incoming"):
            who = "Customer message"
        else:
            who = "Agent reply"
        content += f"\n\n{who}, {conversation.get('created_at')}:\n{body}"
    return {
        "id": _row_id(host, "ticket", ticket["id"]),
        "title": subject,
        "content": content,
        "url": f"https://{host}/a/tickets/{ticket['id']}",
        "_deleted": False,
    }


def _article_row(host: str, article: dict) -> dict:
    title = article.get("title") or "(untitled)"
    lines = [f"Freshdesk solution article: {title}"]
    path = [level.get("data", {}).get("name") for level in article.get("hierarchy") or []]
    if any(path):
        lines.append("Location: " + " > ".join(p for p in path if p))
    if article.get("tags"):
        lines.append("Tags: " + ", ".join(article["tags"]))
    lines.append(f"Updated: {article.get('updated_at')}")
    content = "\n".join(lines)
    text = (article.get("description_text") or "").strip()
    if text:
        content += f"\n\n{text}"
    return {
        "id": _row_id(host, "article", article["id"]),
        "title": title,
        "content": content,
        "url": f"https://{host}/support/solutions/articles/{article['id']}",
        "_deleted": False,
    }


def _label(names: dict[int, str], value) -> str:
    # Accounts can add custom statuses; show their number rather than guess.
    return names.get(value, str(value))


def _person(person: dict) -> str:
    # Name only: the graph doesn't need customer emails.  A contact without a
    # name can have its email as the name, so that is dropped too.
    name = person.get("name") or ""
    return "" if "@" in name else name


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def freshdesk_source(
    domain: str | None = None,
    api_key: str | None = None,
    *,
    include_tickets: bool = True,
    include_conversations: bool = True,
    include_private_notes: bool = False,
    include_articles: bool = True,
    resource_name: str = "freshdesk",
    check_active: Callable[[], None] | None = None,
    client: FreshdeskClient | None = None,
):
    """Return a ``dlt`` resource yielding one row per new or changed ticket or article.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        domain: The helpdesk, like ``"acme"`` or ``"acme.freshdesk.com"``.
            Defaults to ``FRESHDESK_DOMAIN``.
        api_key: Profile settings > View API key.  Defaults to ``FRESHDESK_API_KEY``.
        include_tickets: Sync tickets.
        include_conversations: Add each ticket's replies to its document.
        include_private_notes: Also add private (agent-only) notes.  Off by
            default, so internal notes are not searchable unless you opt in.
        include_articles: Sync published solution articles.
        resource_name: Stable dlt resource name. Give each helpdesk its own name
            when several sync into one dataset, so their state stays separate.
        check_active: Optional host authorization checkpoint during extraction.
        client: A pre-built client, mainly for tests.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Freshdesk sync requires a Cognee build with table-scoped DLT document "
            "cleanup. Upgrade Cognee before syncing to avoid deleting another source's data."
        )
    if not (include_tickets or include_articles):
        # Nothing selected would forget every synced row, so refuse it.
        raise ValueError("Select something to sync: include_tickets or include_articles.")

    config = _Config(
        tickets=include_tickets,
        conversations=include_conversations,
        private_notes=include_private_notes,
        articles=include_articles,
    )
    if client is None:
        domain = domain or os.getenv("FRESHDESK_DOMAIN", "")
        api_key = api_key or os.getenv("FRESHDESK_API_KEY", "")
        if not (domain and api_key):
            # Fail when the source is built, not halfway through a pipeline run.
            raise ValueError(
                "Freshdesk credentials are missing. Set FRESHDESK_DOMAIN and FRESHDESK_API_KEY, "
                "or pass domain= and api_key=."
            )
        client = FreshdeskClient(domain, api_key)

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def freshdesk_items():
        yield from dlt_utils.guarded_rows(
            _iter_rows(client, config, dlt.current.resource_state(), stats),
            check_active,
        )

    resource = freshdesk_items()
    # Opt into the document ingestion path: each ticket and article becomes a
    # text document that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, "freshdesk")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never subjects or content.
    resource.cognee_sync_stats = stats
    return resource
