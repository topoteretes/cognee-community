"""Zendesk connector for cognee: a ``dlt`` source that turns support tickets and
Help Center articles into memory.

Hand the source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_zendesk import zendesk_source

    await cognee.remember(
        zendesk_source(subdomain="acme"),   # credentials from the environment
        dataset_name="zendesk",
        primary_key="id",
        write_disposition="merge",   # REQUIRED, see below
        max_rows_per_table=0,        # compare forget-on-delete against every row
    )

Design
------
* **Auth**: either an OAuth access token (``ZENDESK_OAUTH_TOKEN``, sent as
  ``Bearer``) or email + API token (``ZENDESK_EMAIL`` + ``ZENDESK_API_TOKEN``,
  Basic ``email/token:api_token``). Zendesk is retiring API tokens (accounts
  created after 2026-07-28 cannot create them, creation stops 2026-10-27 and
  existing tokens stop working 2027-04-30), so new setups should use OAuth.
  Credentials are only sent to ``https://<subdomain>.zendesk.com`` and never logged.
* **Selection**: ``resources=("tickets", "articles")`` picks what to ingest;
  ``include_internal_notes=False`` keeps private agent notes out by default.
* **Tickets** use the cursor-based *incremental ticket export*
  (``/api/v2/incremental/tickets/cursor.json``), not search, so older tickets
  are never dropped. The ``after_cursor`` lives in dlt resource state, which
  dlt only commits after a successful load. A ticket comes back in the export
  whenever it changes, *including when only a comment was added* (tested), so
  comments need no polling of their own: each changed ticket is rendered as one
  document with its comment thread.
* **Articles** are listed in full each run (the Help Center has no delete feed).
  Only published articles whose ``updated_at`` changed since the last run are
  re-emitted; drafts are skipped.
* **Forget-on-delete**: deleted tickets come through the export with
  ``status="deleted"`` and are emitted as ``_deleted`` hard-delete rows. An
  article that disappears from a *complete* listing (archived or deleted) or
  turns back into a draft is emitted the same way. cognee's ``orphan_cleanup``
  then forgets the rows. A failed or partial listing raises instead of deleting.

.. important::
   ``write_disposition="merge"`` is mandatory, for both tables. cognee passes
   one ``write_disposition`` to ``pipeline.run()`` for the whole source, and the
   default ``"replace"`` would wipe every ticket from earlier syncs, because the
   incremental export only returns what changed.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

try:  # cognee >= 1.6: per-dataset, per-account dlt state (see _scope_state below)
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR
except ImportError:  # cognee 1.4: one shared pipeline state
    PIPELINE_SCOPE_ATTR = None

logger = get_logger("zendesk_connector")

ZENDESK_SOURCE_NAME = "zendesk"
TICKETS_TABLE = "zendesk_tickets"
ARTICLES_TABLE = "zendesk_articles"
_RESOURCES = ("tickets", "articles")
_MAX_RETRIES = 5
_COLUMNS = {
    "_deleted": {"data_type": "bool", "hard_delete": True},
    "title": {"data_type": "text", "nullable": True},
    "content": {"data_type": "text", "nullable": True},
    "url": {"data_type": "text", "nullable": True},
}


class ZendeskAPIError(RuntimeError):
    """A Zendesk API call failed permanently."""


# ---------------------------------------------------------------------------
# HTTP client (stdlib only)
# ---------------------------------------------------------------------------
class ZendeskClient:
    """Minimal Zendesk REST client: ``get(path_or_url, **params) -> dict``.

    Retries 429 (honouring ``Retry-After``; the incremental export allows only
    10 requests a minute) and 5xx/network errors; raises :class:`ZendeskAPIError`
    otherwise. Absolute ``next_page`` URLs are only followed on the same host.
    """

    def __init__(
        self,
        subdomain: str,
        *,
        oauth_token: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        timeout: float = 60.0,
    ):
        self.base = f"https://{subdomain}.zendesk.com"
        if oauth_token:
            self._auth = f"Bearer {oauth_token}"
        elif email and api_token:
            raw = f"{email}/token:{api_token}".encode()
            self._auth = "Basic " + base64.b64encode(raw).decode()
        else:
            raise ValueError(
                "Zendesk credentials required: an OAuth access token (ZENDESK_OAUTH_TOKEN) "
                "or email + API token (ZENDESK_EMAIL + ZENDESK_API_TOKEN)."
            )
        self._timeout = timeout

    def _url(self, path_or_url: str, params: dict) -> str:
        if path_or_url.startswith("http"):
            if urllib.parse.urlparse(path_or_url).netloc != urllib.parse.urlparse(self.base).netloc:
                raise ZendeskAPIError("Refusing to follow a pagination link to another host.")
            url = path_or_url
        else:
            url = self.base + path_or_url
        if params:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return url

    def get(self, path_or_url: str, **params: Any) -> dict:
        url = self._url(path_or_url, params)
        for attempt in range(_MAX_RETRIES):
            request = urllib.request.Request(
                url, headers={"Authorization": self._auth, "Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise ZendeskAPIError(
                        f"Zendesk rejected the credentials (HTTP {exc.code}). OAuth access "
                        "tokens expire; mint a fresh one, and check the token can read "
                        "tickets / Help Center."
                    ) from None
                if exc.code == 404:
                    raise ZendeskAPIError(
                        f"Zendesk returned 404 for {url.split('?')[0]}."
                    ) from None
                if (exc.code == 429 or exc.code >= 500) and attempt < _MAX_RETRIES - 1:
                    delay = _retry_after(exc.headers, attempt)
                    logger.warning(
                        "Zendesk: HTTP %s, retrying in %.0fs (%d/%d).",
                        exc.code,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                raise ZendeskAPIError(f"Zendesk request failed: HTTP {exc.code}.") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(float(2**attempt))
                    continue
                raise ZendeskAPIError(f"Zendesk request failed: {exc}") from None
        raise ZendeskAPIError("Zendesk request failed after retries.")


def _retry_after(headers, attempt: int) -> float:
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(min(60, 2 ** (attempt + 2)))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    _BREAKS = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre"})

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BREAKS:
            self.parts.append("\n")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f"[image: {alt}]")

    def handle_endtag(self, tag):
        if tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """Article bodies are HTML; keep the text and paragraph breaks."""
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def _user_label(users: dict, user_id) -> str:
    user = users.get(user_id) or {}
    name, email = user.get("name"), user.get("email")
    if name and email:
        return f"{name} <{email}>"
    return name or email or (f"user {user_id}" if user_id else "unknown")


def ticket_to_row(
    ticket: dict,
    comments: list[dict],
    users: dict,
    subdomain: str,
    include_internal: bool,
) -> dict[str, Any]:
    """Render a ticket and its comment thread as one deterministic document row.

    ``updated_at`` is left out on purpose: Zendesk bumps it for metadata-only
    changes (SLA timers, triggers), which would otherwise re-cognify an
    unchanged conversation.
    """
    header = [
        f"Ticket #{ticket['id']}: {ticket.get('subject') or '(no subject)'}",
        f"Status: {ticket.get('status')}",
    ]
    for label, key in (("Priority", "priority"), ("Type", "type")):
        if ticket.get(key):
            header.append(f"{label}: {ticket[key]}")
    if ticket.get("tags"):
        header.append("Tags: " + ", ".join(sorted(ticket["tags"])))
    header.append(f"Requester: {_user_label(users, ticket.get('requester_id'))}")
    if ticket.get("assignee_id"):
        header.append(f"Assignee: {_user_label(users, ticket.get('assignee_id'))}")
    header.append(f"Created: {ticket.get('created_at')}")

    thread = []
    for comment in comments:
        public = comment.get("public", True)
        if not public and not include_internal:
            continue
        body = (comment.get("plain_body") or comment.get("body") or "").strip()
        if not body:
            continue
        who = _user_label(users, comment.get("author_id"))
        kind = "" if public else " [internal note]"
        thread.append(f"--- {who}, {comment.get('created_at')}{kind}\n{body}")

    content = "\n".join(header) + ("\n\n" + "\n\n".join(thread) if thread else "")
    return {
        "id": ticket["id"],
        "title": f"Ticket #{ticket['id']}: {ticket.get('subject') or '(no subject)'}",
        "content": content,
        "url": f"https://{subdomain}.zendesk.com/agent/tickets/{ticket['id']}",
        "status": ticket.get("status"),
        "_deleted": False,
    }


def article_to_row(article: dict) -> dict[str, Any]:
    body = html_to_text(article.get("body"))
    labels = article.get("label_names") or []
    content = body + (f"\n\nLabels: {', '.join(sorted(labels))}" if labels else "")
    return {
        "id": article["id"],
        "title": article.get("title") or f"Article {article['id']}",
        "content": content,
        "url": article.get("html_url"),
        "locale": article.get("locale"),
        "_deleted": False,
    }


def deleted_row(row_id) -> dict[str, Any]:
    return {"id": row_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _paginate(client, path: str, key: str, **params) -> Iterator[dict]:
    """Follow ``next_page`` links (offset or cursor pagination) to the end."""
    data = client.get(path, **params)
    while True:
        yield from data.get(key) or []
        nxt = data.get("next_page") or (data.get("links") or {}).get("next")
        has_more = (data.get("meta") or {}).get("has_more", bool(nxt))
        if not nxt or not has_more:
            return
        data = client.get(nxt)


def _ticket_comments(client, ticket_id) -> tuple[list[dict], dict]:
    comments, users = [], {}
    data = client.get(f"/api/v2/tickets/{ticket_id}/comments.json", include="users")
    while True:
        comments.extend(data.get("comments") or [])
        for user in data.get("users") or []:
            users[user["id"]] = user
        nxt = data.get("next_page")
        if not nxt:
            return comments, users
        data = client.get(nxt)


def sync_tickets(
    client,
    state: dict,
    subdomain: str,
    include_internal: bool = False,
    max_pages: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed tickets (with comments) and tombstones for deleted tickets.

    The first run starts the export at ``start_time=0``; later runs resume from
    ``state['after_cursor']``. dlt persists the cursor only after the load
    succeeds, so a failed run reads the same window again.
    """
    cursor = state.get("after_cursor")
    params = {"cursor": cursor} if cursor else {"start_time": 0}
    pages = 0
    # The export lists a ticket once per change, so it can appear several times in
    # one window (edited, then deleted, then restored). Keep only its newest
    # version, so the last change wins and comments are fetched once per ticket.
    latest: dict = {}
    while True:
        data = client.get("/api/v2/incremental/tickets/cursor.json", **params)
        for ticket in data.get("tickets") or []:
            latest.pop(ticket["id"], None)
            latest[ticket["id"]] = ticket
        pages += 1
        if data.get("after_cursor"):
            state["after_cursor"] = data["after_cursor"]
        if data.get("end_of_stream") or not data.get("after_cursor"):
            break
        if max_pages is not None and pages >= max_pages:
            logger.info("Zendesk: stopping after %d export page(s); next run continues.", pages)
            break
        params = {"cursor": data["after_cursor"]}

    changed = deleted = 0
    for tid, ticket in latest.items():
        if ticket.get("status") == "deleted":
            deleted += 1
            yield deleted_row(tid)
            continue
        comments, users = _ticket_comments(client, tid)
        changed += 1
        yield ticket_to_row(ticket, comments, users, subdomain, include_internal)
    logger.info("Zendesk: %d changed ticket(s), %d deleted.", changed, deleted)


def sync_articles(client, state: dict, locale: str | None = None) -> Iterator[dict[str, Any]]:
    """Yield new/edited published articles and tombstones for removed ones.

    ``state['versions']`` maps article id -> ``updated_at`` of the version stored.
    The listing must complete before anything is tombstoned: an API error
    raises, so a failed run can never forget live articles.
    """
    path = (
        f"/api/v2/help_center/{locale}/articles.json"
        if locale
        else "/api/v2/help_center/articles.json"
    )
    listed = list(_paginate(client, path, "articles", per_page=100, sort_by="updated_at"))
    versions: dict[str, str] = state.setdefault("versions", {})
    live: set[str] = set()
    changed = 0
    for article in listed:
        if article.get("draft"):
            continue
        key = str(article["id"])
        live.add(key)
        if versions.get(key) != article.get("updated_at"):
            versions[key] = article.get("updated_at")
            changed += 1
            yield article_to_row(article)
    removed = [key for key in versions if key not in live]
    for key in removed:
        versions.pop(key)
        yield deleted_row(int(key))
    logger.info("Zendesk: %d new/edited article(s), %d removed.", changed, len(removed))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def zendesk_source(
    subdomain: str | None = None,
    *,
    oauth_token: str | None = None,
    email: str | None = None,
    api_token: str | None = None,
    resources: tuple[str, ...] | list[str] = _RESOURCES,
    include_internal_notes: bool = False,
    locale: str | None = None,
    max_export_pages_per_run: int | None = None,
    client: Any = None,
):
    """Create a dlt source with ``zendesk_tickets`` and/or ``zendesk_articles``.

    Args:
        subdomain: The ``<subdomain>`` of ``<subdomain>.zendesk.com``
            (falls back to ``ZENDESK_SUBDOMAIN``).
        oauth_token: OAuth access token (falls back to ``ZENDESK_OAUTH_TOKEN``).
        email / api_token: Legacy API-token auth (``ZENDESK_EMAIL`` /
            ``ZENDESK_API_TOKEN``), used when no OAuth token is given.
        resources: Any of ``"tickets"`` and ``"articles"``.
        include_internal_notes: Also ingest private agent notes (off by default;
            they often hold internal or sensitive information).
        locale: Help Center locale to list articles for (account default when None).
        max_export_pages_per_run: Cap the ticket export pages per run (each page
            holds up to 1000 tickets) for a large first backfill; the next run
            continues from the saved cursor.
        client: Object with ``get(path_or_url, **params)``; a test injection point.

    Returns:
        A dlt source. Every resource uses ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Zendesk connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    unknown = set(resources) - set(_RESOURCES)
    if unknown:
        raise ValueError(f"Unknown Zendesk resources {sorted(unknown)}; choose from {_RESOURCES}.")
    subdomain = subdomain or os.environ.get("ZENDESK_SUBDOMAIN")
    if not subdomain:
        raise ValueError("Zendesk subdomain required: pass subdomain= or set ZENDESK_SUBDOMAIN.")
    if client is None:
        client = ZendeskClient(
            subdomain,
            oauth_token=oauth_token or os.environ.get("ZENDESK_OAUTH_TOKEN"),
            email=email or os.environ.get("ZENDESK_EMAIL"),
            api_token=api_token or os.environ.get("ZENDESK_API_TOKEN"),
        )

    @dlt.resource(
        name=TICKETS_TABLE,
        primary_key="id",
        write_disposition="merge",
        columns=_COLUMNS,
    )
    def zendesk_tickets():
        yield from sync_tickets(
            client,
            dlt.current.resource_state(),
            subdomain,
            include_internal=include_internal_notes,
            max_pages=max_export_pages_per_run,
        )

    @dlt.resource(
        name=ARTICLES_TABLE,
        primary_key="id",
        write_disposition="merge",
        columns=_COLUMNS,
    )
    def zendesk_articles():
        yield from sync_articles(client, dlt.current.resource_state(), locale=locale)

    selected = {"tickets": zendesk_tickets, "articles": zendesk_articles}

    @dlt.source(name=ZENDESK_SOURCE_NAME)
    def _zendesk():
        return [selected[name] for name in _RESOURCES if name in resources]

    source = _zendesk()
    # Tickets and articles are prose: route them through cognify (document mode).
    setattr(source, DOCUMENT_SOURCE_ATTR, ZENDESK_SOURCE_NAME)
    # Keep the export cursor per Zendesk account (and, in cognee, per dataset), so
    # syncing two accounts or datasets never resumes from the other's cursor.
    if PIPELINE_SCOPE_ATTR:
        setattr(source, PIPELINE_SCOPE_ATTR, f"zendesk:{subdomain}")
    return source
