"""Storyblok connector for cognee: a ``dlt`` source that turns a space's stories into memory.

Sync the stories of a Storyblok space (pages, posts, docs, help articles) into
cognee, incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_storyblok import storyblok_source

    await cognee.remember(
        storyblok_source(token=token),
        dataset_name="storyblok",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``: one per story
  (``story:<id>``: name, path, content type, tags, publish dates, and the text of
  its nested blocks and rich text fields).
* **Text only.** Field values are flattened to text; asset files, links, URLs and
  block metadata (``component``, ``_uid``, ``_editable``) are left out, and every
  text field is scrubbed.
* **Incremental.** Each run asks the space for its cache version first. While
  syncing published content and that version has not moved, nothing was published,
  unpublished or deleted, so the run stops there. Otherwise every story is listed and
  emitted again only when its rendered document changed.
* **Forget-on-delete.** A story missing from a complete listing (unpublished,
  deleted, or outside a new selection) is emitted as a hard-delete tombstone.
* **A run is bounded.** A long rate limit, a spent retry wait budget or a spent
  request budget stops the run cleanly, keeps what it emitted, and reports why in
  ``cognee_sync_stats``.

Privacy
-------
A preview token also reads unpublished drafts. Nothing is fetched until you
construct a source and call ``remember``, and anyone with read access to the target
dataset can read what was ingested.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_storyblok.redact import scrub

logger = get_logger("storyblok_connector")

# The Content Delivery API host of each Storyblok region.
REGION_HOSTS = {
    "eu": "https://api.storyblok.com",
    "us": "https://api-us.storyblok.com",
    "ca": "https://api-ca.storyblok.com",
    "ap": "https://api-ap.storyblok.com",
    "cn": "https://app.storyblokchina.cn",
}
DEFAULT_APP_URL = "https://app.storyblok.com"
# Requests per run; a bigger space finishes over several runs.
DEFAULT_MAX_REQUESTS = 1000
# Total time one run may wait on retries before it stops cleanly.
DEFAULT_MAX_SLEEP = 300.0
# The largest page the stories endpoint allows.
_PAGE_SIZE = 100
# The CDN usually answers in well under a second; only a stuck connection hits this.
_TIMEOUT = 30.0
# Attempts per request; backing off 1, 2, 4 and 8 seconds gives up after about 15.
_RETRIES = 5
# A longer Retry-After stops the run instead of waiting it out.
_MAX_RETRY_SLEEP = 60.0
# Waits are slept in steps this long, asking the host's check_active in between.
_WAIT_STEP = 1.0
# Block keys that describe the block instead of holding its text.
_META_KEYS = {
    "_uid",
    "_editable",
    "component",
    "id",
    "uuid",
    "fieldtype",
    "linktype",
    "filename",
    "url",
    "cached_url",
    "anchor",
    "target",
    "plugin",
    "src",
    "focus",
    "meta_data",
}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Rich text nodes that end a paragraph of their own.
_BLOCK_NODES = {"paragraph", "heading", "blockquote", "code_block", "list_item"}
_STATS = (
    "scanned",
    "emitted",
    "unchanged",
    "deleted",
    "redacted",
    "failed",
    "failed_auth",
    "failed_budget",
    "failed_rate_limit",
)


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the token or bodies.
# ---------------------------------------------------------------------------
class StoryblokSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class StoryblokAuthError(StoryblokSourceError):
    """Storyblok rejected the access token (HTTP 401)."""


class StoryblokRateLimitedError(StoryblokSourceError):
    """Storyblok kept answering HTTP 429, asked for a long wait, or the wait budget is spent."""


class StoryblokAPIError(StoryblokSourceError):
    """Storyblok answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _retry_after(response: Any) -> float | None:
    try:
        return max(float(response.headers.get("Retry-After")), 0.0)
    except (TypeError, ValueError):
        return None


def _token_transport(token: str, inner: Any) -> Any:
    """A transport that adds ``token`` to the query string on the way out.

    The Content Delivery API only takes its token as a query parameter. Adding it
    here keeps it out of the request httpx logs ("HTTP Request: GET ...") and puts
    in its exceptions.
    """
    import httpx

    class _TokenTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            signed = httpx.Request(
                request.method,
                request.url.copy_merge_params({"token": token}),
                headers=request.headers,
                stream=request.stream,
                extensions=request.extensions,
            )
            return inner.handle_request(signed)

        def close(self) -> None:
            inner.close()

    return _TokenTransport()


class StoryblokClient:
    """Minimal synchronous client for the Storyblok Content Delivery API v2.

    ``get`` returns the decoded JSON body. Tests and hosts can pass any object
    with the same method to :func:`storyblok_source` instead.
    """

    def __init__(
        self,
        token: str,
        api_url: str = REGION_HOSTS["eu"],
        *,
        transport: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_sleep: float = DEFAULT_MAX_SLEEP,
        check_active: Callable[[], None] | None = None,
    ):
        token = (token or "").strip()
        if not token or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("The Storyblok access token is empty or has an invalid format")
        self.api_url = api_url.rstrip("/")
        self._token = token
        # The transport under the token layer; tests pass a MockTransport.
        self._transport = transport
        self._http: Any = None
        self._sleep = sleep
        self._max_sleep = max_sleep
        self._slept = 0.0
        self._check_active = check_active

    def __repr__(self) -> str:
        return "StoryblokClient(token=<redacted>)"

    def reset_sleep_budget(self) -> None:
        """Start a new run's retry wait budget."""
        self._slept = 0.0

    def close(self) -> None:
        """Close the HTTP client; the next request opens a new one."""
        if self._http is not None:
            self._http.close()
            self._http = None

    def _wait(self, delay: float) -> None:
        if self._slept + delay > self._max_sleep:
            raise StoryblokRateLimitedError("Storyblok retries used up the wait budget of this run")
        self._slept += delay
        # Slept in short steps, asking the host in between, so revoked access stops
        # a long wait within a second instead of at its end.
        left = delay
        while True:
            if self._check_active is not None:
                self._check_active()
            if left <= 0:
                return
            step = min(_WAIT_STEP, left)
            self._sleep(step)
            left -= step

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import httpx

        if self._http is None:
            inner = self._transport or httpx.HTTPTransport()
            self._http = httpx.Client(
                timeout=_TIMEOUT, transport=_token_transport(self._token, inner)
            )
        attempt = 0
        while True:
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(self.api_url + path, params=params)
            except httpx.TransportError as exc:
                if last:
                    raise StoryblokAPIError(
                        f"Storyblok request failed: {type(exc).__name__}"
                    ) from None
                self._wait(float(2**attempt))
                attempt += 1
                continue
            status = response.status_code
            if status == 429:
                wait = _retry_after(response)
                if last or (wait is not None and wait > _MAX_RETRY_SLEEP):
                    raise StoryblokRateLimitedError("Storyblok rate limit exceeded (HTTP 429)")
                self._wait(wait if wait is not None else float(2**attempt))
            elif status >= 500 and not last:
                wait = _retry_after(response)
                self._wait(min(wait if wait is not None else float(2**attempt), _MAX_RETRY_SLEEP))
            elif status == 401:
                raise StoryblokAuthError("Storyblok rejected the access token (HTTP 401)")
            elif status != 200:
                raise StoryblokAPIError(f"Storyblok request failed: HTTP {status}", status=status)
            else:
                return response.json()
            attempt += 1


# ---------------------------------------------------------------------------
# Rendering: story text flattened from its blocks, deterministic.
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return str(value or "").strip()


def _inline(node: Any) -> str:
    """The text of a rich text node and its children, on one line."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    if node.get("type") == "hard_break":
        return " "
    return "".join(_inline(child) for child in node.get("content") or [])


def _richtext(node: dict) -> Iterator[str]:
    """Paragraphs of a rich text document, including blocks embedded in it."""
    for child in node.get("content") or []:
        if not isinstance(child, dict):
            continue
        kind = child.get("type")
        if kind == "blok":
            attrs = child.get("attrs") if isinstance(child.get("attrs"), dict) else {}
            yield from _texts(attrs.get("body"))
        elif kind in _BLOCK_NODES:
            line = _inline(child).strip()
            if line:
                yield line
        else:
            # Lists, tables and other containers: their paragraphs, in order.
            yield from _richtext(child)


def _texts(value: Any, key: str | None = None) -> Iterator[str]:
    """Every text a field holds, in field order, without block metadata or links."""
    if key in _META_KEYS:
        return
    if isinstance(value, str):
        text = value.strip()
        if text and not text.startswith(("http://", "https://", "//")) and not _UUID.match(text):
            yield text
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)
    elif isinstance(value, dict):
        if value.get("type") == "doc":
            yield from _richtext(value)
        elif value.get("fieldtype") == "asset":
            # An image or file: its description, not its address.
            for field in ("alt", "title"):
                if _text(value.get(field)):
                    yield _text(value[field])
        elif value.get("fieldtype") == "multilink":
            return
        else:
            for field, item in value.items():
                yield from _texts(item, field)


def _date(value: Any) -> str:
    text = _text(value)
    # Storyblok answers ISO 8601 ("2026-10-31T18:20:01.123Z"); minutes are enough.
    return f"{text[:10]} {text[11:16]} UTC" if len(text) >= 16 else text


def render_story(story: dict, space_id: Any, app_url: str = DEFAULT_APP_URL) -> dict[str, Any]:
    """A story's document: its details, then the text of its content."""
    content = story.get("content") if isinstance(story.get("content"), dict) else {}
    tags = sorted({_text(t) for t in story.get("tag_list") or [] if _text(t)})
    fields = [
        ("Story", _text(story.get("name"))),
        ("Path", _text(story.get("full_slug"))),
        ("Content type", _text(content.get("component"))),
        (
            "Language",
            _text(story.get("lang")) if story.get("lang") not in (None, "default") else "",
        ),
        ("Tags", ", ".join(tags)),
        ("First published", _date(story.get("first_published_at"))),
        ("Last published", _date(story.get("published_at"))),
    ]
    parts = [f"{label}: {value}" for label, value in fields if value]
    body = list(dict.fromkeys(_texts(content)))
    if body:
        parts.extend(["", *body])
    story_id = story["id"]
    name = _text(story.get("name")) or _text(story.get("full_slug")) or str(story_id)
    return {
        "id": f"story:{story_id}",
        "title": f"Storyblok story: {name}",
        "content": "\n".join(parts),
        "url": f"{app_url.rstrip('/')}/#/me/spaces/{space_id}/stories/0/0/{story_id}",
        "_deleted": False,
    }


def _scrubbed(row: dict[str, Any], stats: dict[str, int]) -> dict[str, Any]:
    for key in ("title", "content"):
        row[key], found = scrub(row[key])
        stats["redacted"] += found
    return row


def _digest(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _StoryblokConfig:
    version: str = "published"
    starts_with: str | None = None
    content_type: str | None = None
    with_tag: str | None = None
    language: str | None = None
    full_resync: bool = False
    app_url: str = DEFAULT_APP_URL
    max_requests: int = DEFAULT_MAX_REQUESTS


class _CutShortError(Exception):
    """Internal: stop the run here, keeping what was emitted so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Walker:
    """One run over the space's stories, writing progress into ``state``."""

    def __init__(
        self,
        client: Any,
        config: _StoryblokConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.known: dict[str, str] = state.setdefault("stories", {})
        self.requests = 0
        # Set once the first listing page is in; the budget never stops a run before that.
        self.progressed = False

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.progressed and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.get(path, params)
        except StoryblokRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except StoryblokAuthError:
            # Revoked during the run: keep what is done. Before any progress it is a
            # wrong token, which raises.
            if self.progressed:
                raise _CutShortError("auth") from None
            raise

    def _space(self) -> tuple[Any, Any]:
        """The space id and cache version, binding this state to one space."""
        body = self._get("/v2/cdn/spaces/me")
        space = body.get("space") if isinstance(body, dict) else None
        if not isinstance(space, dict) or space.get("id") in (None, ""):
            raise StoryblokAPIError("Storyblok answered /spaces/me without a space")
        known = self.state.get("space")
        if known is not None and known != space["id"]:
            raise ValueError(
                "This sync was made with another Storyblok space. Pass a resource_name "
                "of its own for each space."
            )
        self.state["space"] = space["id"]
        return space["id"], space.get("version")

    def rows(self) -> Iterator[dict]:
        space_id, cache_version = self._space()
        if (
            self.config.version == "published"
            and cache_version is not None
            and cache_version == self.state.get("cv")
            and not self.state.get("empty_listing")
            and not self.config.full_resync
        ):
            # Nothing was published, unpublished or deleted since the last run.
            self.stats["unchanged"] = len(self.known)
            return
        params: dict[str, Any] = {"version": self.config.version, "per_page": _PAGE_SIZE}
        if cache_version is not None:
            params["cv"] = cache_version
        for name, value in (
            ("starts_with", self.config.starts_with),
            ("content_type", self.config.content_type),
            ("with_tag", self.config.with_tag),
            ("language", self.config.language),
        ):
            if value:
                params[name] = value
        seen: set[str] = set()
        page = 1
        while True:
            body = self._get("/v2/cdn/stories", {**params, "page": page})
            items = body.get("stories") if isinstance(body, dict) else None
            # A 200 without the list must not read as "every story was removed".
            if not isinstance(items, list):
                raise StoryblokAPIError("Storyblok answered without a story list")
            for story in items:
                if not isinstance(story, dict) or story.get("id") in (None, ""):
                    # An entry that cannot be told apart leaves the listing incomplete.
                    raise StoryblokAPIError("Storyblok listed a story without its id")
                story_id = str(story["id"])
                seen.add(story_id)
                self.stats["scanned"] += 1
                row = _scrubbed(render_story(story, space_id, self.config.app_url), self.stats)
                digest = _digest(row)
                if self.known.get(story_id) == digest and not self.config.full_resync:
                    self.stats["unchanged"] += 1
                    continue
                self.known[story_id] = digest
                self.stats["emitted"] += 1
                yield row
            self.progressed = True
            if len(items) < _PAGE_SIZE:
                break
            page += 1
        yield from self._forget_unlisted(seen)
        # Only a complete run moves the cache version, so a cut run lists again.
        self.state["cv"] = cache_version

    def _forget_unlisted(self, seen: set[str]) -> Iterator[dict]:
        gone = [story_id for story_id in self.known if story_id not in seen]
        if gone and not seen and not self.state.get("empty_listing"):
            # A listing that is empty as a whole is confirmed by the next run.
            self.state["empty_listing"] = True
            logger.warning(
                "Storyblok listed no stories although %d were synced before; nothing is "
                "forgotten until the next run confirms it.",
                len(gone),
            )
            return
        self.state.pop("empty_listing", None)
        for story_id in gone:
            del self.known[story_id]
            self.stats["deleted"] += 1
            yield {"id": f"story:{story_id}", "_deleted": True}


def _iter_rows(
    client: Any,
    config: _StoryblokConfig,
    state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict.

    A rejected token raises :class:`StoryblokAuthError`, which is never read as "gone".
    """
    for key in _STATS:
        stats.setdefault(key, 0)
    reset = getattr(client, "reset_sleep_budget", None)
    if reset is not None:
        reset()
    walker = _Walker(client, config, state, stats, check_active)
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        # Every cut leaves the sync incomplete, so failed is set; failed_<reason> says why.
        # Nothing is forgotten from an incomplete listing.
        stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning(
            "Storyblok sync stopped early (%s); it continues on the next run.", cut.reason
        )
        return
    logger.info(
        "Storyblok: %d story(ies) listed, %d document(s) emitted, %d deleted.",
        stats["scanned"],
        stats["emitted"],
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
# dlt table names stay readable up to this length; the merged Asana connector uses it too.
_MAX_RESOURCE_NAME = 64


def _default_resource_name(config: _StoryblokConfig) -> str:
    name = "storyblok"
    scope = [
        config.version,
        config.starts_with,
        config.content_type,
        config.with_tag,
        config.language,
    ]
    if scope != ["published", None, None, None, None]:
        # Two differently selected syncs into one dataset must not share state and cleanup.
        name += "_" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()[:8]
    return name


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that scope,
    and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share a table.
    """
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > _MAX_RESOURCE_NAME:
        raise ValueError(f"resource_name must be at most {_MAX_RESOURCE_NAME} characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")


def _optional(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, or None")
    return value.strip()


def storyblok_source(
    *,
    token: str | None = None,
    region: str = "eu",
    version: str = "published",
    starts_with: str | None = None,
    content_type: str | None = None,
    with_tag: str | None = None,
    language: str | None = None,
    full_resync: bool = False,
    api_url: str | None = None,
    app_url: str = DEFAULT_APP_URL,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per Storyblok story.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        token: A space access token (Settings, Access Tokens): public for published
            content, preview to also read drafts.
        region: The space's region: ``eu``, ``us``, ``ca``, ``ap`` or ``cn``.
        version: ``"published"`` (default) or ``"draft"``, which needs a preview token.
        starts_with: Only stories under this path, for example ``"blog/"``.
        content_type: Only stories of this content type (component name).
        with_tag: Only stories with this tag.
        language: Only this language's translation of each story.
        full_resync: Emit every story again.
        api_url: Content Delivery API address, overriding ``region``.
        app_url: Storyblok app address used for links.
        max_requests: Request budget of one run, checked once the first page is in.
            A spent budget sets ``failed`` and ``failed_budget``.
        resource_name: Staging table name, which scopes this sync's state and
            cleanup. Defaults to ``storyblok`` (plus a hash of the selection when it
            is not the default). Give each space its own name. A name you pass must be
            lowercase snake_case of at most 64 characters.
        check_active: Optional host authorization checkpoint, called before each
            request and around each row, and in the client built here during every
            retry wait.
        service: Pre-built client (see :class:`StoryblokClient`). Mainly an injection
            point for tests and hosts. ``client`` is accepted as an alias.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Storyblok connector requires dlt: pip install "cognee[dlt]".'
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Storyblok sync requires a cognee build with table-scoped DLT document cleanup."
        )
    if not isinstance(region, str) or region.strip().lower() not in REGION_HOSTS:
        raise ValueError(f"region must be one of {', '.join(REGION_HOSTS)}")
    if version not in ("published", "draft"):
        raise ValueError('version must be "published" or "draft"')
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
        raise ValueError("max_requests must be a whole number of at least 1")

    config = _StoryblokConfig(
        version=version,
        starts_with=_optional("starts_with", starts_with),
        content_type=_optional("content_type", content_type),
        with_tag=_optional("with_tag", with_tag),
        language=_optional("language", language),
        full_resync=bool(full_resync),
        app_url=app_url,
        max_requests=max_requests,
    )
    name = resource_name
    if name is None:
        name = _default_resource_name(config)
    _check_resource_name(name)
    service = service or client
    owned = service is None
    if owned:
        if not token:
            raise ValueError(
                "A Storyblok access token is required: pass token=, or a pre-built service."
            )
        service = StoryblokClient(
            token,
            api_url or REGION_HOSTS[region.strip().lower()],
            check_active=check_active,
        )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def storyblok_documents():
        stats.clear()
        state = dlt.current.resource_state()
        rows = _iter_rows(service, config, state, stats, check_active)
        try:
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            # Also on errors and when dlt stops early; an injected service is the caller's.
            if owned:
                service.close()

    resource = storyblok_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "storyblok")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
