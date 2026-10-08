"""MediaWiki connector for cognee, a ``dlt`` source that turns a wiki into memory.

Sync pages from any MediaWiki (Wikipedia, Fandom, an internal company wiki)
into cognee, incrementally and with forget-on-deletion::

    import cognee
    from cognee_community_connector_mediawiki import mediawiki_source

    await cognee.remember(
        mediawiki_source(
            "https://en.wikipedia.org/w/api.php",
            categories=["Nobel laureates in Physics"],
        ),
        dataset_name="wiki",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Transport**: the MediaWiki Action API (``api.php``) over plain ``httpx``
  (a core cognee dependency), so the connector adds no new dependency. Public
  wikis need no credentials; a private wiki takes a bot password
  (``Special:BotPasswords``) or an injected, already authenticated
  ``http_client``. Requests are sequential, carry a descriptive User-Agent and
  ``maxlag``, and are retried on maxlag, HTTP 429 and 5xx, as the API
  etiquette asks of bots.
* **Scope**: ``namespaces`` (numeric ids, default the main namespace),
  ``categories`` (direct members) and explicit ``titles`` (redirects are
  followed, so a selected page that is later renamed stays selected). The
  union of the three is synced. Redirect pages and code pages (CSS, JS, JSON,
  Lua modules) are never ingested.
* **One document per page**, keyed by the page id, not the title: a move keeps
  the page's identity, so a rename updates the same document instead of
  forgetting one and ingesting another. The text is rendered by the wiki
  itself, so templates, transclusions and parser functions are expanded and
  no wikitext is parsed here: through the TextExtracts extension when the wiki
  has it (Wikipedia and most large wikis), otherwise from ``action=parse``
  HTML reduced to text. Categories, the current revision and the latest
  edits (timestamp, author, summary) are part of the document; fields the
  wiki has hidden (revision deletion) are left out. Each page carries the
  ``mediawiki:<server>`` node set, so ``recall`` can be scoped to one wiki.
* **Incremental**: the first run captures the server time, then enumerates
  the scope. Later runs replay ``list=recentchanges`` (edits, page creations
  and log events) from that time minus an overlap window, because the API
  documents that changes can enter the feed slightly out of timestamp order.
  Only entries that can touch the synced set are kept (a synced or selected
  page, or a title in a selected namespace); every page such an entry names
  (its page id, its title, a move's target, a merge's destination) is
  re-checked against the live wiki in batches of 50 and re-rendered only when
  its revision or title changed, so replaying an entry twice is harmless and
  the overlap needs no bookkeeping. Selected categories have their member
  lists diffed every run instead, which also catches membership that changed
  through a template.
* **Forget-on-delete**: a page the re-check reports missing (deleted), or that
  left the scope (moved out of a namespace, removed from a category, turned
  into a redirect), is emitted as a ``_deleted`` tombstone. dlt drops it on
  ``merge`` and cognee's ``orphan_cleanup`` removes it from the graph, vector
  and relational stores.
* **Reconciliation**: ``recentchanges`` is pruned (``$wgRCMaxAge``, 90 days by
  default, 30 on Wikimedia wikis). When the oldest retained entry is newer
  than the replay window, entries may be gone, so the run falls back to a full
  enumeration diffed against the pages synced before. The same happens when
  the scope or rendering settings change, every ``reconcile_after_days`` (a
  safety net for feed entries delayed beyond the overlap), and on
  ``full_sync=True``. An enumeration that comes
  back empty while pages were known deletes nothing, so a typo or a transient
  outage cannot wipe the dataset.

Every API failure raises before the run's state is saved, so dlt keeps the
previous cursor and a later run retries the same window.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("mediawiki_connector")

SOURCE_TAG = "mediawiki"
DEFAULT_USER_AGENT = (
    "cognee-community-connector-mediawiki/0.1.0 (+https://github.com/topoteretes/cognee-community)"
)

# MediaWiki accepts at most 50 page ids / titles per request for normal users.
_BATCH_SIZE = 50
_MAX_RETRIES = 5
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Content models that hold code or data rather than prose.
_NON_PROSE_CONTENT_MODELS = {"css", "sanitized-css", "javascript", "json", "Scribunto"}
_CONTENT_FORMATS = ("auto", "extracts", "parse")
_RETRYABLE_API_ERRORS = {"maxlag", "ratelimited"}
_GONE_API_ERRORS = {"missingtitle", "nosuchpageid"}


class MediaWikiAPIError(RuntimeError):
    """The wiki answered with an API error (``{"error": {...}}``) or bad HTTP."""

    def __init__(self, code: str, info: str = ""):
        super().__init__(f"MediaWiki API error {code}: {info}" if info else code)
        self.code = code
        self.info = info


class MediaWikiAuthError(MediaWikiAPIError):
    """Logging in with the bot password failed. Never carries the password."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class _MediaWikiClient:
    """Thin, sequential wrapper over ``api.php`` with retries and continuation."""

    def __init__(
        self,
        http_client: Any,
        api_url: str,
        user_agent: str,
        maxlag: int | None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = http_client
        self._api_url = api_url
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._maxlag = maxlag
        self._sleep = sleep
        self.requests = 0

    def get(self, params: dict[str, Any]) -> dict:
        return self._request("GET", params)

    def post(self, params: dict[str, Any]) -> dict:
        return self._request("POST", params)

    def _request(self, method: str, params: dict[str, Any]) -> dict:
        import httpx

        full = {"format": "json", "formatversion": "2", **params}
        if self._maxlag is not None:
            full["maxlag"] = str(self._maxlag)

        for attempt in range(_MAX_RETRIES):
            last_attempt = attempt == _MAX_RETRIES - 1
            self.requests += 1
            try:
                if method == "GET":
                    response = self._http.get(self._api_url, params=full, headers=self._headers)
                else:
                    response = self._http.post(self._api_url, data=full, headers=self._headers)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise
                self._backoff(f"network error ({type(exc).__name__})", None, attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if last_attempt:
                    raise MediaWikiAPIError(f"http_{response.status_code}")
                self._backoff(f"HTTP {response.status_code}", response.headers, attempt)
                continue
            if response.status_code >= 400:
                raise MediaWikiAPIError(f"http_{response.status_code}")

            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if error:
                code = str(error.get("code") or "unknown")
                if code in _RETRYABLE_API_ERRORS and not last_attempt:
                    self._backoff(code, response.headers, attempt)
                    continue
                raise MediaWikiAPIError(code, str(error.get("info") or ""))
            return payload

        raise MediaWikiAPIError("retries_exhausted")  # pragma: no cover - loop always returns

    def _backoff(self, reason: str, headers: Any, attempt: int) -> None:
        delay = _retry_after(headers, attempt)
        logger.warning(
            "MediaWiki: %s, retrying in %.1fs (%d/%d).", reason, delay, attempt + 1, _MAX_RETRIES
        )
        self._sleep(delay)

    def continued(self, params: dict[str, Any]) -> Iterator[dict]:
        """Yield every response of a query, following ``continue``."""
        cont: dict[str, Any] = {}
        while True:
            response = self.get({**params, **cont})
            yield response
            next_cont = response.get("continue")
            if not next_cont:
                return
            if next_cont == cont:
                raise MediaWikiAPIError("continuation_stalled", "continue did not advance")
            cont = next_cont

    def query_pages(self, params: dict[str, Any]) -> list[dict]:
        """Run a ``prop``/``generator`` query and merge pages split by continuation.

        A page can come back in several responses when one of its list props
        (categories, revisions) is continued; list values are concatenated and
        de-duplicated, scalar values overwritten.
        """
        merged: dict[Any, dict] = {}
        for response in self.continued({"action": "query", **params}):
            for page in (response.get("query") or {}).get("pages") or []:
                key = page.get("pageid") or ("title", page.get("title"))
                target = merged.setdefault(key, {})
                for name, value in page.items():
                    if isinstance(value, list):
                        existing = target.setdefault(name, [])
                        existing.extend(item for item in value if item not in existing)
                    else:
                        target[name] = value
        return list(merged.values())

    def login(self, username: str, password: str) -> None:
        """Log in with a bot password; the session cookie rides on the client."""
        tokens = self.get({"action": "query", "meta": "tokens", "type": "login"})
        token = ((tokens.get("query") or {}).get("tokens") or {}).get("logintoken")
        if not token:
            raise MediaWikiAuthError("login_token_missing")
        result = (
            self.post(
                {"action": "login", "lgname": username, "lgpassword": password, "lgtoken": token}
            ).get("login")
            or {}
        )
        if result.get("result") != "Success":
            raise MediaWikiAuthError("login_failed", str(result.get("reason") or result))


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") if headers is not None else None
    try:
        return max(float(header), 0.0)
    except (TypeError, ValueError):
        return float(2**attempt)


# ---------------------------------------------------------------------------
# Site and scope
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Site:
    server_time: str
    server_name: str
    has_text_extracts: bool


@dataclass(frozen=True)
class _Settings:
    namespaces: tuple[int, ...]
    categories: tuple[str, ...]
    titles: tuple[str, ...]
    content_format: str
    revision_history: int
    overlap_seconds: int
    reconcile_after_days: float | None
    full_sync: bool

    def scope_fingerprint(self) -> list:
        return [sorted(self.namespaces), sorted(self.categories), sorted(self.titles)]

    def render_fingerprint(self) -> list:
        return [self.content_format, self.revision_history]


@dataclass
class _Scope:
    """The scope resolved against the live wiki for one run."""

    namespaces: frozenset[int]
    category_titles: frozenset[str]
    title_page_ids: frozenset[int]

    def contains(self, page: dict) -> bool:
        if page.get("redirect") or page.get("contentmodel") in _NON_PROSE_CONTENT_MODELS:
            return False
        if page.get("ns") in self.namespaces or page.get("pageid") in self.title_page_ids:
            return True
        return any(
            category.get("title") in self.category_titles
            for category in page.get("categories") or []
        )


def _site_info(client: _MediaWikiClient) -> _Site:
    response = client.get(
        {
            "action": "query",
            "meta": "siteinfo",
            "siprop": "general|extensions",
            "curtimestamp": "1",
        }
    )
    query = response.get("query") or {}
    general = query.get("general") or {}
    extensions = {extension.get("name") for extension in query.get("extensions") or []}
    server_time = response.get("curtimestamp")
    if not server_time:
        raise MediaWikiAPIError("no_server_time", "siteinfo returned no curtimestamp")
    return _Site(
        server_time=server_time,
        server_name=general.get("servername") or urlparse(client._api_url).netloc,
        has_text_extracts="TextExtracts" in extensions,
    )


def _batched(values: Iterable[Any], size: int = _BATCH_SIZE) -> Iterator[list]:
    batch: list = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _resolve_category_titles(client: _MediaWikiClient, categories: Iterable[str]) -> set[str]:
    """Normalize category names to the wiki's own ``Category:Name`` titles.

    ``Category:`` is the canonical prefix every wiki accepts; the API answers
    with the localized title (``Kategorie:...``) that ``prop=categories`` uses.
    """
    titles = [
        name if name.lower().startswith("category:") else f"Category:{name}"
        for name in (category.strip() for category in categories)
        if name
    ]
    resolved: set[str] = set()
    for batch in _batched(titles):
        for page in client.query_pages({"titles": "|".join(batch)}):
            resolved.add(page["title"])
    return resolved


def _resolve_title_page_ids(client: _MediaWikiClient, titles: Iterable[str]) -> set[int]:
    """Resolve explicit titles to page ids, following redirects (renames)."""
    page_ids: set[int] = set()
    for batch in _batched(title.strip() for title in titles if title.strip()):
        for page in client.query_pages({"titles": "|".join(batch), "redirects": "1"}):
            if page.get("pageid") and not page.get("missing"):
                page_ids.add(page["pageid"])
    return page_ids


def _resolve_scope(client: _MediaWikiClient, settings: _Settings) -> _Scope:
    return _Scope(
        namespaces=frozenset(settings.namespaces),
        category_titles=frozenset(_resolve_category_titles(client, settings.categories)),
        title_page_ids=frozenset(_resolve_title_page_ids(client, settings.titles)),
    )


_INFO_PARAMS = {"prop": "info", "inprop": "url"}


def _ingestible(pages: Iterable[dict]) -> dict[int, dict]:
    """Key existing prose pages by id, dropping missing, redirect and code pages."""
    return {
        page["pageid"]: page
        for page in pages
        if page.get("pageid")
        and not page.get("missing")
        and not page.get("redirect")
        and page.get("contentmodel") not in _NON_PROSE_CONTENT_MODELS
    }


def _category_members(client: _MediaWikiClient, scope: _Scope) -> dict[int, dict]:
    """The current direct member pages of the selected categories."""
    members: dict[int, dict] = {}
    for category in sorted(scope.category_titles):
        members.update(
            _ingestible(
                client.query_pages(
                    {
                        "generator": "categorymembers",
                        "gcmtitle": category,
                        "gcmtype": "page",
                        "gcmlimit": "max",
                        **_INFO_PARAMS,
                    }
                )
            )
        )
    return members


def _enumerate_scope(client: _MediaWikiClient, scope: _Scope) -> dict[int, dict]:
    """List every page in scope with its ``lastrevid`` (no content)."""
    pages: dict[int, dict] = {}
    for namespace in sorted(scope.namespaces):
        pages.update(
            _ingestible(
                client.query_pages(
                    {
                        "generator": "allpages",
                        "gapnamespace": str(namespace),
                        "gapfilterredir": "nonredirects",
                        "gaplimit": "max",
                        **_INFO_PARAMS,
                    }
                )
            )
        )
    pages.update(_category_members(client, scope))
    for batch in _batched(sorted(scope.title_page_ids)):
        pages.update(
            _ingestible(client.query_pages({"pageids": "|".join(map(str, batch)), **_INFO_PARAMS}))
        )
    return pages


_META_PARAMS = {
    "prop": "info|revisions|categories",
    "inprop": "url",
    "rvprop": "ids|timestamp|user|comment",
    "clprop": "hidden",
    "cllimit": "max",
}


def _fetch_meta(
    client: _MediaWikiClient,
    page_ids: Iterable[int] = (),
    titles: Iterable[str] = (),
) -> tuple[dict[int, dict], set[int]]:
    """Fetch live metadata for pages; return ``(present_by_id, missing_ids)``."""
    present: dict[int, dict] = {}
    missing: set[int] = set()

    def collect(found: Iterable[dict]) -> None:
        for page in found:
            page_id = page.get("pageid")
            if not page_id:
                continue  # a title that does not exist: nothing to update
            if page.get("missing"):
                missing.add(page_id)
            else:
                present[page_id] = page

    for batch in _batched(sorted(set(page_ids))):
        collect(client.query_pages({"pageids": "|".join(map(str, batch)), **_META_PARAMS}))
    for batch in _batched(sorted(set(titles))):
        collect(client.query_pages({"titles": "|".join(batch), **_META_PARAMS}))
    return present, missing - set(present)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_SKIP_TAGS = {"script", "style", "noscript", "template", "head", "title", "meta", "link"}
_SKIP_CLASSES = {
    "mw-editsection",
    "reference",
    "references",
    "reflist",
    "mw-references-wrap",
    "navbox",
    "navbox-styles",
    "noprint",
    "metadata",
    "mw-empty-elt",
    "shortdescription",
    "hatnote",
    "toc",
    "catlinks",
    "printfooter",
    "mw-jump-link",
    "sistersitebox",
}
_VOID_TAGS = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "table",
    "tr",
    "ul",
    "ol",
    "dl",
    "dd",
    "dt",
    "blockquote",
    "pre",
    "figure",
    "figcaption",
    "caption",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class _HTMLToText(HTMLParser):
    """Reduce MediaWiki parser output to readable plain text.

    Dependency-free on purpose. Drops styles, scripts, edit links, reference
    markers, navboxes and hidden elements; keeps paragraphs, list items,
    headings (as ``#`` lines) and table rows (cells joined with ``|``).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0

    @staticmethod
    def _hidden(attrs: list[tuple[str, str | None]]) -> bool:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        style = (values.get("style") or "").replace(" ", "").lower()
        return bool(classes & _SKIP_CLASSES) or "display:none" in style

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag not in _VOID_TAGS and (tag in _SKIP_TAGS or self._hidden(attrs)):
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag in _HEADINGS:
            self._parts.append("\n\n" + "#" * _HEADINGS[tag] + " ")
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag == "br":
            self._parts.append("\n")
        elif tag in ("td", "th"):
            self._parts.append(" | ")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is None and tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        # A list item or table row ends where the next one starts, so
        # consecutive items stay on consecutive lines.
        if tag in _HEADINGS or (tag in _BLOCK_TAGS and tag != "tr"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_tag is None:
            self._parts.append(data)

    def text(self) -> str:
        return _tidy("".join(self._parts))


_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


def _tidy(text: str) -> str:
    lines = []
    for raw_line in _INVISIBLE_RE.sub("", text).splitlines():
        line = re.sub(r"[ \t\u00a0]+", " ", raw_line).strip()
        # Table cells open with a separator; drop it at the edges of a row, so
        # an empty cell leaves no stray "|".
        line = line.strip("| ").strip()
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    """Convert ``action=parse`` HTML to plain text."""
    parser = _HTMLToText()
    parser.feed(html or "")
    parser.close()
    return parser.text()


_WIKI_HEADING_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)


def extract_to_text(extract: str) -> str:
    """Turn a TextExtracts plain-text extract's ``== Heading ==`` lines into ``##``."""
    converted = _WIKI_HEADING_RE.sub(lambda m: "#" * len(m.group(1)) + " " + m.group(2), extract)
    return _tidy(converted)


def _page_text(client: _MediaWikiClient, page_id: int, use_extracts: bool) -> str | None:
    """Rendered page text, or ``None`` when the page vanished meanwhile."""
    try:
        if use_extracts:
            pages = client.query_pages(
                {
                    "pageids": str(page_id),
                    "prop": "extracts",
                    "explaintext": "1",
                    "exsectionformat": "wiki",
                }
            )
            page = pages[0] if pages else {}
            if page.get("missing"):
                return None
            return extract_to_text(page.get("extract") or "")
        parsed = (
            client.get(
                {
                    "action": "parse",
                    "pageid": str(page_id),
                    "prop": "text",
                    "disableeditsection": "1",
                    "disablelimitreport": "1",
                    "disabletoc": "1",
                }
            ).get("parse")
            or {}
        )
        return html_to_text(parsed.get("text") or "")
    except MediaWikiAPIError as exc:
        if exc.code in _GONE_API_ERRORS:
            return None
        raise


def _describe_revision(revision: dict) -> str:
    parts = [revision.get("timestamp") or "unknown time"]
    if revision.get("user") and not revision.get("userhidden"):
        parts.append(f"by {revision['user']}")
    line = " ".join(parts)
    if revision.get("revid"):
        line += f" (revision {revision['revid']})"
    comment = revision.get("comment")
    if comment and not revision.get("commenthidden"):
        line += f": {comment}"
    return line


def _revision_history(client: _MediaWikiClient, page_id: int, limit: int) -> list[dict]:
    response = client.get(
        {
            "action": "query",
            "pageids": str(page_id),
            "prop": "revisions",
            "rvprop": "ids|timestamp|user|comment",
            "rvlimit": str(limit),
        }
    )
    pages = (response.get("query") or {}).get("pages") or []
    return (pages[0].get("revisions") or []) if pages else []


def _strip_namespace(title: str) -> str:
    return title.split(":", 1)[1] if ":" in title else title


def _render_row(
    client: _MediaWikiClient,
    page: dict,
    settings: _Settings,
    use_extracts: bool,
    node_set: str,
) -> dict | None:
    """Build the document row for one page, or ``None`` if it vanished meanwhile."""
    page_id = page["pageid"]
    text = _page_text(client, page_id, use_extracts)
    if text is None:
        return None

    header: list[str] = []
    categories = [
        _strip_namespace(category["title"])
        for category in page.get("categories") or []
        if not category.get("hidden") and category.get("title")
    ]
    if categories:
        header.append("Categories: " + ", ".join(categories))
    history = (
        _revision_history(client, page_id, settings.revision_history)
        if settings.revision_history > 0
        else []
    )
    latest = (page.get("revisions") or [{}])[0]
    if latest and not history:
        header.append("Last edited: " + _describe_revision(latest))

    sections = ["\n".join(header), text]
    if history:
        sections.append(
            "## Recent edits\n" + "\n".join(f"- {_describe_revision(r)}" for r in history)
        )

    return {
        "id": str(page_id),
        "title": page.get("title") or "",
        "content": "\n\n".join(section for section in sections if section),
        "url": page.get("fullurl") or page.get("canonicalurl") or "",
        "_deleted": False,
        dlt_utils.NODE_SET_COLUMN: [node_set],
    }


def _seen(page: dict) -> dict:
    """What was synced for a page; any difference means it must be re-rendered."""
    return {"revid": page.get("lastrevid"), "title": page.get("title"), "ns": page.get("ns")}


def _tombstone(page_id: int | str) -> dict:
    return {"id": str(page_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
@dataclass
class _Run:
    client: _MediaWikiClient
    settings: _Settings
    scope: _Scope
    use_extracts: bool
    node_set: str
    previous: dict[int, dict]
    pages: dict[int, dict] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    def emit(self, page: dict) -> Iterator[dict]:
        """Render a page if it is new or changed; tombstone it if it vanished."""
        page_id = page["pageid"]
        seen = _seen(page)
        if self.previous.get(page_id) == seen and not self.stats.get("rerender"):
            self.pages[page_id] = seen
            self.stats["pages_unchanged"] += 1
            return
        row = _render_row(self.client, page, self.settings, self.use_extracts, self.node_set)
        if row is None:
            yield from self.forget(page_id)
            return
        self.pages[page_id] = seen
        self.stats["pages_changed"] += 1
        yield row

    def forget(self, page_id: int) -> Iterator[dict]:
        self.pages.pop(page_id, None)
        if page_id in self.previous:
            self.stats["deleted"] += 1
            yield _tombstone(page_id)


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def _full_sync_reason(state: dict, settings: _Settings, site: _Site) -> str | None:
    if not state.get("rc_floor"):
        return "initial"
    if settings.full_sync:
        return "requested"
    if state.get("scope") != settings.scope_fingerprint():
        return "scope_changed"
    if state.get("render") != settings.render_fingerprint():
        return "render_changed"
    last_full = state.get("last_full_sync")
    if settings.reconcile_after_days is not None and (
        not last_full
        or _parse_time(site.server_time) - _parse_time(last_full)
        >= timedelta(days=settings.reconcile_after_days)
    ):
        return "periodic"
    return None


def _replay_start(state: dict, settings: _Settings) -> str:
    floor = _parse_time(state["rc_floor"])
    return _format_time(floor - timedelta(seconds=settings.overlap_seconds))


def _retention_gap(client: _MediaWikiClient, start: str) -> bool:
    """True when ``recentchanges`` no longer reaches back to ``start``."""
    response = client.get(
        {
            "action": "query",
            "list": "recentchanges",
            "rcdir": "newer",
            "rclimit": "1",
            "rcprop": "timestamp",
        }
    )
    entries = (response.get("query") or {}).get("recentchanges") or []
    if not entries:
        return True
    return _parse_time(entries[0]["timestamp"]) > _parse_time(start)


def _full_sync(run: _Run) -> Iterator[dict]:
    current = _enumerate_scope(run.client, run.scope)
    run.stats["pages_scanned"] = len(current)

    if run.previous and not current:
        # An empty enumeration while pages were known is far more likely a
        # typo'd category, a permissions change or an outage than a wiki that
        # deleted everything. Forgetting the whole dataset would be permanent.
        logger.warning(
            "MediaWiki: the scope enumerated 0 pages but %d were synced before; "
            "skipping deletions this run.",
            len(run.previous),
        )
        run.pages.update(run.previous)
        return

    changed = [
        page_id
        for page_id, page in current.items()
        if run.stats.get("rerender") or run.previous.get(page_id) != _seen(page)
    ]
    for page_id in current.keys() - set(changed):
        run.pages[page_id] = run.previous[page_id]
        run.stats["pages_unchanged"] += 1

    # Enumeration already proved these pages are in scope; a page deleted
    # since then comes back missing and is forgotten.
    present, _missing = _fetch_meta(run.client, page_ids=changed)
    for page_id in changed:
        if page_id in present:
            yield from run.emit(present[page_id])
        else:
            yield from run.forget(page_id)

    for page_id in sorted(run.previous.keys() - current.keys()):
        yield from run.forget(page_id)


def _incremental_sync(run: _Run, start: str) -> Iterator[dict]:
    scope = run.scope
    known_titles = {seen.get("title"): page_id for page_id, seen in run.previous.items()}
    page_ids: set[int] = set()
    titles: set[str] = set()
    entries = 0

    for response in run.client.continued(
        {
            "action": "query",
            "list": "recentchanges",
            "rcstart": start,
            "rcdir": "newer",
            "rctype": "edit|new|log",
            "rcprop": "ids|title|timestamp|loginfo",
            "rclimit": "max",
        }
    ):
        for entry in (response.get("query") or {}).get("recentchanges") or []:
            entries += 1
            params = entry.get("logparams") or {}
            named = [(entry.get("title"), entry.get("ns"))]
            if params.get("target_title"):  # move
                named.append((params["target_title"], params.get("target_ns")))
            if params.get("dest_title"):  # history merge
                named.append((params["dest_title"], params.get("dest_ns")))

            # Only entries that can touch the synced set matter: a page already
            # synced or selected by title, or a title in a selected namespace.
            # Category members are diffed below instead, which also catches
            # membership that changed through a template.
            page_id = entry.get("pageid") or 0
            relevant = page_id in run.previous or page_id in scope.title_page_ids
            for title, namespace in named:
                if title in known_titles:
                    page_ids.add(known_titles[title])
                    relevant = True
                elif namespace in scope.namespaces:
                    relevant = True
            if not relevant:
                continue
            if page_id:
                page_ids.add(page_id)
            titles.update(title for title, _ in named if title)

    run.stats["changes_replayed"] = entries
    # A selected title that was created or renamed into place since the last
    # run: its id is resolved fresh every run.
    page_ids |= scope.title_page_ids - run.previous.keys()
    if scope.category_titles:
        members = _category_members(run.client, scope)
        # New members, and synced pages that left every category and are not
        # held in scope by a namespace or title, are re-checked.
        page_ids |= members.keys() - run.previous.keys()
        page_ids |= {
            page_id
            for page_id, seen in run.previous.items()
            if page_id not in members
            and page_id not in scope.title_page_ids
            and seen.get("ns") not in scope.namespaces
        }

    present, missing = _fetch_meta(run.client, page_ids=page_ids, titles=titles)
    run.pages.update(run.previous)
    for page_id in sorted(present):
        page = present[page_id]
        if scope.contains(page):
            yield from run.emit(page)
        else:
            yield from run.forget(page_id)
    for page_id in sorted(missing):
        yield from run.forget(page_id)


def sync_pages(client: _MediaWikiClient, settings: _Settings, state: dict, stats: dict):
    """Yield changed pages and deletion tombstones, then advance ``state``.

    ``state`` is only written after the last row was yielded, so a run that
    raises part-way keeps the previous cursor (dlt also discards the state of
    a failed load).
    """
    site = _site_info(client)
    use_extracts = settings.content_format == "extracts" or (
        settings.content_format == "auto" and site.has_text_extracts
    )
    previous = {int(page_id): seen for page_id, seen in (state.get("pages") or {}).items()}

    stats.clear()
    stats.update(pages_scanned=0, pages_changed=0, pages_unchanged=0, deleted=0)
    reason = _full_sync_reason(state, settings, site)
    start = None
    if reason is None:
        start = _replay_start(state, settings)
        if _retention_gap(client, start):
            reason = "retention_gap"
    stats["mode"] = "full" if reason else "incremental"
    stats["full_sync_reason"] = reason
    stats["rerender"] = reason == "render_changed"

    run = _Run(
        client=client,
        settings=settings,
        scope=_resolve_scope(client, settings),
        use_extracts=use_extracts,
        node_set=f"{SOURCE_TAG}:{site.server_name}",
        previous=previous,
        stats=stats,
    )
    if reason:
        logger.info("MediaWiki: full sync (%s).", reason)
        yield from _full_sync(run)
    else:
        yield from _incremental_sync(run, start)

    state["pages"] = {str(page_id): seen for page_id, seen in sorted(run.pages.items())}
    state["rc_floor"] = site.server_time
    state["scope"] = settings.scope_fingerprint()
    state["render"] = settings.render_fingerprint()
    if reason:
        state["last_full_sync"] = site.server_time
    stats["requests"] = client.requests
    stats.pop("rerender", None)
    logger.info(
        "MediaWiki: %s sync, %d changed, %d unchanged, %d deleted, %d request(s).",
        stats["mode"],
        stats["pages_changed"],
        stats["pages_unchanged"],
        stats["deleted"],
        client.requests,
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _default_resource_name(api_url: str) -> str:
    parsed = urlparse(api_url)
    path = parsed.path.removesuffix("api.php")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{parsed.netloc}{path}".lower()).strip("_")
    return f"mediawiki_{slug}"


def mediawiki_source(
    api_url: str | None = None,
    *,
    namespaces: list[int] | None = None,
    categories: list[str] | None = None,
    titles: list[str] | None = None,
    content_format: str = "auto",
    revision_history: int = 5,
    overlap_seconds: int = 600,
    reconcile_after_days: float | None = 7,
    full_sync: bool = False,
    username: str | None = None,
    password: str | None = None,
    user_agent: str | None = None,
    maxlag: int | None = 5,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    http_client: Any = None,
):
    """Return a ``dlt`` resource yielding one document row per in-scope wiki page.

    Hand the result to ``cognee.remember(...)`` with
    ``write_disposition="merge"``, ``primary_key="id"`` and
    ``max_rows_per_table=0``.

    Args:
        api_url: The wiki's ``api.php`` URL, e.g.
            ``https://en.wikipedia.org/w/api.php``. Falls back to
            ``MEDIAWIKI_API_URL``.
        namespaces: Namespace ids to sync in full (``0`` is articles). Defaults
            to ``[0]`` when no ``categories`` or ``titles`` are given.
        categories: Sync the direct member pages of these categories (with or
            without the ``Category:`` prefix).
        titles: Sync these pages. Redirects are followed, so a renamed page
            stays selected.
        content_format: ``"extracts"`` (TextExtracts plain text), ``"parse"``
            (rendered HTML reduced to text) or ``"auto"``: extracts when the
            wiki has the extension, parse otherwise.
        revision_history: How many recent edits (time, author, summary) to list
            in each document. ``0`` lists none and saves a request per page.
        overlap_seconds: How far before the previous run's server time the
            change feed is replayed, to catch entries written out of order.
        reconcile_after_days: Run a full enumeration at least this often
            (``None`` never does so unless needed).
        full_sync: Force a full enumeration on this run.
        username: Bot-password user name (``User@botname``) for a private wiki.
            Falls back to ``MEDIAWIKI_USERNAME``.
        password: Bot password. Falls back to ``MEDIAWIKI_PASSWORD``.
        user_agent: User-Agent header. Wikimedia asks for contact details, e.g.
            ``"my-app/1.0 (me@example.com)"``. Falls back to
            ``MEDIAWIKI_USER_AGENT``, then a generic connector agent.
        maxlag: The ``maxlag`` seconds sent with every request (``None`` to
            omit it); the wiki answers "too busy" above it and the request is
            retried after the ``Retry-After`` it sends.
        resource_name: dlt resource / staging table name, also the key of the
            incremental state. Defaults to one derived from ``api_url``, so two
            wikis can sync into one dataset. Two sources sharing a name share
            state, so each would forget the other's pages.
        check_active: Optional host authorization checkpoint during extraction.
        http_client: Pre-built ``httpx.Client`` (a test injection point, or a
            client that already carries a private wiki's session cookie).
    """
    import dlt
    import httpx

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The MediaWiki connector requires a cognee build that reads per-row node "
            "sets (DOCUMENT_SYNC_VERSION >= 2, cognee 1.6.3+). Upgrade cognee."
        )

    resolved_api_url = (api_url or os.getenv("MEDIAWIKI_API_URL") or "").strip()
    parsed = urlparse(resolved_api_url)
    if parsed.scheme not in ("http", "https") or not parsed.path.endswith("api.php"):
        raise ValueError(
            "mediawiki_source needs the wiki's api.php URL, e.g. "
            "https://en.wikipedia.org/w/api.php (pass api_url= or set MEDIAWIKI_API_URL)."
        )
    if content_format not in _CONTENT_FORMATS:
        raise ValueError(f"content_format must be one of {_CONTENT_FORMATS}.")
    if revision_history < 0 or overlap_seconds < 0:
        raise ValueError("revision_history and overlap_seconds must not be negative.")

    resolved_username = username or os.getenv("MEDIAWIKI_USERNAME")
    resolved_password = password or os.getenv("MEDIAWIKI_PASSWORD")
    if bool(resolved_username) != bool(resolved_password):
        raise ValueError("Pass both username and password (a bot password), or neither.")

    selected_namespaces = list(namespaces or [])
    if not selected_namespaces and not categories and not titles:
        selected_namespaces = [0]
    settings = _Settings(
        namespaces=tuple(sorted({int(namespace) for namespace in selected_namespaces})),
        categories=tuple(sorted({c.strip() for c in categories or [] if c.strip()})),
        titles=tuple(sorted({t.strip() for t in titles or [] if t.strip()})),
        content_format=content_format,
        revision_history=revision_history,
        overlap_seconds=overlap_seconds,
        reconcile_after_days=reconcile_after_days,
        full_sync=full_sync,
    )
    resolved_user_agent = user_agent or os.getenv("MEDIAWIKI_USER_AGENT") or DEFAULT_USER_AGENT
    name = resource_name or _default_resource_name(resolved_api_url)
    stats: dict[str, Any] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup. cognee_node_set needs no
        # hint here: ingest_dlt_source applies it to every document source.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def mediawiki_pages():
        owned_client = (
            None if http_client is not None else httpx.Client(timeout=30.0, follow_redirects=True)
        )
        try:
            client = _MediaWikiClient(
                http_client or owned_client, resolved_api_url, resolved_user_agent, maxlag
            )
            if resolved_username:
                client.login(resolved_username, resolved_password)
            state = dlt.current.resource_state()
            if state.get("pages") and state.get("api_url") not in (None, resolved_api_url):
                # Same resource name, different wiki: syncing on would tombstone
                # every page of the other wiki.
                raise ValueError(
                    f"MediaWiki resource {name!r} already holds pages from another wiki "
                    "in this dataset. Give each wiki its own resource_name."
                )
            state["api_url"] = resolved_api_url
            yield from dlt_utils.guarded_rows(
                sync_pages(client, settings, state, stats), check_active
            )
        finally:
            if owned_client is not None:
                owned_client.close()

    resource = mediawiki_pages()
    # Opt into the document ingestion path: each page row (id/title/content/url
    # plus cognee_node_set) becomes a text document that flows through normal
    # cognify. resolve_dlt_sources reads this marker; it never imports this
    # connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_TAG)
    # Keep the incremental state per (dataset, resource), so syncing the same
    # wiki into another dataset does not reuse this one's cursor.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    # Host-readable diagnostics: counts and the sync mode, never page content.
    resource.cognee_sync_stats = stats
    return resource
