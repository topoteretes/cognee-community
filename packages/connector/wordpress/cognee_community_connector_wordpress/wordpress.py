"""WordPress connector for cognee, a ``dlt`` source that turns a site into memory.

Sync posts, pages, custom post types and their comments from a WordPress site
(self-hosted or WordPress.com) into cognee, incrementally and with
forget-on-deletion::

    import cognee
    from cognee_community_connector_wordpress import wordpress_source

    await cognee.remember(
        wordpress_source(
            "https://blog.example.com",
            username="editor",
            application_password="abcd efgh ijkl mnop qrst uvwx",
        ),
        dataset_name="blog",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Transport**: the WordPress REST API over plain ``httpx`` (a core cognee
  dependency), so the connector adds no new dependency. The API root is
  discovered from the site URL: ``/wp-json/`` on self-hosted sites,
  ``?rest_route=`` on sites without pretty permalinks (from the ``Link``
  header the site advertises), and ``public-api.wordpress.com`` for
  WordPress.com sites. Public content needs no credentials; an Application
  Password (Users → Profile) reads private posts too. Requests are sequential
  and retried on HTTP 429 / 5xx and network errors, honouring ``Retry-After``.
* **Scope**: ``post_types`` (default posts and pages). Custom post types that
  plugins register (products, events, docs, ...) are resolved through
  ``/wp/v2/types``, so their REST route never has to be known up front.
  ``statuses`` (default ``publish``; ``private`` and others need an
  Application Password) and optional ``categories`` / ``tags`` slugs narrow it
  further. Password-protected posts are never ingested.
* **One document per item**, keyed by the post id (unique across every post
  type on a site). The rendered HTML is reduced to markdown-style text;
  author, dates, taxonomy terms (categories, tags, custom taxonomies) and the
  approved comments are part of the document. Each item carries the
  ``wordpress:<host>`` node set, so ``recall`` can be scoped to one site.
* **Incremental**: every run records the server's clock (the ``Date`` header).
  Later runs fetch what changed with ``modified_after`` (sent with an explicit
  UTC offset: WordPress compares it with the site-local ``post_modified``
  column, so a naive timestamp is read in the site's timezone), from that
  time minus an overlap window, since modification times have one-second
  resolution. An item is re-rendered only when its ``modified_gmt`` (or its
  comments) changed, so replaying the overlap is harmless.
* **Id sweep**: ``modified_after`` reports neither deletions nor posts it
  cannot see change: a trashed, unpublished or permanently deleted post simply
  stops appearing, and a post scheduled in the editor goes live without its
  modification time moving. So every run also lists ids and modification
  times only (``_fields=id,modified_gmt``, 100 per request). Ids that vanished
  are re-checked by id, and only confirmed ones become ``_deleted`` tombstones
  (offset pagination can skip an item while the site changes underneath).
  dlt drops tombstones on ``merge`` and cognee's ``orphan_cleanup`` removes
  them from the graph, vector and relational stores. Ids the sweep finds that
  ``modified_after`` missed are fetched. A sweep that comes back empty while
  items were known deletes nothing.
* **Comments**: an id-only sweep of approved comments re-renders the items
  whose comment set changed (new, deleted, unapproved). Editing a comment's
  text does not change its id, so that is picked up when the item changes, on
  a scope or rendering change, or by the periodic full re-check
  (``reconcile_after_days``).

Every API failure raises before the run's state is saved, so dlt keeps the
previous cursor and a later run retries.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("wordpress_connector")

SOURCE_TAG = "wordpress"
DEFAULT_USER_AGENT = (
    "cognee-community-connector-wordpress/0.1.0 (+https://github.com/topoteretes/cognee-community)"
)

# The REST API caps per_page at 100 (WP_REST_Controller::get_collection_params).
_PAGE_SIZE = 100
# Attempts per request on 429 / 5xx / network errors: our choice, so an
# overloaded site costs at most a few minutes before the run stops cleanly.
_MAX_RETRIES = 5
# Longest wait honoured for Retry-After. The sync runs while cognee holds its
# process-wide dlt staging lock, so a site asking for more stops the run cleanly
# (state untouched, retried next run) instead of sleeping inside the lock.
_MAX_RETRY_AFTER_SECONDS = 60.0
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
_WPCOM_API = "https://public-api.wordpress.com"
_ITEM_FIELDS = (
    "id,type,status,date_gmt,modified_gmt,link,title,content,excerpt,author,_links,_embedded"
)


class WordPressAPIError(RuntimeError):
    """The site answered with a REST error (``{"code": ..., "message": ...}``)."""

    def __init__(self, code: str, status: int | None = None, message: str = ""):
        detail = f" (HTTP {status})" if status else ""
        super().__init__(f"WordPress API error {code}{detail}: {message}".rstrip(": "))
        self.code = code
        self.status = status


class WordPressAuthError(WordPressAPIError):
    """The credentials were rejected. Never carries the password."""


class WordPressRateLimitedError(WordPressAPIError):
    """The site kept answering 429 beyond what a run waits."""


class WordPressScopeError(ValueError):
    """A configured post type, category or tag does not exist on the site."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Api:
    """How REST routes are spelled on one site."""

    flavor: str  # "wp-json", "rest_route" or "wpcom"
    root: str  # wp-json root URL, site URL (rest_route) or WordPress.com site id
    host: str

    def url(self, namespace: str, route: str) -> tuple[str, dict[str, str]]:
        namespace, route = namespace.strip("/"), route.strip("/")
        path = f"{namespace}/{route}"
        if self.flavor == "wpcom":
            # public-api.wordpress.com/<namespace>/sites/<site>/<route>
            return f"{_WPCOM_API}/{namespace}/sites/{self.root}/{route}", {}
        if self.flavor == "rest_route":
            return self.root, {"rest_route": f"/{path}"}
        return f"{self.root}{path}", {}


class _WordPressClient:
    """Thin, sequential wrapper over the REST API with retries and paging."""

    def __init__(
        self,
        http_client: Any,
        api: _Api,
        user_agent: str,
        auth: tuple[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = http_client
        self.api = api
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._auth = auth
        self._sleep = sleep
        self.requests = 0
        self.server_time: datetime | None = None

    @property
    def authenticated(self) -> bool:
        return self._auth is not None

    def get(self, route: str, params: dict[str, Any] | None = None, namespace="wp/v2"):
        """GET a route; return ``(json, headers)``."""
        url, base_params = self.api.url(namespace, route)
        return _get(self, url, {**base_params, **(params or {})})

    def paged(self, route: str, params: dict[str, Any], namespace: str = "wp/v2") -> Iterator[dict]:
        """Yield every item of a collection, page by page (``X-WP-TotalPages``)."""
        page = 1
        while True:
            items, headers = self.get(
                route, {**params, "per_page": _PAGE_SIZE, "page": page}, namespace
            )
            if not isinstance(items, list):
                raise WordPressAPIError("unexpected_response", None, f"{route} is not a list")
            yield from items
            try:
                total_pages = int(headers.get("x-wp-totalpages") or 0)
            except ValueError:
                total_pages = 0
            # Trust the page count when the site sends it: some hosts cap
            # per_page below 100, so a short page is not proof of the last one.
            if not items or (page >= total_pages if total_pages else len(items) < _PAGE_SIZE):
                return
            page += 1


def _get(client: _WordPressClient, url: str, params: dict[str, Any]) -> tuple[Any, Any]:
    import httpx

    attempt = 0
    while True:
        last_attempt = attempt >= _MAX_RETRIES - 1
        client.requests += 1
        try:
            response = client._http.get(
                url, params=params, headers=client._headers, auth=client._auth
            )
        except httpx.TransportError as exc:
            if last_attempt:
                raise
            _backoff(client, f"network error ({type(exc).__name__})", None, attempt, False)
            attempt += 1
            continue

        if client.server_time is None and response.headers.get("date"):
            # The site's own clock is the cursor, never this machine's.
            with contextlib.suppress(TypeError, ValueError):
                client.server_time = parsedate_to_datetime(response.headers["date"]).astimezone(
                    timezone.utc
                )

        status = response.status_code
        if (status == 429 or status >= 500) and not last_attempt:
            _backoff(client, f"HTTP {status}", response.headers, attempt, status == 429)
            attempt += 1
            continue
        if status >= 400:
            try:
                error = response.json()
            except ValueError:
                error = {}
            code = str(error.get("code") or f"http_{status}") if isinstance(error, dict) else ""
            message = str(error.get("message") or "") if isinstance(error, dict) else ""
            if status in (401, 403):
                error_class: type[WordPressAPIError] = WordPressAuthError
            elif status == 429:
                error_class = WordPressRateLimitedError
            else:
                error_class = WordPressAPIError
            raise error_class(code or f"http_{status}", status, message)
        try:
            return response.json(), response.headers
        except ValueError as exc:
            raise WordPressAPIError("invalid_json", status, "the response is not JSON") from exc


def _backoff(
    client: _WordPressClient, reason: str, headers: Any, attempt: int, rate_limit: bool
) -> None:
    delay = min(float(2**attempt), _MAX_RETRY_AFTER_SECONDS)
    if headers is not None:
        with contextlib.suppress(TypeError, ValueError):
            delay = max(float(headers.get("retry-after")), 0.0)
    if delay > _MAX_RETRY_AFTER_SECONDS:
        error_class = WordPressRateLimitedError if rate_limit else WordPressAPIError
        raise error_class(
            "retry_after_too_long",
            None,
            f"the site asked to wait {delay:.0f}s (more than {_MAX_RETRY_AFTER_SECONDS:.0f}s)",
        )
    logger.warning(
        "WordPress: %s, retrying in %.1fs (%d/%d).", reason, delay, attempt + 1, _MAX_RETRIES
    )
    client._sleep(delay)


# ---------------------------------------------------------------------------
# API discovery
# ---------------------------------------------------------------------------
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="https://api\.w\.org/"')


def _api_from_link(link: str, host: str) -> _Api:
    parsed = urlparse(link)
    route = dict(parse_qsl(parsed.query)).get("rest_route", "")
    if parsed.netloc == urlparse(_WPCOM_API).netloc and route.startswith("/sites/"):
        return _Api("wpcom", route.removeprefix("/sites/").strip("/"), host)
    if "rest_route" in dict(parse_qsl(parsed.query)):
        site = urlunparse(parsed._replace(query="", fragment=""))
        return _Api("rest_route", site, host)
    return _Api("wp-json", link if link.endswith("/") else f"{link}/", host)


def discover_api(http_client: Any, site_url: str, user_agent: str) -> _Api:
    """Find how a site serves its REST API, from its URL alone."""
    import httpx

    parsed = urlparse(site_url)
    host = parsed.netloc
    if host.endswith(".wordpress.com"):
        return _Api("wpcom", host, host)

    base = site_url.rstrip("/")
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    try:
        index = http_client.get(f"{base}/wp-json/", headers=headers)
        if index.status_code == 200 and "wp/v2" in (index.json().get("namespaces") or []):
            return _Api("wp-json", f"{base}/wp-json/", host)
    except (ValueError, AttributeError, httpx.HTTPError):
        pass

    # The site advertises its API root in a Link header on every page.
    page = http_client.get(f"{base}/", headers={"User-Agent": user_agent})
    match = _LINK_RE.search(page.headers.get("link", ""))
    if not match:
        raise WordPressAPIError(
            "rest_api_not_found",
            page.status_code,
            f"{site_url} does not advertise a WordPress REST API (is the REST API disabled?)",
        )
    return _api_from_link(match.group(1), host)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Settings:
    post_types: tuple[str, ...]
    statuses: tuple[str, ...]
    categories: tuple[str, ...]
    tags: tuple[str, ...]
    include_comments: bool
    overlap_seconds: int
    reconcile_after_days: float | None
    full_sync: bool

    def scope_fingerprint(self) -> list:
        return [
            sorted(self.post_types),
            sorted(self.statuses),
            sorted(self.categories),
            sorted(self.tags),
        ]

    def render_fingerprint(self) -> list:
        return [self.include_comments]


@dataclass(frozen=True)
class _PostType:
    slug: str
    label: str
    namespace: str
    rest_base: str
    taxonomies: tuple[str, ...]


@dataclass
class _Scope:
    types: list[_PostType]
    taxonomy_labels: dict[str, str]
    category_ids: list[int]
    tag_ids: list[int]

    def filters(self, post_type: _PostType) -> dict[str, str]:
        """Taxonomy filters for a type: only types that have the taxonomy are narrowed."""
        params: dict[str, str] = {}
        if self.category_ids and "category" in post_type.taxonomies:
            params["categories"] = ",".join(map(str, self.category_ids))
        if self.tag_ids and "post_tag" in post_type.taxonomies:
            params["tags"] = ",".join(map(str, self.tag_ids))
        return params


def _resolve_scope(client: _WordPressClient, settings: _Settings) -> _Scope:
    types, _ = client.get("types")
    if not isinstance(types, dict):
        raise WordPressAPIError("unexpected_response", None, "types is not an object")
    selected: list[_PostType] = []
    unknown = [slug for slug in settings.post_types if slug not in types]
    if unknown:
        raise WordPressScopeError(
            f"Unknown post type(s) {unknown}; this site exposes {sorted(types)} over REST."
        )
    for slug in settings.post_types:
        spec = types[slug]
        if not spec.get("rest_base"):
            raise WordPressScopeError(f"Post type {slug!r} has no REST route on this site.")
        selected.append(
            _PostType(
                slug=slug,
                label=str(spec.get("name") or slug),
                namespace=str(spec.get("rest_namespace") or "wp/v2"),
                rest_base=str(spec["rest_base"]),
                taxonomies=tuple(spec.get("taxonomies") or ()),
            )
        )

    labels: dict[str, str] = {}
    try:
        taxonomies, _ = client.get("taxonomies")
        if isinstance(taxonomies, dict):
            labels = {slug: str(spec.get("name") or slug) for slug, spec in taxonomies.items()}
    except WordPressAPIError as exc:
        logger.warning("WordPress: could not list taxonomies (%s); using their slugs.", exc.code)

    return _Scope(
        types=selected,
        taxonomy_labels=labels,
        category_ids=_term_ids(client, "categories", settings.categories),
        tag_ids=_term_ids(client, "tags", settings.tags),
    )


def _term_ids(client: _WordPressClient, route: str, slugs: tuple[str, ...]) -> list[int]:
    if not slugs:
        return []
    found = {
        term["slug"]: term["id"]
        for term in client.paged(route, {"slug": ",".join(slugs), "_fields": "id,slug"})
    }
    missing = [slug for slug in slugs if slug not in found]
    if missing:
        # A typo would otherwise sync nothing and, worse, look like every
        # previously synced item was deleted.
        raise WordPressScopeError(f"No {route} with slug(s) {missing} on this site.")
    return sorted(found.values())


def _status_params(settings: _Settings) -> dict[str, str]:
    return {"status": ",".join(settings.statuses)}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "form", "button"}
_SKIP_CLASSES = {"screen-reader-text", "sharedaddy", "jp-relatedposts", "wp-block-buttons"}
_VOID_TAGS = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "aside",
    "table",
    "ul",
    "ol",
    "dl",
    "dd",
    "dt",
    "figure",
    "figcaption",
    "caption",
    "details",
    "summary",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_INVISIBLE_RE = re.compile("[​‌‍⁠﻿]")


class _HTMLToText(HTMLParser):
    """Reduce rendered post HTML to readable markdown-style text.

    Dependency-free on purpose. Keeps paragraphs, headings (``#``), list
    items, quotes (``>``), code blocks (fenced), table rows (``|``) and image
    alt text; drops scripts, styles, embeds and share widgets.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._lists: list[list] = []  # [tag, counter]
        self._quote_depth = 0
        self._pre_depth = 0

    @staticmethod
    def _hidden(attrs: list[tuple[str, str | None]]) -> bool:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        style = (values.get("style") or "").replace(" ", "").lower()
        return bool(classes & _SKIP_CLASSES) or "display:none" in style or "hidden" in values

    def _newline(self) -> None:
        self._parts.append("\n" + "> " * self._quote_depth)

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
        elif tag in ("ul", "ol"):
            self._lists.append([tag, 0])
        elif tag == "li":
            marker = "- "
            if self._lists and self._lists[-1][0] == "ol":
                self._lists[-1][1] += 1
                marker = f"{self._lists[-1][1]}. "
            indent = "  " * max(len(self._lists) - 1, 0)
            self._newline()
            self._parts.append(indent + marker)
        elif tag == "blockquote":
            self._quote_depth += 1
            self._newline()
        elif tag == "pre":
            self._pre_depth += 1
            self._parts.append("\n```\n")
        elif tag == "br":
            self._newline()
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self._parts.append(f"[image: {alt}]")
        elif tag in ("td", "th"):
            self._parts.append(" | ")
        elif tag == "tr" or tag in _BLOCK_TAGS:
            self._newline()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is None and tag in ("br", "img"):
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag in ("ul", "ol") and self._lists:
            self._lists.pop()
            self._newline()
        elif tag == "blockquote":
            self._quote_depth = max(self._quote_depth - 1, 0)
            self._newline()
        elif tag == "pre":
            self._pre_depth = max(self._pre_depth - 1, 0)
            self._parts.append("\n```\n")
        elif tag in _HEADINGS or (tag in _BLOCK_TAGS and tag not in ("ul", "ol")):
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip_tag is not None:
            return
        if self._pre_depth:
            self._parts.append(data)
        else:
            self._parts.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        return _tidy("".join(self._parts))


def _tidy(text: str) -> str:
    lines: list[str] = []
    in_code = False
    for raw_line in _INVISIBLE_RE.sub("", text).splitlines():
        if raw_line.strip() == "```":
            in_code = not in_code
            lines.append("```")
            continue
        if in_code:
            lines.append(raw_line.rstrip())
            continue
        indent = re.match(r"^( *)(?:- |\d+\. )", raw_line)
        line = re.sub(r"[ \t\u00a0]+", " ", raw_line).strip()
        if indent and indent.group(1):
            line = indent.group(1) + line
        line = line.strip("| ").strip() if "|" in line else line
        if re.fullmatch(r"(> ?)+", line):
            line = ""
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> str:
    """Convert rendered WordPress HTML to plain markdown-style text."""
    parser = _HTMLToText()
    parser.feed(markup or "")
    parser.close()
    return parser.text()


def _plain(rendered: Any) -> str:
    """A ``{"rendered": "..."}`` title-like field as plain text."""
    value = rendered.get("rendered") if isinstance(rendered, dict) else rendered
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def _comments_hash(comment_ids: Iterable[int]) -> str:
    joined = ",".join(map(str, sorted(comment_ids)))
    return hashlib.sha1(joined.encode()).hexdigest()[:16]


def _comments_of(
    client: _WordPressClient, post_ids: list[int], fields: str, orderby: str
) -> list[dict]:
    """Approved comments of the given items, 100 items per query.

    Anonymously, a comments query that names a password-protected item fails
    as a whole (HTTP 401), so such a batch is split until the item is isolated;
    its comments are never read (the item itself is never ingested).
    """
    found: list[dict] = []

    def read(batch: list[int]) -> None:
        try:
            found.extend(
                client.paged(
                    "comments",
                    {
                        "post": ",".join(map(str, batch)),
                        "orderby": orderby,
                        "order": "asc",
                        "_fields": fields,
                    },
                )
            )
        except WordPressAuthError:
            if client.authenticated:
                raise  # credentials were verified up front: a real failure
            if len(batch) == 1:
                return
            middle = len(batch) // 2
            read(batch[:middle])
            read(batch[middle:])

    for start in range(0, len(post_ids), _PAGE_SIZE):
        read(post_ids[start : start + _PAGE_SIZE])
    return found


def _fetch_comments(client: _WordPressClient, post_ids: list[int]) -> dict[int, list[dict]]:
    """Approved comments of the given items, oldest first, grouped by item."""
    grouped: dict[int, list[dict]] = {post_id: [] for post_id in post_ids}
    fields = "id,post,parent,author_name,date_gmt,content"
    for comment in _comments_of(client, post_ids, fields, "date_gmt"):
        grouped.setdefault(comment.get("post"), []).append(comment)
    return grouped


def _render_row(
    item: dict,
    post_type: _PostType,
    scope: _Scope,
    comments: list[dict] | None,
    node_set: str,
) -> dict:
    embedded = item.get("_embedded") or {}
    header = [f"Type: {post_type.label}"]
    authors = [
        a.get("name") for a in embedded.get("author") or [] if isinstance(a, dict) and a.get("name")
    ]
    if authors:
        header.append("Author: " + ", ".join(authors))
    if item.get("date_gmt"):
        header.append(f"Published: {item['date_gmt']} UTC")
    if item.get("modified_gmt") and item.get("modified_gmt") != item.get("date_gmt"):
        header.append(f"Updated: {item['modified_gmt']} UTC")

    terms: dict[str, list[str]] = {}
    for group in embedded.get("wp:term") or []:
        for term in group if isinstance(group, list) else []:
            if isinstance(term, dict) and term.get("name"):
                taxonomy = term.get("taxonomy") or "terms"
                terms.setdefault(taxonomy, []).append(html.unescape(term["name"]))
    # Sorted: the embedded term order is not guaranteed between requests, and a
    # different order would change the content hash and re-extract the item.
    for taxonomy in sorted(terms):
        label = scope.taxonomy_labels.get(taxonomy, taxonomy)
        header.append(f"{label}: " + ", ".join(sorted(terms[taxonomy])))

    body = html_to_text((item.get("content") or {}).get("rendered") or "")
    if not body:
        body = html_to_text((item.get("excerpt") or {}).get("rendered") or "")

    sections = ["\n".join(header), body]
    if comments:
        lines = []
        for comment in comments:
            text = html_to_text((comment.get("content") or {}).get("rendered") or "")
            if not text:
                continue
            who = comment.get("author_name") or "Anonymous"
            when = f" ({comment['date_gmt']} UTC)" if comment.get("date_gmt") else ""
            reply = " (reply)" if comment.get("parent") else ""
            lines.append(f"- {who}{when}{reply}: " + text.replace("\n", " "))
        if lines:
            sections.append("## Comments\n" + "\n".join(lines))

    return {
        "id": str(item["id"]),
        "title": _plain(item.get("title")),
        "content": "\n\n".join(section for section in sections if section),
        "url": item.get("link") or "",
        "_deleted": False,
        dlt_utils.NODE_SET_COLUMN: [node_set],
    }


def _tombstone(item_id: int | str) -> dict:
    return {"id": str(item_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def _format_time(value: datetime) -> str:
    """UTC with an explicit offset: WordPress reads a naive time as site-local."""
    return value.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT) + "+00:00"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass
class _Run:
    client: _WordPressClient
    settings: _Settings
    scope: _Scope
    node_set: str
    previous: dict[int, dict]
    rerender: bool
    items: dict[int, dict] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    # Items already re-rendered by an earlier, rate-limited attempt at the same
    # re-render; they are compared as usual instead of rendered again.
    rerendered_before: frozenset[int] = frozenset()
    rendered: set[int] = field(default_factory=set)
    forgotten: set[int] = field(default_factory=set)
    keep_state: bool = False
    empty_runs: int = 0

    def forget(self, item_id: int) -> dict:
        self.items.pop(item_id, None)
        self.forgotten.add(item_id)
        self.stats["deleted"] += 1
        return _tombstone(item_id)

    def progress(self) -> dict[str, dict]:
        """The items state after a partial run: previous, updated by what was done."""
        items = {
            item_id: seen
            for item_id, seen in self.previous.items()
            if item_id not in self.forgotten
        }
        items.update(self.items)
        return {str(item_id): seen for item_id, seen in sorted(items.items())}

    def seen(self, item: dict, post_type: _PostType, comments_hash: str | None) -> dict:
        return {
            "modified": item.get("modified_gmt"),
            "type": post_type.slug,
            "comments": comments_hash,
        }


def _sweep(client: _WordPressClient, run: _Run, post_type: _PostType) -> dict[int, str]:
    """``{id: modified_gmt}`` of every item of a type currently in scope."""
    return {
        item["id"]: item.get("modified_gmt")
        for item in client.paged(
            post_type.rest_base,
            {
                **_status_params(run.settings),
                **run.scope.filters(post_type),
                "orderby": "id",
                "order": "asc",
                "_fields": "id,modified_gmt",
            },
            post_type.namespace,
        )
    }


def _comment_sweep(client: _WordPressClient, post_ids: list[int]) -> dict[int, list[int]]:
    """``{item id: comment ids}`` for the in-scope items only (never site-wide:
    a large blog can hold hundreds of thousands of comments)."""
    by_post: dict[int, list[int]] = {}
    for comment in _comments_of(client, sorted(post_ids), "id,post", "id"):
        by_post.setdefault(comment.get("post"), []).append(comment["id"])
    return by_post


def _fetch_items(
    client: _WordPressClient, run: _Run, post_type: _PostType, ids: list[int]
) -> list[dict]:
    """Full items by id (current scope filters applied), 100 per request."""
    items: list[dict] = []
    for start in range(0, len(ids), _PAGE_SIZE):
        batch = ids[start : start + _PAGE_SIZE]
        items.extend(
            client.paged(
                post_type.rest_base,
                {
                    **_status_params(run.settings),
                    **run.scope.filters(post_type),
                    "include": ",".join(map(str, batch)),
                    "_fields": _ITEM_FIELDS,
                    "_embed": "author,wp:term",
                },
                post_type.namespace,
            )
        )
    return items


def _full_sync_reason(state: dict, settings: _Settings, now: datetime) -> str | None:
    if not state.get("cursor"):
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
        or now - _parse_time(last_full) >= timedelta(days=settings.reconcile_after_days)
    ):
        return "periodic"
    return None


# A sweep that lists nothing twice in a row is taken at its word: the first time
# could be an outage, a second consecutive one almost never is, and the old items
# must not stay in memory forever.
_EMPTY_RUNS_BEFORE_FORGET = 2
_RERENDER_REASONS = ("render_changed", "requested", "periodic")


def _sync(
    client: _WordPressClient,
    settings: _Settings,
    state: dict,
    stats: dict[str, int],
    holder: dict,
):
    if client.authenticated:
        # WordPress serves a request with rejected credentials as anonymous, so
        # a wrong password would otherwise surface as a confusing "status is
        # forbidden" or, worse, a sync of public content only.
        try:
            client.get("users/me", {"_fields": "id"})
        except WordPressAPIError as exc:
            raise WordPressAuthError(
                exc.code, exc.status, "the Application Password was rejected"
            ) from exc
    scope = _resolve_scope(client, settings)
    # The first response carried the site's clock: anything modified from here
    # on is newer than this run's cursor.
    now = client.server_time or datetime.now(timezone.utc)
    previous = {int(item_id): seen for item_id, seen in (state.get("items") or {}).items()}
    reason = _full_sync_reason(state, settings, now)
    if reason:
        stats["full_sync"] = 1
        stats[f"full_sync_{reason}"] = 1

    rerender = reason in _RERENDER_REASONS
    progress = state.get("rerender_progress") or {}
    progress_key = [reason, settings.render_fingerprint(), settings.scope_fingerprint()]
    run = _Run(
        client=client,
        settings=settings,
        scope=scope,
        node_set=f"{SOURCE_TAG}:{client.api.host}",
        previous=previous,
        rerender=rerender,
        stats=stats,
        rerendered_before=frozenset(
            int(item_id)
            for item_id in progress.get("ids") or []
            if rerender and progress.get("for") == progress_key
        ),
    )
    holder.update(run=run, progress_key=progress_key)

    present: dict[int, tuple[_PostType, str]] = {}
    fetched: dict[int, tuple[_PostType, dict]] = {}

    for post_type in scope.types:
        if not reason:
            # The change feed: everything this type modified since the last
            # run (minus the overlap), with full content.
            since = _parse_time(state["cursor"]) - timedelta(seconds=settings.overlap_seconds)
            for item in client.paged(
                post_type.rest_base,
                {
                    **_status_params(settings),
                    **scope.filters(post_type),
                    "modified_after": _format_time(since),
                    "orderby": "modified",
                    "order": "asc",
                    "_fields": _ITEM_FIELDS,
                    "_embed": "author,wp:term",
                },
                post_type.namespace,
            ):
                fetched[item["id"]] = (post_type, item)
        for item_id, modified in _sweep(client, run, post_type).items():
            present[item_id] = (post_type, modified)

    stats["items_scanned"] = len(present)
    if previous and not present:
        empty_runs = int(state.get("empty_runs") or 0) + 1
        # An empty sweep could be a revoked password, a disabled REST route or
        # an outage, so the first one deletes nothing and moves no cursor. A
        # scope the caller just changed is believed right away, and so is the
        # second consecutive empty sweep: the old items must not stay forever.
        if reason != "scope_changed" and empty_runs < _EMPTY_RUNS_BEFORE_FORGET:
            logger.warning(
                "WordPress: the scope listed 0 items but %d were synced before; "
                "keeping them for now (forgotten if the next sweep is empty too).",
                len(previous),
            )
            run.keep_state = True
            run.empty_runs = empty_runs
            stats["failed"] = 1
            stats["failed_empty_scope"] = 1
            state["empty_runs"] = empty_runs
            return
        logger.warning(
            "WordPress: the scope is empty; forgetting the %d items synced before.",
            len(previous),
        )

    comment_sets: dict[int, list[int]] = (
        _comment_sweep(
            client,
            # Items known to be password-protected never have comments read.
            [i for i in present if not (previous.get(i) or {}).get("skipped")],
        )
        if settings.include_comments
        else {}
    )
    yield from _emit_changes(run, present, fetched, comment_sets)
    yield from _emit_deletions(run, present)

    # State is written only after the last row was yielded, so a run that
    # raises part-way keeps the previous cursor (dlt also discards the state of
    # a failed load).
    state["items"] = {str(item_id): seen for item_id, seen in sorted(run.items.items())}
    state["cursor"] = _format_time(now)
    state["scope"] = settings.scope_fingerprint()
    state["render"] = settings.render_fingerprint()
    if reason:
        state["last_full_sync"] = _format_time(now)
    state.pop("empty_runs", None)
    state.pop("rerender_progress", None)
    logger.info(
        "WordPress: %s sync, %d changed, %d unchanged, %d deleted, %d request(s).",
        "full" if reason else "incremental",
        stats["items_changed"],
        stats["items_unchanged"],
        stats["deleted"],
        client.requests,
    )


def _save_progress(state: dict, holder: dict) -> None:
    """After a rate-limited stop: keep what was done, so the next run continues.

    The cursor, scope and render settings are left alone (the next run replays
    the same window and repeats a due full sync), but the items already compared,
    rendered or forgotten are recorded, so it does not start over.
    """
    run: _Run | None = holder.get("run")
    if run is None or run.keep_state:
        return
    state["items"] = run.progress()
    if run.rerender:
        state["rerender_progress"] = {
            "for": holder.get("progress_key"),
            "ids": sorted(run.rendered | set(run.rerendered_before)),
        }


def sync_items(client: _WordPressClient, settings: _Settings, state: dict, stats: dict):
    """Yield changed items and deletion tombstones, then advance ``state``.

    ``stats`` receives plain int counters for the host, all present on every run:
    ``items_scanned`` (items listed by the sweep), ``items_changed``,
    ``items_unchanged``, ``deleted``, ``fetched_by_id``, ``requests``,
    ``full_sync`` (plus ``full_sync_<reason>``) and ``failed`` (plus
    ``failed_<reason>``: ``rate_limit`` and ``empty_scope`` finish the run;
    ``auth``, ``scope``, ``api`` and ``error`` also raise).

    A site that keeps rate-limiting the run stops it cleanly: what was yielded
    stays, the work done is recorded, and the next run continues from there.
    """
    stats.clear()
    stats.update(
        items_scanned=0,
        items_changed=0,
        items_unchanged=0,
        deleted=0,
        fetched_by_id=0,
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
    except WordPressRateLimitedError as exc:
        failed("rate_limit")
        _save_progress(state, holder)
        logger.warning("WordPress: rate limited (%s); the next run continues from here.", exc.code)
    except WordPressAuthError:
        failed("auth")
        raise
    except WordPressScopeError:
        failed("scope")
        raise
    except WordPressAPIError:
        failed("api")
        raise
    except Exception:
        failed("error")
        raise
    finally:
        stats["requests"] = client.requests


def _emit_changes(
    run: _Run,
    present: dict[int, tuple[_PostType, str]],
    fetched: dict[int, tuple[_PostType, dict]],
    comment_sets: dict[int, list[int]],
) -> Iterator[dict]:
    def comments_hash(item_id: int) -> str | None:
        if not run.settings.include_comments:
            return None
        return _comments_hash(comment_sets.get(item_id, []))

    stale: list[int] = []
    for item_id, (_post_type, modified) in present.items():
        before = run.previous.get(item_id) or {}
        # A skipped (password-protected) item only matters again once it is
        # edited, e.g. when its password is removed.
        comments_changed = not before.get("skipped") and before.get("comments") != comments_hash(
            item_id
        )
        forced = run.rerender and item_id not in run.rerendered_before
        if forced or not before or before.get("modified") != modified or comments_changed:
            stale.append(item_id)
        else:
            run.items[item_id] = before
            run.stats["items_unchanged"] += 1

    # Worked through in batches of one page of items, each yielded before the
    # next is fetched, so a run stopped by a rate limit keeps what it finished.
    types = {post_type.slug: post_type for post_type in run.scope.types}
    stale.sort()
    for start in range(0, len(stale), _PAGE_SIZE):
        batch = stale[start : start + _PAGE_SIZE]
        # Fetch whatever the change feed did not already return: on a full sync
        # that is every stale item; on an incremental run, only what the sweep
        # caught that modified_after cannot see (a scheduled post going live, a
        # new comment, a modification time tied with the cursor).
        missing: dict[str, list[int]] = {}
        for item_id in batch:
            if item_id not in fetched:
                missing.setdefault(present[item_id][0].slug, []).append(item_id)
        for slug, ids in missing.items():
            for item in _fetch_items(run.client, run, types[slug], sorted(ids)):
                fetched[item["id"]] = (types[slug], item)
        run.stats["fetched_by_id"] += sum(len(ids) for ids in missing.values())

        renderable: list[int] = []
        for item_id in batch:
            if item_id not in fetched:
                continue  # listed by the sweep but gone by the time it was fetched
            post_type, item = fetched[item_id]
            if not (item.get("content") or {}).get("protected"):
                renderable.append(item_id)
                continue
            # Password-protected: never ingested. Remembered as skipped so it is
            # not fetched again every run, and forgotten if it was ingested
            # before the password was set.
            before = run.previous.get(item_id) or {}
            if before and not before.get("skipped"):
                yield run.forget(item_id)
            run.items[item_id] = {**run.seen(item, post_type, None), "skipped": True}

        # Fetched only for items that will be rendered: anonymously, a comments
        # query that names a password-protected item fails as a whole (HTTP 401).
        comments = _fetch_comments(run.client, renderable) if run.settings.include_comments else {}
        for item_id in renderable:
            post_type, item = fetched[item_id]
            item_comments = comments.get(item_id, [])
            row = _render_row(item, post_type, run.scope, item_comments, run.node_set)
            run.items[item_id] = run.seen(
                item,
                post_type,
                _comments_hash(c["id"] for c in item_comments)
                if run.settings.include_comments
                else None,
            )
            run.rendered.add(item_id)
            run.stats["items_changed"] += 1
            yield row


def _emit_deletions(run: _Run, present: dict[int, tuple[_PostType, str]]) -> Iterator[dict]:
    candidates = [
        item_id
        for item_id in run.previous
        if item_id not in run.items and item_id not in run.forgotten
    ]
    if not candidates:
        return
    # Offset pagination can skip an item while the site changes underneath the
    # sweep, so a vanished id is confirmed by asking for it directly before it
    # is forgotten.
    still_there: set[int] = set()
    types = {post_type.slug: post_type for post_type in run.scope.types}
    by_type: dict[str, list[int]] = {}
    for item_id in candidates:
        slug = (run.previous[item_id] or {}).get("type")
        if slug in types and item_id not in present:
            by_type.setdefault(slug, []).append(item_id)
    for slug, ids in by_type.items():
        for item in _fetch_items(run.client, run, types[slug], sorted(ids)):
            if not (item.get("content") or {}).get("protected"):
                still_there.add(item["id"])

    for item_id in sorted(candidates):
        if item_id in still_there:
            run.items[item_id] = run.previous[item_id]
            continue
        if (run.previous[item_id] or {}).get("skipped"):
            continue  # never ingested, so there is nothing to forget
        # Gone, out of scope, or listed but no longer renderable (deleted
        # mid-run, or made password-protected).
        yield run.forget(item_id)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _default_resource_name(site_url: str) -> str:
    parsed = urlparse(site_url)
    slug = re.sub(r"[^a-z0-9]+", "_", f"{parsed.netloc}{parsed.path}".lower()).strip("_")
    return f"wordpress_{slug}"


def wordpress_source(
    site_url: str,
    *,
    post_types: list[str] | None = None,
    statuses: list[str] | None = None,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    include_comments: bool = True,
    username: str | None = None,
    application_password: str | None = None,
    overlap_seconds: int = 300,
    reconcile_after_days: float | None = 7,
    full_sync: bool = False,
    user_agent: str = DEFAULT_USER_AGENT,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
):
    """Return a ``dlt`` resource yielding one document row per in-scope item.

    Hand the result to ``cognee.remember(...)`` with
    ``write_disposition="merge"``, ``primary_key="id"`` and
    ``max_rows_per_table=0``. Configuration is passed explicitly; reading it
    from the environment is left to the caller (see ``examples/example.py``).

    Args:
        site_url: The site's address, e.g. ``https://blog.example.com`` or
            ``https://example.wordpress.com``.
        post_types: Post type slugs to sync. Defaults to ``["post", "page"]``.
            Custom post types work as long as the plugin exposes them over REST.
        statuses: Post statuses to sync. Defaults to ``["publish"]``; anything
            else (``private``, ``draft``, ...) needs an Application Password.
        categories: Category slugs. Types that have categories (posts) are
            narrowed to them; other types are synced in full.
        tags: Tag slugs, applied the same way.
        include_comments: Append each item's approved comments to its document.
        username: The WordPress user the Application Password belongs to.
        application_password: An Application Password (Users → Profile →
            Application Passwords). WordPress only accepts it over HTTPS (or on
            a local development site).
        overlap_seconds: How far before the previous run's server time the
            ``modified_after`` feed is replayed.
        reconcile_after_days: Re-render every item at least this often, which
            picks up edited comment text (``None`` to disable).
        full_sync: Re-render every item on this run.
        user_agent: User-Agent header.
        resource_name: dlt resource / staging table name, also the key of the
            incremental state. Defaults to one derived from ``site_url``, so
            several sites can sync into one dataset. Two sources sharing a
            name share state, so each would forget the other's items.
        check_active: Optional host authorization checkpoint during extraction.
        service: Pre-built ``httpx.Client`` (a test injection point, or a client
            that already carries a session).
    """
    import dlt
    import httpx

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The WordPress connector requires a cognee build that reads per-row node "
            "sets (DOCUMENT_SYNC_VERSION >= 2, cognee 1.6.3+). Upgrade cognee."
        )

    resolved_url = (site_url or "").strip()
    if urlparse(resolved_url).scheme not in ("http", "https") or not urlparse(resolved_url).netloc:
        raise ValueError("wordpress_source needs the site's URL, e.g. https://blog.example.com.")
    if bool(username) != bool(application_password):
        raise ValueError("Pass both username and application_password, or neither.")
    if not user_agent or not user_agent.strip():
        raise ValueError("user_agent must not be empty.")
    resolved_types = post_types or ["post", "page"]
    resolved_statuses = statuses or ["publish"]
    if set(resolved_statuses) - {"publish"} and not application_password and service is None:
        raise ValueError(
            f"Statuses {sorted(set(resolved_statuses) - {'publish'})} are only visible with "
            "an Application Password (pass username= and application_password=)."
        )
    if overlap_seconds < 0:
        raise ValueError("overlap_seconds must not be negative.")

    settings = _Settings(
        post_types=tuple(dict.fromkeys(t.strip() for t in resolved_types if t.strip())),
        statuses=tuple(sorted({s.strip() for s in resolved_statuses if s.strip()})),
        categories=tuple(sorted({c.strip() for c in categories or [] if c.strip()})),
        tags=tuple(sorted({t.strip() for t in tags or [] if t.strip()})),
        include_comments=include_comments,
        overlap_seconds=overlap_seconds,
        reconcile_after_days=reconcile_after_days,
        full_sync=full_sync,
    )
    auth = (username, application_password) if username and application_password else None
    name = resource_name or _default_resource_name(resolved_url)
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
    def wordpress_items():
        owned_client = (
            None if service is not None else httpx.Client(timeout=30.0, follow_redirects=True)
        )
        try:
            http = service or owned_client
            api = discover_api(http, resolved_url, user_agent)
            client = _WordPressClient(http, api, user_agent, auth)
            state = dlt.current.resource_state()
            site_key = f"{api.flavor}:{api.root}"
            if state.get("items") and state.get("site") not in (None, site_key):
                # Same resource name, different site: syncing on would tombstone
                # every item of the other site.
                raise ValueError(
                    f"WordPress resource {name!r} already holds items from another site "
                    "in this dataset. Give each site its own resource_name."
                )
            state["site"] = site_key
            yield from dlt_utils.guarded_rows(
                sync_items(client, settings, state, stats), check_active
            )
        finally:
            if owned_client is not None:
                owned_client.close()

    resource = wordpress_items()
    # Opt into the document ingestion path: each item row (id/title/content/url
    # plus cognee_node_set) becomes a text document that flows through normal
    # cognify. resolve_dlt_sources reads this marker; it never imports this
    # connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_TAG)
    # Keep the incremental state per (dataset, resource), so syncing the same
    # site into another dataset does not reuse this one's cursor.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    # Host-readable diagnostics: int counters only, never content.
    resource.cognee_sync_stats = stats
    return resource
