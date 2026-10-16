"""DLT source for Help Scout (incremental sync + forget-on-delete).

Pulls Help Scout **conversations with their threads** (Inbox API v2) and,
optionally, **Docs articles** (Docs API v1) into cognee as documents::

    import cognee
    from cognee_community_connector_help_scout import help_scout_source

    await cognee.remember(
        help_scout_source(),             # HELPSCOUT_APP_ID / HELPSCOUT_APP_SECRET from env
        dataset_name="help_scout",
        write_disposition="merge",       # REQUIRED (see .. important:: below)
    )

Rows are ingested as *normal documents*: the source declares
``cognee_document_source = "help_scout"``, so each conversation / article flows
through cognify entity extraction instead of the deterministic dlt-row path.

.. important::
   ``write_disposition="merge"`` is **mandatory**. Incremental runs only see what
   changed, so cognee's default ``"replace"`` would forget everything else.

Design
------
* **Auth (OAuth 2.0)** - Inbox API: an app id + secret exchanged for a bearer
  token with the Client Credentials grant (tokens live 2 days; a ``401`` triggers
  one automatic re-auth), or a ready ``access_token`` from an Authorization Code
  app. Docs API: a per-user Docs API key over HTTP Basic auth.
* **One document per conversation** (``id = "conversation:<id>"``): subject,
  inbox, status, tags, customer, assignee and the thread text oldest-first.
  Internal notes are off by default; system "line item" threads are skipped.
* **Incremental** - ``GET /v2/conversations?status=all&modifiedSince=<cursor>``
  sorted by ``modifiedAt``. The cursor is the UTC time the last run *started*,
  kept in dlt resource state and only advanced after a fully successful run.
* **Rate-limit friendly thread loading** - changed conversations are listed with
  ``embed=threads``, so one request returns up to 25 conversations *with* their
  threads. The per-conversation threads endpoint is only called when the
  embedded copy can be incomplete: chat conversations (Help Scout truncates
  embedded chat threads) or when fewer published threads came back than the
  conversation's ``threads`` count says.
* **Forget-on-delete** - conversations in ``state == "deleted"`` (or moved to
  spam, unless ``include_spam=True``) become ``{"id", "_deleted": True}``
  tombstones. Because a deleted conversation may also just vanish from the list,
  ``reconcile=True`` (default) also lists all conversation ids each run and
  tombstones the known ids that are gone. dlt removes tombstoned rows on
  ``merge`` and cognee's ``orphan_cleanup`` drops them from the graph.
* **Docs articles** (``id = "article:<id>"``) - collections -> article refs; an
  article's full text is fetched only when its ``updatedAt`` /
  ``lastPublishedAt`` / ``status`` marker changed. Articles that are deleted or
  unpublished drop out of the listing and are tombstoned.
* **Safe failure** - ``429`` (honouring ``X-RateLimit-Retry-After``), ``5xx`` and
  network errors are retried; anything else aborts the run, so a partial read
  never moves a cursor or causes a false deletion.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("help_scout_connector")

HELP_SCOUT_SOURCE_NAME = "help_scout"
CONVERSATIONS_TABLE = "help_scout_conversations"
ARTICLES_TABLE = "help_scout_articles"

MAILBOX_API = "https://api.helpscout.net/v2"
TOKEN_URL = f"{MAILBOX_API}/oauth2/token"
DOCS_API = "https://docsapi.helpscout.net/v1"

_MAX_RETRIES = 5
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_DELETED_COLUMN = {"_deleted": {"data_type": "bool", "hard_delete": True}}
_THREAD_LABELS = {
    "customer": "customer",
    "message": "reply",
    "note": "internal note",
    "chat": "chat",
    "beaconchat": "chat",
    "phone": "phone",
    "forwardparent": "forward",
    "forwardchild": "forward",
}

_EXTRA_HINT = (
    "The Help Scout connector requires dlt and httpx: "
    "pip install cognee-community-connector-help-scout"
)


@dataclass(frozen=True)
class _Options:
    mailbox_ids: tuple[int, ...]
    include_notes: bool
    include_spam: bool
    reconcile: bool
    modified_since: str | None
    collection_ids: frozenset[str]
    article_status: str


def help_scout_source(
    app_id: str | None = None,
    app_secret: str | None = None,
    access_token: str | None = None,
    mailbox_ids: Iterable[int] | None = None,
    include_conversations: bool = True,
    include_notes: bool = False,
    include_spam: bool = False,
    reconcile: bool = True,
    modified_since: str | None = None,
    docs_api_key: str | None = None,
    include_articles: bool | None = None,
    collection_ids: Iterable[str] | None = None,
    article_status: str = "published",
    client: Any = None,
):
    """Create a dlt source with Help Scout conversations and/or Docs articles.

    Args:
        app_id / app_secret: OAuth app credentials (Client Credentials grant).
            Fall back to ``HELPSCOUT_APP_ID`` / ``HELPSCOUT_APP_SECRET``.
        access_token: A ready OAuth bearer token (e.g. from an Authorization Code
            app). Falls back to ``HELPSCOUT_ACCESS_TOKEN``. Used as-is; it cannot be
            renewed, so prefer app credentials for scheduled syncs.
        mailbox_ids: Only these inboxes. Default: every inbox the token can see.
        include_conversations: Sync conversations (default ``True``).
        include_notes: Include internal notes in conversation documents.
        include_spam: Keep spam conversations (default: they are forgotten).
        reconcile: List all conversation ids each run to catch deletions that do
            not show up as a change. Costs about one request per 25 conversations.
        modified_since: ISO 8601 time; limits only the *first* run.
        docs_api_key: Docs API key. Falls back to ``HELPSCOUT_DOCS_API_KEY``.
        include_articles: Sync Docs articles. Default: ``True`` when a Docs key is set.
        collection_ids: Only these Docs collections. Default: all.
        article_status: ``"published"`` (default) or ``"all"``.
        client: ``httpx.Client``-like object (test-injection point) with
            ``get(url, params=, headers=)`` and ``post(url, data=)``.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    app_id = app_id or os.environ.get("HELPSCOUT_APP_ID")
    app_secret = app_secret or os.environ.get("HELPSCOUT_APP_SECRET")
    access_token = access_token or os.environ.get("HELPSCOUT_ACCESS_TOKEN")
    docs_api_key = docs_api_key or os.environ.get("HELPSCOUT_DOCS_API_KEY")
    if include_articles is None:
        include_articles = bool(docs_api_key)

    if include_conversations and not (access_token or (app_id and app_secret)):
        raise ValueError(
            "Help Scout credentials required: pass app_id= and app_secret= (or set "
            "HELPSCOUT_APP_ID / HELPSCOUT_APP_SECRET), or pass access_token=."
        )
    if include_articles and not docs_api_key:
        raise ValueError(
            "Docs articles need a Docs API key: docs_api_key= or HELPSCOUT_DOCS_API_KEY."
        )
    if not (include_conversations or include_articles):
        raise ValueError("Nothing to ingest: enable conversations and/or articles.")

    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc
        client = httpx.Client(timeout=60.0)

    options = _Options(
        mailbox_ids=tuple(mailbox_ids or ()),
        include_notes=include_notes,
        include_spam=include_spam,
        reconcile=reconcile,
        modified_since=modified_since,
        collection_ids=frozenset(collection_ids or ()),
        article_status=article_status,
    )

    resources = []
    if include_conversations:
        inbox_api = _Api(client, _OAuthToken(client, app_id, app_secret, access_token))

        @dlt.resource(
            name=CONVERSATIONS_TABLE,
            primary_key="id",
            write_disposition="merge",
            columns=_DELETED_COLUMN,
        )
        def help_scout_conversations():
            yield from _sync_conversations(inbox_api, dlt.current.resource_state(), options)

        resources.append(help_scout_conversations)

    if include_articles:
        docs_api = _Api(client, _DocsKey(docs_api_key))

        @dlt.resource(
            name=ARTICLES_TABLE,
            primary_key="id",
            write_disposition="merge",
            columns=_DELETED_COLUMN,
        )
        def help_scout_articles():
            yield from _sync_articles(docs_api, dlt.current.resource_state(), options)

        resources.append(help_scout_articles)

    @dlt.source(name=HELP_SCOUT_SOURCE_NAME)
    def _help_scout():
        return resources

    source = _help_scout()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, HELP_SCOUT_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# HTTP: auth + retries
# ---------------------------------------------------------------------------


class _OAuthToken:
    """Bearer token for the Inbox API (Client Credentials, renewed on 401)."""

    def __init__(self, http: Any, app_id: str | None, app_secret: str | None, token: str | None):
        self._http = http
        self._app_id = app_id
        self._app_secret = app_secret
        self._token = token

    def headers(self) -> dict:
        if self._token is None:
            self.renew()
        return {"Authorization": f"Bearer {self._token}"}

    def can_renew(self) -> bool:
        return bool(self._app_id and self._app_secret)

    def renew(self) -> None:
        if not self.can_renew():
            raise PermissionError(
                "Help Scout rejected the access token (HTTP 401) and no app id/secret "
                "was given to request a new one."
            )
        data = {
            "grant_type": "client_credentials",
            "client_id": self._app_id,
            "client_secret": self._app_secret,
        }
        response = _with_retries(lambda: self._http.post(TOKEN_URL, data=data))
        if response.status_code in (400, 401, 403):
            raise PermissionError(
                f"Help Scout rejected the app id/secret (HTTP {response.status_code})."
            )
        response.raise_for_status()
        self._token = response.json()["access_token"]
        logger.info("Help Scout: obtained a new access token.")


class _DocsKey:
    """HTTP Basic auth for the Docs API: the key is the user name, password ``X``."""

    def __init__(self, key: str):
        encoded = base64.b64encode(f"{key}:X".encode()).decode()
        self._headers = {"Authorization": f"Basic {encoded}"}

    def headers(self) -> dict:
        return self._headers

    def can_renew(self) -> bool:
        return False

    def renew(self) -> None:  # pragma: no cover - never called (can_renew is False)
        raise PermissionError("Docs API key rejected.")


class _Api:
    """GET JSON with auth, one re-auth on 401, and transient-error retries."""

    def __init__(self, http: Any, auth: Any):
        self._http = http
        self._auth = auth

    def get(self, url: str, params: dict | None = None) -> dict:
        renewed = False
        while True:
            response = _with_retries(
                lambda: self._http.get(url, params=params, headers=self._auth.headers())
            )
            status = response.status_code
            if status == 401 and not renewed and self._auth.can_renew():
                self._auth.renew()
                renewed = True
                continue
            if status in (401, 403):
                raise PermissionError(
                    f"Help Scout refused the request (HTTP {status}) for {url}. "
                    "Check the credentials and the app's permissions."
                )
            response.raise_for_status()
            return response.json()


def _with_retries(send) -> Any:
    """Call ``send()`` and retry 429/5xx/network errors with backoff.

    Returns the last response (possibly an error) once retries are exhausted so
    the caller raises a meaningful HTTP error.
    """
    import httpx

    for attempt in range(_MAX_RETRIES):
        try:
            response = send()
        except httpx.TransportError as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = float(2**attempt)
            logger.warning("Help Scout: %s - retrying in %.1fs.", exc, delay)
            time.sleep(delay)
            continue
        if response.status_code in _TRANSIENT_STATUS and attempt < _MAX_RETRIES - 1:
            delay = _retry_after(response.headers, attempt)
            logger.warning(
                "Help Scout: HTTP %s - retrying in %.1fs (%d/%d).",
                response.status_code,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue
        return response
    raise RuntimeError("Help Scout: retry loop exited unexpectedly.")  # pragma: no cover


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait: Help Scout's ``X-RateLimit-Retry-After``, else backoff."""
    for name in ("X-RateLimit-Retry-After", "x-ratelimit-retry-after", "Retry-After"):
        value = headers.get(name) if headers else None
        if value is not None:
            try:
                return max(float(value), 0.0)
            except (TypeError, ValueError):
                break
    return float(2**attempt)


def _utc_now_iso() -> str:
    # Second precision, as Help Scout's examples use; truncating moves the cursor
    # slightly earlier, which can only re-read a change, never skip one.
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _iter_hal(api: _Api, url: str, params: dict | None, key: str) -> Iterator[dict]:
    """Yield ``_embedded[key]`` items, following HAL ``_links.next`` links."""
    seen: set[str] = set()
    next_url, next_params = url, params
    while True:
        payload = api.get(next_url, next_params)
        yield from (payload.get("_embedded") or {}).get(key) or []
        href = ((payload.get("_links") or {}).get("next") or {}).get("href")
        # Stop on the last page, or if a next link repeats (never loop forever).
        if not href or href in seen:
            return
        seen.add(href)
        next_url, next_params = href, None


def _iter_docs(api: _Api, url: str, key: str, params: dict) -> Iterator[dict]:
    """Yield items of a Docs API list (``{key: {page, pages, items}}``)."""
    page = 1
    while True:
        block = api.get(url, {**params, "page": page}).get(key) or {}
        items = block.get("items") or []
        yield from items
        if not items or page >= int(block.get("pages") or 1):
            return
        page += 1


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def _sync_conversations(api: _Api, state: dict, options: _Options) -> Iterator[dict]:
    cursor = state.get("modified_since") or options.modified_since
    full_listing = state.get("modified_since") is None and options.modified_since is None
    known = set(state.get("known_ids") or [])
    run_started = _utc_now_iso()

    url = f"{MAILBOX_API}/conversations"
    base: dict[str, Any] = {"status": "all"}
    if options.mailbox_ids:
        base["mailbox"] = ",".join(str(m) for m in options.mailbox_ids)

    inbox_names = _inbox_names(api)
    changed_params = {**base, "embed": "threads", "sortField": "modifiedAt", "sortOrder": "asc"}
    if cursor:
        changed_params["modifiedSince"] = cursor

    emitted: set[int] = set()
    gone: set[int] = set()
    thread_calls = 0
    for conversation in _iter_hal(api, url, changed_params, "conversations"):
        conversation_id = conversation.get("id")
        if conversation_id is None:
            continue
        if _excluded(conversation, options):
            if conversation_id in known:
                gone.add(conversation_id)
                yield _tombstone(f"conversation:{conversation_id}")
            continue
        threads, fetched = _threads_for(api, conversation)
        thread_calls += fetched
        emitted.add(conversation_id)
        yield _conversation_row(conversation, threads, options, inbox_names)

    if full_listing:
        present: set[int] | None = set(emitted)
    elif options.reconcile:
        present = {
            c["id"]
            for c in _iter_hal(api, url, dict(base), "conversations")
            if c.get("id") is not None and not _excluded(c, options)
        }
    else:
        present = None

    if present is not None:
        # A conversation seen alive in this run is never tombstoned in the same
        # load; if it vanished meanwhile, the next run removes it.
        for conversation_id in sorted(known - present - gone - emitted):
            gone.add(conversation_id)
            yield _tombstone(f"conversation:{conversation_id}")
        state["known_ids"] = sorted(present | emitted)
    else:
        state["known_ids"] = sorted((known | emitted) - gone)

    state["modified_since"] = run_started
    logger.info(
        "Help Scout: %d conversation(s) synced (%d extra thread request(s)), %d forgotten.",
        len(emitted),
        thread_calls,
        len(gone),
    )


def _excluded(conversation: dict, options: _Options) -> bool:
    if conversation.get("state") in ("deleted", "draft"):
        return True
    return conversation.get("status") == "spam" and not options.include_spam


def _inbox_names(api: _Api) -> dict[int, str]:
    return {
        m["id"]: m.get("name") or str(m["id"])
        for m in _iter_hal(api, f"{MAILBOX_API}/mailboxes", None, "mailboxes")
        if m.get("id") is not None
    }


def _threads_for(api: _Api, conversation: dict) -> tuple[list[dict], int]:
    """Return the conversation's threads and how many extra requests it took."""
    embedded = (conversation.get("_embedded") or {}).get("threads")
    if embedded is not None and not _needs_full_threads(conversation, embedded):
        return embedded, 0
    url = f"{MAILBOX_API}/conversations/{conversation['id']}/threads"
    return list(_iter_hal(api, url, None, "threads")), 1


def _needs_full_threads(conversation: dict, threads: list[dict]) -> bool:
    """True when the embedded thread list may be incomplete.

    Help Scout truncates chat threads embedded in list responses, and the
    conversation's ``threads`` field counts its published non-note threads, so a
    shorter embedded list means something is missing.
    """
    if conversation.get("type") == "chat":
        return True
    if any(t.get("type") in ("chat", "beaconchat") for t in threads):
        return True
    published = [
        t for t in threads if t.get("type") != "note" and t.get("state", "published") == "published"
    ]
    expected = conversation.get("threads")
    return isinstance(expected, int) and len(published) < expected


def _conversation_row(
    conversation: dict, threads: list[dict], options: _Options, inbox_names: dict[int, str]
) -> dict:
    """Flatten a conversation and its threads into a document row.

    Volatile fields (preview, waiting-since, read state) are left out so they do
    not churn the content-hash ``data_id``.
    """
    number = conversation.get("number")
    subject = (conversation.get("subject") or "").strip()
    header = []
    for label, value in (
        ("Inbox", inbox_names.get(conversation.get("mailboxId"))),
        ("Status", conversation.get("status")),
        ("Channel", conversation.get("type")),
        ("Customer", _person(conversation.get("primaryCustomer"))),
        ("Assignee", _person(conversation.get("assignee"))),
        (
            "Tags",
            ", ".join(t.get("tag", "") for t in conversation.get("tags") or [] if t.get("tag")),
        ),
        ("Created", conversation.get("createdAt")),
        ("Closed", conversation.get("closedAt")),
    ):
        if value:
            header.append(f"{label}: {value}")

    lines = []
    for thread in sorted(threads, key=lambda t: (t.get("createdAt") or "", t.get("id") or 0)):
        thread_type = thread.get("type")
        if thread_type == "lineitem":
            continue
        if thread_type == "note" and not options.include_notes:
            continue
        if thread.get("state", "published") != "published":
            continue
        text = _html_to_text(thread.get("body"))
        if not text:
            continue
        who = _person(thread.get("createdBy")) or thread_type or "unknown"
        label = _THREAD_LABELS.get(thread_type, thread_type or "message")
        lines.append(f"[{label}] {who} ({thread.get('createdAt') or 'unknown time'}): {text}")

    parts = ["\n".join(header)]
    if lines:
        parts.append("Thread:\n" + "\n\n".join(lines))
    web = ((conversation.get("_links") or {}).get("web") or {}).get("href")
    return {
        "id": f"conversation:{conversation['id']}",
        "title": f"#{number} {subject}".strip() if number is not None else subject,
        "content": "\n\n".join(p for p in parts if p),
        "url": web or None,
        "_deleted": False,
    }


def _person(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    name = " ".join(p for p in (value.get("first"), value.get("last")) if p).strip()
    return name or value.get("name") or ""


# ---------------------------------------------------------------------------
# Docs articles
# ---------------------------------------------------------------------------


def _sync_articles(api: _Api, state: dict, options: _Options) -> Iterator[dict]:
    known: dict[str, str] = dict(state.get("versions") or {})
    present: dict[str, str] = {}
    fetched = 0

    for collection in _iter_docs(api, f"{DOCS_API}/collections", "collections", {}):
        collection_id = collection.get("id")
        if not collection_id or (
            options.collection_ids and collection_id not in options.collection_ids
        ):
            continue
        url = f"{DOCS_API}/collections/{collection_id}/articles"
        params = {"status": options.article_status, "pageSize": 100}
        for ref in _iter_docs(api, url, "articles", params):
            article_id = ref.get("id")
            if not article_id:
                continue
            marker = f"{ref.get('updatedAt')}|{ref.get('lastPublishedAt')}|{ref.get('status')}"
            present[article_id] = marker
            if known.get(article_id) == marker:
                continue
            article = api.get(f"{DOCS_API}/articles/{article_id}").get("article") or {}
            fetched += 1
            yield _article_row(article or ref, collection)

    for article_id in sorted(set(known) - set(present)):
        yield _tombstone(f"article:{article_id}")

    state["versions"] = present
    logger.info(
        "Help Scout Docs: %d article(s) fetched, %d forgotten.",
        fetched,
        len(set(known) - set(present)),
    )


def _article_row(article: dict, collection: dict) -> dict:
    header = [f"Collection: {collection.get('name')}"] if collection.get("name") else []
    if article.get("publicUrl"):
        header.append(f"URL: {article['publicUrl']}")
    body = _html_to_text(article.get("text"))
    return {
        "id": f"article:{article.get('id')}",
        "title": (article.get("name") or "").strip(),
        "content": "\n\n".join(p for p in ("\n".join(header), body) if p),
        "url": article.get("publicUrl") or None,
        "_deleted": False,
    }


def _tombstone(row_id: str) -> dict:
    return {"id": row_id, "_deleted": True}


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(self._skip - 1, 0)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(value: Any) -> str:
    """Thread bodies and articles are HTML; keep the text and line breaks."""
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(str(value))
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line).strip()
