"""MediaWiki connector for cognee: a ``dlt`` source that turns a wiki into memory.

Syncs the pages of any MediaWiki (Wikipedia, Fandom, an internal wiki) selected
by namespace, category or title, one document per page keyed by page id, with
the text rendered by the wiki itself. Later runs only fetch what changed and
forget what was deleted or left the selection::

    await cognee.remember(
        mediawiki_source("https://en.wikipedia.org/w/api.php", categories=["Physics"]),
        dataset_name="wiki",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

How changes are detected, when a full reconcile runs, how deletions, rate limits
and empty scopes are handled, and what ``cognee_sync_stats`` reports: see the
README.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

# MediaWiki accepts at most 50 page ids / titles per request for normal users
# (ApiBase::LIMIT_SML1; bots get 500, which this connector does not rely on).
_BATCH_SIZE = 50
# Attempts per request on 429 / 5xx / maxlag / network errors: our choice, so a
# lagging wiki costs at most a few minutes before the run stops cleanly.
_MAX_RETRIES = 5
# Longest wait honoured for Retry-After / maxlag. The sync runs while cognee holds
# its process-wide dlt staging lock, so a wiki asking for more stops the run cleanly
# (state untouched, retried next run) instead of sleeping inside the lock.
_MAX_RETRY_AFTER_SECONDS = 60.0
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Content models that hold code or data rather than prose.
_NON_PROSE_CONTENT_MODELS = {"css", "sanitized-css", "javascript", "json", "Scribunto"}
_CONTENT_FORMATS = ("auto", "extracts", "parse")
_RATE_LIMIT_API_ERRORS = {"maxlag", "ratelimited"}
_AUTH_API_ERRORS = {"readapidenied", "permissiondenied", "assertuserfailed", "assertbotfailed"}
_GONE_API_ERRORS = {"missingtitle", "nosuchpageid"}


class MediaWikiAPIError(RuntimeError):
    """The wiki answered with an API error (``{"error": {...}}``) or bad HTTP."""

    def __init__(self, code: str, info: str = ""):
        super().__init__(f"MediaWiki API error {code}: {info}" if info else code)
        self.code = code
        self.info = info


class MediaWikiAuthError(MediaWikiAPIError):
    """Login failed or the wiki refused to be read. Never carries the password."""


class MediaWikiRateLimitedError(MediaWikiAPIError):
    """The wiki kept asking to slow down (429, maxlag) beyond what a run waits."""


class MediaWikiScopeError(ValueError):
    """A configured category or title does not exist on the wiki (likely a typo)."""


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

        attempt = 0
        while True:
            last_attempt = attempt >= _MAX_RETRIES - 1
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
                attempt += 1
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                if last_attempt:
                    error_class = MediaWikiRateLimitedError if status == 429 else MediaWikiAPIError
                    raise error_class(f"http_{status}")
                self._backoff(f"HTTP {status}", response.headers, attempt, rate_limit=status == 429)
                attempt += 1
                continue
            if status in (401, 403):
                raise MediaWikiAuthError(f"http_{status}")
            if status >= 400:
                raise MediaWikiAPIError(f"http_{status}")

            try:
                payload = response.json()
            except ValueError:
                # A proxy or maintenance page answering 200 with HTML: treat it
                # as a failure, never as an empty result.
                raise MediaWikiAPIError(
                    "invalid_response", "the wiki did not answer with JSON"
                ) from None
            if not isinstance(payload, dict):
                raise MediaWikiAPIError("invalid_response", "the wiki did not answer an object")
            error = payload.get("error")
            if not error:
                return payload
            code = str(error.get("code") or "unknown")
            info = str(error.get("info") or "")
            if code in _RATE_LIMIT_API_ERRORS:
                if last_attempt:
                    raise MediaWikiRateLimitedError(code, info)
                self._backoff(code, response.headers, attempt, rate_limit=True)
                attempt += 1
                continue
            if code in _AUTH_API_ERRORS:
                raise MediaWikiAuthError(code, info)
            raise MediaWikiAPIError(code, info)

    def _backoff(self, reason: str, headers: Any, attempt: int, rate_limit: bool = False) -> None:
        delay = _retry_after(headers, attempt)
        if delay > _MAX_RETRY_AFTER_SECONDS:
            error_class = MediaWikiRateLimitedError if rate_limit else MediaWikiAPIError
            raise error_class(
                "retry_after_too_long",
                f"the wiki asked to wait {delay:.0f}s (more than {_MAX_RETRY_AFTER_SECONDS:.0f}s)",
            )
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
            # Only the result code and the wiki's own reason: never the raw body,
            # which can echo back request fields.
            reason = result.get("reason")
            if isinstance(reason, dict):  # some versions answer {"code", "text"}
                reason = reason.get("text") or reason.get("code")
            raise MediaWikiAuthError(
                "login_failed", str(reason or f"result: {result.get('result') or 'unknown'}")
            )


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") if headers is not None else None
    try:
        return max(float(header), 0.0)
    except (TypeError, ValueError):
        return min(float(2**attempt), _MAX_RETRY_AFTER_SECONDS)


# ---------------------------------------------------------------------------
# Site and scope
# ---------------------------------------------------------------------------
_CATEGORY_NS = 14


@dataclass(frozen=True)
class _Namespaces:
    """The wiki's namespace names, so localized prefixes (``Kategorie:``) work."""

    ids: dict[str, int] = field(default_factory=dict)  # lower-cased name / alias -> id
    case_sensitive: frozenset[int] = frozenset()

    def split(self, title: str) -> tuple[int, str]:
        """``(namespace id, normalized title without prefix)`` for any spelling."""
        title = title.replace("_", " ").strip()
        namespace = 0
        if ":" in title:
            prefix, rest = title.split(":", 1)
            key = prefix.strip().lower()
            if key in self.ids:
                namespace, title = self.ids[key], rest.strip()
        if namespace not in self.case_sensitive:
            title = title[:1].upper() + title[1:]
        return namespace, title


@dataclass(frozen=True)
class _Site:
    server_time: str
    server_name: str
    has_text_extracts: bool
    namespaces: _Namespaces = field(default_factory=_Namespaces)


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
    # The selected titles' current pages (``prop=info``), keyed by page id. Read
    # on every run anyway (to follow renames), so they double as change checks.
    title_pages: dict[int, dict] = field(default_factory=dict)

    @property
    def title_page_ids(self) -> frozenset[int]:
        return frozenset(self.title_pages)

    def contains(self, page: dict) -> bool:
        if page.get("redirect") or page.get("contentmodel") in _NON_PROSE_CONTENT_MODELS:
            return False
        if page.get("ns") in self.namespaces or page.get("pageid") in self.title_pages:
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
            "siprop": "general|extensions|namespaces|namespacealiases",
            "curtimestamp": "1",
        }
    )
    query = response.get("query") or {}
    general = query.get("general") or {}
    extensions = {extension.get("name") for extension in query.get("extensions") or []}
    server_time = response.get("curtimestamp")
    if not server_time:
        raise MediaWikiAPIError("no_server_time", "siteinfo returned no curtimestamp")

    ids: dict[str, int] = {}
    case_sensitive: set[int] = set()
    namespaces = query.get("namespaces") or {}
    for spec in namespaces.values() if isinstance(namespaces, dict) else namespaces:
        namespace = int(spec.get("id", 0))
        for name in (spec.get("name"), spec.get("canonical")):
            if name:
                ids[name.lower()] = namespace
        if spec.get("case") == "case-sensitive":
            case_sensitive.add(namespace)
    for alias in query.get("namespacealiases") or []:
        if alias.get("alias"):
            ids[alias["alias"].lower()] = int(alias.get("id", 0))
    ids.setdefault("category", _CATEGORY_NS)  # canonical, accepted by every wiki

    return _Site(
        server_time=server_time,
        server_name=general.get("servername") or urlparse(client._api_url).netloc,
        has_text_extracts="TextExtracts" in extensions,
        namespaces=_Namespaces(ids, frozenset(case_sensitive)),
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


def _category_title(name: str, namespaces: _Namespaces) -> str:
    """``Category:<name>`` for a bare or prefixed name, in any of the wiki's spellings.

    ``Kategorie:Physik`` on a German wiki is recognised as the category itself, not
    sent as ``Category:Kategorie:Physik``. ``Category:`` is the canonical prefix
    every wiki accepts; the API answers with its localized title.
    """
    namespace, rest = namespaces.split(name)
    if namespace != _CATEGORY_NS:
        rest = namespaces.split(f"Category:{name}")[1]
    return f"Category:{rest}"


def _resolve_category_titles(
    client: _MediaWikiClient, categories: Iterable[str], new: frozenset[str], site: _Site
) -> set[str]:
    """Normalize category names to the wiki's own (localized) category titles.

    A category can have members without a description page, so a name counts as
    unknown only when its page is missing *and* it has no members. Only names
    added to the configuration since the last run are checked: one synced before
    and emptied since is a deletion to propagate, not a typo.
    """
    namespaces = site.namespaces
    resolved: set[str] = set()
    unknown: list[str] = []
    wanted = sorted({name.strip() for name in categories if name.strip()})
    new_keys = {namespaces.split(_category_title(name, namespaces)) for name in new}
    for batch in _batched(wanted):
        pages = client.query_pages(
            {
                "titles": "|".join(_category_title(name, namespaces) for name in batch),
                "prop": "categoryinfo",
            }
        )
        for page in pages:
            resolved.add(page["title"])
            members = (page.get("categoryinfo") or {}).get("size") or 0
            if page.get("missing") and not members and namespaces.split(page["title"]) in new_keys:
                unknown.append(page["title"])
    if unknown:
        raise MediaWikiScopeError(
            f"These categories do not exist on the wiki (no page, no members): {sorted(unknown)}"
        )
    return resolved


def _resolve_title_pages(
    client: _MediaWikiClient, titles: Iterable[str], new: frozenset[str], site: _Site
) -> dict[int, dict]:
    """Resolve explicit titles to their current pages, following redirects (renames).

    Returns ``prop=info`` for each, which the incremental run compares instead of
    fetching the page again. A title newly added to the configuration that does
    not exist raises; one synced before and deleted since is left to
    forget-on-delete.
    """
    namespaces = site.namespaces
    pages_by_id: dict[int, dict] = {}
    unknown: list[str] = []
    new_keys = {namespaces.split(title) for title in new}
    for batch in _batched(sorted({title.strip() for title in titles if title.strip()})):
        for page in client.query_pages(
            {"titles": "|".join(batch), "redirects": "1", **_INFO_PARAMS}
        ):
            if page.get("pageid") and not page.get("missing"):
                pages_by_id.update(_ingestible([page]))
            elif page.get("missing") and namespaces.split(page.get("title") or "") in new_keys:
                unknown.append(page.get("title") or "")
    if unknown:
        raise MediaWikiScopeError(f"These titles do not exist on the wiki: {sorted(unknown)}")
    return pages_by_id


def _resolve_scope(
    client: _MediaWikiClient, settings: _Settings, previous_scope: Any, site: _Site
) -> _Scope:
    known_categories: set[str] = set()
    known_titles: set[str] = set()
    if isinstance(previous_scope, list) and len(previous_scope) == 3:
        known_categories = set(previous_scope[1] or [])
        known_titles = set(previous_scope[2] or [])
    return _Scope(
        namespaces=frozenset(settings.namespaces),
        category_titles=frozenset(
            _resolve_category_titles(
                client,
                settings.categories,
                frozenset(settings.categories) - known_categories,
                site,
            )
        ),
        title_pages=_resolve_title_pages(
            client, settings.titles, frozenset(settings.titles) - known_titles, site
        ),
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
    pages.update(scope.title_pages)  # already read with prop=info this run
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
    # Sorted: continuation can merge the list in a different order between runs,
    # which would change the content hash and re-extract an unchanged page.
    categories = sorted(
        _strip_namespace(category["title"])
        for category in page.get("categories") or []
        if not category.get("hidden") and category.get("title")
    )
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
    """What a page looked like when synced: the fields that signal a change."""
    return {
        "revid": page.get("lastrevid"),
        "title": page.get("title"),
        "ns": page.get("ns"),
        "touched": page.get("touched"),
    }


def _same(before: dict | None, now: dict) -> bool:
    """Whether a synced page is unchanged.

    ``touched`` moves when a template or transcluded page re-renders the page
    without an edit, so it is compared too. State written before it was tracked
    has no ``touched``; those pages are compared on the rest, so upgrading does
    not re-render everything.
    """
    if not before:
        return False
    keys = ("revid", "title", "ns", "touched") if "touched" in before else ("revid", "title", "ns")
    return all(before.get(key) == now.get(key) for key in keys)


def _row_hash(row: dict) -> str:
    text = "\x1f".join((row.get("title") or "", row.get("url") or "", row.get("content") or ""))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _tombstone(page_id: int | str) -> dict:
    return {"id": str(page_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
# A namespace that enumerates empty twice in a row is taken at its word: the
# first time could be an outage or a permissions change, a second consecutive
# one almost never is, and the old pages must not stay in memory forever.
_EMPTY_RUNS_BEFORE_FORGET = 2


@dataclass
class _Run:
    client: _MediaWikiClient
    settings: _Settings
    scope: _Scope
    use_extracts: bool
    node_set: str
    previous: dict[int, dict]
    stats: dict[str, int]
    rerender: bool = False
    # Re-yield even an identical rendering (``full_sync=True`` asks for that).
    force_yield: bool = False
    # Pages already re-rendered by an earlier, rate-limited attempt at the same
    # re-render; they are compared as usual instead of rendered again.
    rerendered_before: frozenset[int] = frozenset()
    keep_state: bool = False
    empty_runs: int = 0
    pages: dict[int, dict] = field(default_factory=dict)
    rendered: set[int] = field(default_factory=set)
    forgotten: set[int] = field(default_factory=set)

    def needs_render(self, page_id: int, seen: dict) -> bool:
        if self.rerender and page_id not in self.rerendered_before:
            return True
        return not _same(self.previous.get(page_id), seen)

    def keep(self, page_id: int) -> None:
        if page_id in self.pages:
            return  # reached twice (listed and named by the feed): count it once
        self.pages[page_id] = self.previous[page_id]
        self.stats["pages_unchanged"] += 1

    def emit(self, page: dict) -> Iterator[dict]:
        """Render a page if it is new or changed; tombstone it if it vanished."""
        page_id = page["pageid"]
        seen = _seen(page)
        if not self.needs_render(page_id, seen):
            self.keep(page_id)
            return
        row = _render_row(self.client, page, self.settings, self.use_extracts, self.node_set)
        if row is None:
            yield from self.forget(page_id)
            return
        seen["hash"] = _row_hash(row)
        self.pages[page_id] = seen
        self.rendered.add(page_id)
        before = self.previous.get(page_id) or {}
        if before.get("hash") == seen["hash"] and not self.force_yield:
            # ``touched`` also moves on purges, cache updates and link changes;
            # an identical rendering is not a change, so nothing is re-ingested.
            self.stats["pages_unchanged"] += 1
            self.stats["pages_rendered_unchanged"] += 1
            return
        self.stats["pages_changed"] += 1
        yield row

    def forget(self, page_id: int) -> Iterator[dict]:
        self.pages.pop(page_id, None)
        self.forgotten.add(page_id)
        if page_id in self.previous:
            self.stats["deleted"] += 1
            yield _tombstone(page_id)

    def progress(self) -> dict[str, dict]:
        """The pages state after a partial run: previous, updated by what was done."""
        pages = {
            page_id: seen
            for page_id, seen in self.previous.items()
            if page_id not in self.forgotten
        }
        pages.update(self.pages)
        return {str(page_id): seen for page_id, seen in sorted(pages.items())}


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


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


# Reasons that re-render every page: the output format changed, or the caller
# asked for it.
_RERENDER_REASONS = {"render_changed", "requested"}


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


def _full_sync(run: _Run, reason: str, state: dict) -> Iterator[dict]:
    current = _enumerate_scope(run.client, run.scope)
    run.stats["pages_scanned"] = len(current)

    if run.previous and not current:
        empty_runs = int(state.get("empty_runs") or 0) + 1
        # Title and category scopes are confirmed empty by the API itself (the
        # titles are reported missing, the categories have no members); so is a
        # scope the caller just changed. A namespace that lists nothing could be
        # an outage, so it is only believed the second time in a row.
        confirmed = (
            not run.scope.namespaces
            or reason == "scope_changed"
            or empty_runs >= _EMPTY_RUNS_BEFORE_FORGET
        )
        if not confirmed:
            logger.warning(
                "MediaWiki: the scope enumerated 0 pages but %d were synced before; "
                "keeping them for now (forgotten if the next full sync is empty too).",
                len(run.previous),
            )
            run.keep_state = True
            run.empty_runs = empty_runs
            return
        logger.warning(
            "MediaWiki: the scope is empty; forgetting the %d pages synced before.",
            len(run.previous),
        )

    changed = [
        page_id for page_id, page in current.items() if run.needs_render(page_id, _seen(page))
    ]
    for page_id in current.keys() - set(changed):
        run.keep(page_id)

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


def _named_titles(entry: dict, params: dict) -> list[tuple[str | None, int | None]]:
    """Every page a feed or log entry names: itself, a move target, a merge partner."""
    named = [(entry.get("title"), entry.get("ns"))]
    if params.get("target_title"):  # move
        named.append((params["target_title"], params.get("target_ns")))
    if params.get("dest_title"):  # history merge, logged on the source (older wikis)
        named.append((params["dest_title"], params.get("dest_ns")))
    if params.get("src_title"):  # history merge, logged on the destination (merge-into)
        named.append((params["src_title"], params.get("src_ns")))
    return named


def _read_feed(run: _Run, start: str) -> tuple[set[int], set[str]]:
    """Pages the change feed names since ``start``, for the selected namespaces."""
    scope = run.scope
    known_titles = {seen.get("title"): page_id for page_id, seen in run.previous.items()}
    page_ids: set[int] = set()
    titles: set[str] = set()
    entries = 0

    def consider(entry: dict, params: dict) -> None:
        # Only entries that can touch the synced set matter: a page already
        # synced, or a title in a selected namespace.
        named = _named_titles(entry, params)
        page_id = entry.get("pageid") or 0
        relevant = page_id in run.previous
        for title, namespace in named:
            if title in known_titles:
                page_ids.add(known_titles[title])
                relevant = True
            elif namespace in scope.namespaces:
                relevant = True
        if not relevant:
            return
        if page_id:
            page_ids.add(page_id)
        titles.update(title for title, _ in named if title)

    for response in run.client.continued(
        {
            "action": "query",
            "list": "recentchanges",
            "rcstart": start,
            "rcdir": "newer",
            "rctype": "edit|new|log",
            "rcnamespace": "|".join(map(str, sorted(scope.namespaces))),
            "rcprop": "ids|title|timestamp|loginfo",
            "rclimit": "max",
        }
    ):
        for entry in (response.get("query") or {}).get("recentchanges") or []:
            entries += 1
            consider(entry, entry.get("logparams") or {})
    # The feed above is filtered by the namespace an entry is logged under, which
    # for a move is the *source* namespace: a draft moved into the articles would
    # be missed. Moves and history merges are rare, so read them unfiltered.
    for log_type in ("move", "merge"):
        for response in run.client.continued(
            {
                "action": "query",
                "list": "logevents",
                "letype": log_type,
                "lestart": start,
                "ledir": "newer",
                "leprop": "ids|title|type|timestamp|details",
                "lelimit": "max",
            }
        ):
            for entry in (response.get("query") or {}).get("logevents") or []:
                entries += 1
                consider(entry, entry.get("params") or {})
    run.stats["feed_read"] = 1
    run.stats["changes_replayed"] = entries
    return page_ids, titles


def _incremental_sync(run: _Run, start: str) -> Iterator[dict]:
    scope = run.scope
    page_ids: set[int] = set()
    titles: set[str] = set()
    # The feed is only needed to discover pages in the selected namespaces.
    if scope.namespaces:
        page_ids, titles = _read_feed(run, start)

    # Pages held in scope by a title or a category are compared against listings
    # this run reads anyway (the titles are resolved every run to follow renames,
    # the category member lists are diffed), so only the ones that differ are
    # fetched: an unchanged 10,000 page category costs ~20 listing requests.
    listed: dict[int, dict] = dict(scope.title_pages)
    if scope.category_titles:
        listed.update(_category_members(run.client, scope))
    run.stats["pages_scanned"] = len(listed)
    for page_id, page in listed.items():
        if run.needs_render(page_id, _seen(page)):
            page_ids.add(page_id)
        else:
            run.keep(page_id)
    # A synced page outside the selected namespaces that is no longer listed has
    # left its category, lost its title, or was deleted: re-check it.
    page_ids |= {
        page_id
        for page_id, seen in run.previous.items()
        if seen.get("ns") not in scope.namespaces and page_id not in listed
    }

    present, missing = _fetch_meta(run.client, page_ids=page_ids, titles=titles)
    run.stats["pages_scanned"] += len(present.keys() - listed.keys())
    for page_id in sorted(present):
        page = present[page_id]
        if scope.contains(page):
            yield from run.emit(page)
        else:
            yield from run.forget(page_id)
    for page_id in sorted(missing):
        yield from run.forget(page_id)
    # Everything not looked at this run is unchanged.
    for page_id in run.previous.keys() - run.pages.keys() - run.forgotten:
        run.pages[page_id] = run.previous[page_id]


def _sync(
    client: _MediaWikiClient,
    settings: _Settings,
    state: dict,
    stats: dict[str, int],
    holder: dict,
):
    site = _site_info(client)
    use_extracts = settings.content_format == "extracts" or (
        settings.content_format == "auto" and site.has_text_extracts
    )
    previous = {int(page_id): seen for page_id, seen in (state.get("pages") or {}).items()}

    reason = _full_sync_reason(state, settings, site)
    start = None
    if reason is None:
        start = _replay_start(state, settings)
        # Only namespace scopes rely on the feed; the others are compared directly.
        if settings.namespaces and _retention_gap(client, start):
            reason = "retention_gap"
    if reason:
        stats["full_sync"] = 1
        stats[f"full_sync_{reason}"] = 1

    rerender = reason in _RERENDER_REASONS
    progress = state.get("rerender_progress") or {}
    rerendered_before = frozenset(
        int(page_id)
        for page_id in progress.get("ids") or []
        if rerender and progress.get("for") == [reason, settings.render_fingerprint()]
    )
    run = _Run(
        client=client,
        settings=settings,
        scope=_resolve_scope(client, settings, state.get("scope"), site),
        use_extracts=use_extracts,
        node_set=f"{SOURCE_TAG}:{site.server_name}",
        previous=previous,
        stats=stats,
        rerender=rerender,
        force_yield=reason == "requested",
        rerendered_before=rerendered_before,
    )
    holder.update(run=run, reason=reason)
    if reason:
        logger.info("MediaWiki: full sync (%s).", reason)
        yield from _full_sync(run, reason, state)
    else:
        yield from _incremental_sync(run, start)

    if run.keep_state:
        stats["failed"] = 1
        stats["failed_empty_scope"] = 1
        state["empty_runs"] = run.empty_runs
        return
    # State is written only after the last row was yielded, so a run that
    # raises part-way keeps the previous cursor (dlt also discards the state of
    # a failed load).
    state["pages"] = {str(page_id): seen for page_id, seen in sorted(run.pages.items())}
    state["rc_floor"] = site.server_time
    state["scope"] = settings.scope_fingerprint()
    state["render"] = settings.render_fingerprint()
    if reason:
        state["last_full_sync"] = site.server_time
    state.pop("empty_runs", None)
    state.pop("rerender_progress", None)
    logger.info(
        "MediaWiki: %s sync, %d changed, %d unchanged, %d deleted, %d request(s).",
        "full" if reason else "incremental",
        stats["pages_changed"],
        stats["pages_unchanged"],
        stats["deleted"],
        client.requests,
    )


def _save_progress(state: dict, settings: _Settings, holder: dict) -> None:
    """After a rate-limited stop: keep what was done, so the next run continues.

    The cursor, scope and render settings are left alone (the next run replays
    the same window and repeats the same full sync if one was due), but the
    pages already compared, rendered or forgotten are recorded, so it does not
    start over from page 0.
    """
    run: _Run | None = holder.get("run")
    if run is None or run.keep_state:
        return
    state["pages"] = run.progress()
    if run.rerender:
        done = run.rendered | set(run.rerendered_before)
        state["rerender_progress"] = {
            "for": [holder.get("reason"), settings.render_fingerprint()],
            "ids": sorted(done),
        }


def sync_pages(client: _MediaWikiClient, settings: _Settings, state: dict, stats: dict):
    """Yield changed pages and deletion tombstones, then advance ``state``.

    ``stats`` receives plain int counters for the host (all present on every run):

    - ``pages_scanned``: pages whose metadata was compared this run;
    - ``pages_changed`` / ``pages_unchanged`` / ``deleted``: what happened to them
      (``pages_rendered_unchanged`` counts re-renders that produced the same text);
    - ``feed_read`` (0/1) and ``changes_replayed``: whether the change feed was
      read and how many entries it returned (title and category scopes never
      read it);
    - ``requests``; ``full_sync`` (0/1, plus ``full_sync_<reason>``);
    - ``failed`` (0/1, plus ``failed_<reason>``: ``rate_limit``, ``auth``,
      ``empty_scope``, ``scope``, ``api`` or ``error``). Every failure sets it,
      including the ones that raise.

    A wiki that keeps rate-limiting the run stops it cleanly: what was yielded
    stays, the work done is recorded, and the next run continues from there.
    """
    stats.clear()
    stats.update(
        pages_scanned=0,
        pages_changed=0,
        pages_unchanged=0,
        pages_rendered_unchanged=0,
        deleted=0,
        feed_read=0,
        changes_replayed=0,
        requests=0,
        full_sync=0,
        failed=0,
    )
    holder: dict = {}

    def failed(reason: str) -> None:
        stats["failed"] = 1
        stats[f"failed_{reason}"] = 1

    try:
        yield from _sync(client, settings, state, stats, holder)
    except MediaWikiRateLimitedError as exc:
        failed("rate_limit")
        _save_progress(state, settings, holder)
        logger.warning("MediaWiki: rate limited (%s); the next run continues from here.", exc.code)
    except MediaWikiAuthError:
        failed("auth")
        raise
    except MediaWikiScopeError:
        failed("scope")
        raise
    except MediaWikiAPIError:
        failed("api")
        raise
    except Exception:
        failed("error")
        raise
    finally:
        stats["requests"] = client.requests


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _default_resource_name(api_url: str) -> str:
    parsed = urlparse(api_url)
    path = parsed.path.removesuffix("api.php")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{parsed.netloc}{path}".lower()).strip("_")
    return f"mediawiki_{slug}"


def mediawiki_source(
    api_url: str,
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
    user_agent: str = DEFAULT_USER_AGENT,
    maxlag: int | None = 5,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
):
    """Return a ``dlt`` resource yielding one document row per in-scope wiki page.

    Hand the result to ``cognee.remember(...)`` with
    ``write_disposition="merge"``, ``primary_key="id"`` and
    ``max_rows_per_table=0``. Configuration is passed explicitly; reading it
    from the environment is left to the caller (see ``examples/example.py``).

    Args:
        api_url: The wiki's ``api.php`` URL, e.g.
            ``https://en.wikipedia.org/w/api.php``.
        namespaces: Namespace ids to sync in full (``0`` is articles). Defaults
            to ``[0]`` when no ``categories`` or ``titles`` are given.
        categories: Sync the direct member pages of these categories (with or
            without the ``Category:`` prefix). A category that does not exist
            (no page and no members) raises ``MediaWikiScopeError``.
        titles: Sync these pages. Redirects are followed, so a renamed page
            stays selected. A title that does not exist raises
            ``MediaWikiScopeError``.
        content_format: ``"extracts"`` (TextExtracts plain text), ``"parse"``
            (rendered HTML reduced to text) or ``"auto"``: extracts when the
            wiki has the extension, parse otherwise.
        revision_history: How many recent edits (time, author, summary) to list
            in each document. ``0`` lists none and saves a request per page.
        overlap_seconds: How far before the previous run's server time the
            change feed is replayed, to catch entries written out of order.
        reconcile_after_days: Run a full enumeration at least this often
            (``None`` never does so unless needed). It also re-renders pages
            whose rendering changed without an edit (template changes).
        full_sync: Force a full enumeration and re-render every page on this run.
        username: Bot-password user name (``User@botname``) for a private wiki.
        password: Bot password.
        user_agent: User-Agent header. Wikimedia asks for contact details, e.g.
            ``"my-app/1.0 (me@example.com)"``.
        maxlag: The ``maxlag`` seconds sent with every request (``None`` to
            omit it); the wiki answers "too busy" above it and the request is
            retried after the ``Retry-After`` it sends, up to 60 seconds.
        resource_name: dlt resource / staging table name, also the key of the
            incremental state. Defaults to one derived from ``api_url``, so two
            wikis can sync into one dataset. Two sources sharing a name share
            state, so each would forget the other's pages.
        check_active: Optional host authorization checkpoint during extraction.
        service: Pre-built ``httpx.Client`` (a test injection point, or a
            client that already carries a private wiki's session cookie).
    """
    import dlt
    import httpx

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The MediaWiki connector requires a cognee build that reads per-row node "
            "sets (DOCUMENT_SYNC_VERSION >= 2, cognee 1.6.3+). Upgrade cognee."
        )
    resolved_api_url = (api_url or "").strip()
    parsed = urlparse(resolved_api_url)
    if parsed.scheme not in ("http", "https") or not parsed.path.endswith("api.php"):
        raise ValueError(
            "mediawiki_source needs the wiki's api.php URL, e.g. "
            "https://en.wikipedia.org/w/api.php."
        )
    if content_format not in _CONTENT_FORMATS:
        raise ValueError(f"content_format must be one of {_CONTENT_FORMATS}.")
    if revision_history < 0 or overlap_seconds < 0:
        raise ValueError("revision_history and overlap_seconds must not be negative.")
    if bool(username) != bool(password):
        raise ValueError("Pass both username and password (a bot password), or neither.")
    if not user_agent or not user_agent.strip():
        raise ValueError("user_agent must not be empty.")

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
    name = resource_name or _default_resource_name(resolved_api_url)
    stats: dict[str, int] = {}

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
            None if service is not None else httpx.Client(timeout=30.0, follow_redirects=True)
        )
        try:
            client = _MediaWikiClient(service or owned_client, resolved_api_url, user_agent, maxlag)
            if username:
                try:
                    client.login(username, password or "")
                except MediaWikiAuthError:
                    stats.clear()
                    stats.update(failed=1, failed_auth=1, requests=client.requests)
                    raise
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
    # Host-readable diagnostics: int counters only, never page content.
    resource.cognee_sync_stats = stats
    return resource
