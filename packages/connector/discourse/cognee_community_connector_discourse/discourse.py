"""DLT source for Discourse forum topics (full-snapshot sync + forget-on-delete).

Lists the topics in scope (whole forum, categories, or tags), fetches each one as
markdown via ``/raw/{topic_id}`` and yields it as a dlt resource for cognee's
ingestion pipeline. One topic is one document: the opening post and every reply,
so a thread is remembered as a whole conversation.

Like the Notion connector, topics are ingested as *normal documents*: the source
declares ``cognee_document_source = "discourse"``, so ``resolve_dlt_sources``
tags each row ``external_metadata["source"] = "discourse"`` and routes it
through the standard cognify entity-extraction pipeline.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the topics currently listed each run. A deleted (or no longer
visible) topic drops out of Discourse's listings, so it is absent from the
snapshot and cognee's ``orphan_cleanup`` removes it from the graph and vector
stores. Unchanged topics keep a stable content-hash ``data_id``, so they are not
re-ingested or re-cognified.

Incremental fetch keeps that snapshot complete while skipping the expensive
part. Every run still lists every topic in scope (cheap: 30 topics per request,
metadata only) and yields every topic, but a topic's ``/raw`` body is only
re-fetched when its signature ``(bumped_at, posts_count, highest_post_number)``
changed since the last run; otherwise the content cached in dlt source state
is reused. A new reply, a deleted reply, or a bump changes the signature.

An edit to an *existing* post changes none of those fields, so it is invisible
to the signature. ``full_refresh_every`` (default 7) re-fetches every body on
every Nth run to pick such edits up, and ``force_full_refresh=True`` does it on
demand. The cache only advances when a run completes: dlt commits source
state with a successful load, so an aborted run leaves the previous cache (and
memory) untouched.
"""

import os
import time
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("discourse_connector")

# dlt resource / staging-table name for Discourse topics.
DISCOURSE_TABLE_NAME = "discourse_topics"
DISCOURSE_SOURCE_NAME = "discourse"

# /raw/{topic_id} returns at most this many posts per page.
RAW_PAGE_SIZE = 100
# /raw output ends every post with this separator line.
RAW_POST_SEPARATOR = "-------------------------"

# Retry budget for rate-limited / transient responses.
_MAX_RETRIES = 5
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
# Guard against a listing that keeps advertising more pages forever.
_MAX_LISTING_PAGES = 10_000

_EXTRA_HINT = (
    "The Discourse connector requires dlt and httpx: "
    'pip install "cognee-community-connector-discourse".'
)


class DiscourseAPIError(RuntimeError):
    """A non-retryable error response from the Discourse API."""

    def __init__(self, status: int, path: str, detail: str = ""):
        self.status = status
        self.path = path
        super().__init__(f"Discourse API {status} for {path}{': ' + detail if detail else ''}")


def discourse_source(
    base_url: str | None = None,
    api_key: str | None = None,
    api_username: str | None = None,
    category_ids: list[int] | None = None,
    tags: list[str] | None = None,
    include_subcategories: bool = True,
    request_delay: float = 0.0,
    incremental: bool = True,
    full_refresh_every: int | None = 7,
    force_full_refresh: bool = False,
    client: Any = None,
):
    """Create a dlt source that yields Discourse topics as markdown documents.

    Args:
        base_url: Forum URL, e.g. ``https://meta.discourse.org``. Falls back to
            ``DISCOURSE_BASE_URL``.
        api_key: API key for private forums. Falls back to ``DISCOURSE_API_KEY``.
            Public forums need no key.
        api_username: Username the API key acts as. Falls back to
            ``DISCOURSE_API_USERNAME``. Required together with ``api_key``.
        category_ids: Restrict ingestion to topics in these categories.
        tags: Restrict ingestion to topics with these tags. Combined with
            ``category_ids`` as a union; a topic matching both is ingested once.
            When neither is given, every topic in ``/latest`` is ingested.
        include_subcategories: Discourse lists a parent category together with
            all of its subcategories, which can be far larger than the parent
            alone. ``False`` restricts ``category_ids`` to the categories
            themselves.
        request_delay: Seconds to wait before each request, to stay polite on
            shared forums.
        incremental: Re-fetch only topics whose signature changed since the
            last run, reusing cached content for the rest. ``False`` fetches
            every topic body on every run and keeps no cache.
        full_refresh_every: With ``incremental``, re-fetch every topic body on
            every Nth run, so edits to existing posts (which don't change the
            signature) are picked up. ``None`` disables periodic refreshes.
        force_full_refresh: Re-fetch every topic body on this run.
        client: Pre-built :class:`DiscourseClient` (mainly a test-injection
            point); when omitted one is built from the arguments above.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if full_refresh_every is not None and full_refresh_every < 1:
        raise ValueError("full_refresh_every must be a positive integer or None.")

    if client is None:
        resolved_url = base_url or os.environ.get("DISCOURSE_BASE_URL")
        if not resolved_url:
            raise ValueError(
                "Discourse forum URL required: pass base_url= or set DISCOURSE_BASE_URL."
            )
        resolved_key = api_key or os.environ.get("DISCOURSE_API_KEY")
        resolved_user = api_username or os.environ.get("DISCOURSE_API_USERNAME")
        if bool(resolved_key) != bool(resolved_user):
            raise ValueError(
                "Discourse API auth needs both api_key and api_username "
                "(or DISCOURSE_API_KEY and DISCOURSE_API_USERNAME); omit both for public forums."
            )
        client = DiscourseClient(
            resolved_url,
            api_key=resolved_key,
            api_username=resolved_user,
            request_delay=request_delay,
        )

    @dlt.resource(name=DISCOURSE_TABLE_NAME, primary_key="id", write_disposition="replace")
    def discourse_topics():
        # Full-snapshot sync: each run replaces staging with exactly the topics
        # currently listed. Deleted topics fall out of staging and cognee's
        # orphan_cleanup forgets them.
        #
        # Errors are NOT swallowed (except a topic that is gone between listing
        # and fetching): staging is authoritative under replace, so a topic
        # missing from a partial snapshot would be forgotten as if deleted.
        # Aborting leaves staging — and memory — untouched.
        #
        # Incremental fetch never skips a *topic*, only its body fetch: every
        # listed topic is still yielded, so the snapshot stays authoritative.
        # The cache lives in *source* state: dlt resets resource state on every
        # run of a replace resource. cognee runs every dlt source through one
        # shared pipeline, so the cache is keyed by forum + scope.
        forums = dlt.current.source_state().setdefault("forums", {})
        cache_key = _cache_key(client.base_url, category_ids, tags, include_subcategories)
        cache = forums.get(cache_key) or {}
        cached_topics = cache.get("topics") or {}
        full = _is_full_refresh(cache, incremental, full_refresh_every, force_full_refresh)

        new_topics = {}
        fetched = reused = 0
        for topic in _iter_topics(client, category_ids, tags, include_subcategories):
            topic_id = str(topic["id"])
            signature = _signature(topic)
            cached = cached_topics.get(topic_id)
            if not full and cached and cached.get("signature") == signature:
                content = cached["content"]
                reused += 1
            else:
                content = _fetch_topic_markdown(client, topic["id"])
                if content is None:
                    continue
                fetched += 1
            new_topics[topic_id] = {"signature": signature, "content": content}
            yield _topic_to_row(client.base_url, topic, content)

        if incremental:
            # Rebuilt from this run's topics only, so deleted topics leave the cache.
            forums[cache_key] = {
                "topics": new_topics,
                "syncs_since_full_refresh": (
                    0 if full else cache.get("syncs_since_full_refresh", 0) + 1
                ),
            }
        logger.info(
            "Discourse: synced %d topic(s) (%d fetched, %d unchanged)%s.",
            fetched + reused,
            fetched,
            reused,
            " [full refresh]" if full and incremental else "",
        )

    @dlt.source(name=DISCOURSE_SOURCE_NAME)
    def _discourse():
        return discourse_topics

    source = _discourse()
    # Opt into the document ingestion path (topic → text document → cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, DISCOURSE_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class DiscourseClient:
    """Minimal Discourse API client with retry on rate limits and server errors.

    ``get`` returns the final response (2xx or a non-retryable error status);
    callers decide what each status means. Rate-limit (429), server (5xx),
    timeout and network errors are retried with backoff, honouring
    ``Retry-After``.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_username: str | None = None,
        request_delay: float = 0.0,
        timeout: float = 30.0,
        transport: Any = None,
    ):
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        self.base_url = base_url.rstrip("/")
        self.request_delay = request_delay
        headers = {
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-discourse",
        }
        if api_key:
            headers["Api-Key"] = api_key
            headers["Api-Username"] = api_username or ""
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def get(self, path: str, params: dict | None = None):
        import httpx

        for attempt in range(_MAX_RETRIES):
            if self.request_delay:
                time.sleep(self.request_delay)
            last_attempt = attempt == _MAX_RETRIES - 1
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise
                delay = float(2**attempt)
                logger.warning(
                    "Discourse: %s — retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue

            if response.status_code in _TRANSIENT_STATUSES and not last_attempt:
                delay = _retry_after(response.headers, attempt)
                logger.warning(
                    "Discourse: HTTP %d for %s — retrying in %.1fs (%d/%d).",
                    response.status_code,
                    path,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def close(self) -> None:
        self._http.close()


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


# ---------------------------------------------------------------------------
# Listing and fetching (module-private)
# ---------------------------------------------------------------------------


def _listing_paths(category_ids, tags, include_subcategories: bool = True) -> list[str]:
    """Listing endpoints for the configured scope (``/latest`` when unscoped).

    ``/c/{id}.json`` lists a category together with its subcategories;
    ``/c/{id}/none.json`` lists only the category itself.
    """
    suffix = ".json" if include_subcategories else "/none.json"
    paths = [f"/c/{int(category_id)}{suffix}" for category_id in category_ids or []]
    paths += [f"/tag/{tag}.json" for tag in tags or []]
    return paths or ["/latest.json"]


def _iter_topics(client, category_ids, tags, include_subcategories: bool = True):
    """Yield each topic in scope once, following every listing's pagination."""
    seen: set[int] = set()
    for path in _listing_paths(category_ids, tags, include_subcategories):
        for topic in _paginate_listing(client, path):
            if topic.get("archetype", "regular") != "regular":
                continue  # private messages, if an admin key exposes them
            if topic["id"] in seen:
                continue
            seen.add(topic["id"])
            yield topic


def _paginate_listing(client, path: str):
    """Yield raw topic objects across a listing's ``page=`` pagination."""
    for page in range(_MAX_LISTING_PAGES):
        response = client.get(path, params={"page": page})
        if response.status_code != 200:
            raise _api_error(response, path)
        topic_list = (response.json() or {}).get("topic_list") or {}
        topics = topic_list.get("topics") or []
        yield from topics
        # Discourse only advertises a next page when there is one; asking past
        # the end returns HTTP 400, so stop on the missing link (or an empty page).
        if not topics or not topic_list.get("more_topics_url"):
            return
    logger.warning("Discourse: stopped %s after %d pages.", path, _MAX_LISTING_PAGES)


def _fetch_topic_markdown(client, topic_id: int) -> str | None:
    """Return a topic's posts as markdown, or ``None`` if the topic is gone.

    ``/raw/{id}`` pages through posts ``RAW_PAGE_SIZE`` at a time; a page past
    the end comes back empty. A 404/403 means the topic was deleted or hidden
    after it was listed, so skipping it (and letting it be forgotten) is
    correct. Any other error propagates so the run aborts.
    """
    path = f"/raw/{topic_id}"
    chunks: list[str] = []
    page = 1
    while True:
        response = client.get(path, params={"page": page} if page > 1 else None)
        if response.status_code in (403, 404):
            if page == 1:
                logger.warning("Discourse: topic %s is gone, skipping.", topic_id)
                return None
            break
        if response.status_code != 200:
            raise _api_error(response, path)

        text = response.text.strip()
        if not text:
            break
        chunks.append(text)
        if text.count(RAW_POST_SEPARATOR) < RAW_PAGE_SIZE:
            break
        page += 1
    return "\n\n".join(chunks)


def _signature(topic: dict) -> list:
    """Cheap change marker from the listing: new/deleted replies and bumps move it.

    A list (not a tuple) so it compares equal after a round-trip through dlt state.
    """
    return [topic.get("bumped_at"), topic.get("posts_count"), topic.get("highest_post_number")]


def _cache_key(base_url: str, category_ids, tags, include_subcategories: bool = True) -> str:
    """Identify one forum + scope inside the shared pipeline state."""
    categories = ",".join(str(c) for c in sorted(int(c) for c in category_ids or []))
    tag_list = ",".join(sorted(tags or []))
    subcategories = "" if include_subcategories else "|no-subcategories"
    return f"{base_url}|categories={categories}|tags={tag_list}{subcategories}"


def _is_full_refresh(cache: dict, incremental: bool, every: int | None, force: bool) -> bool:
    """Whether this run must re-fetch every topic body."""
    if force or not incremental or not cache.get("topics"):
        return True
    return every is not None and cache.get("syncs_since_full_refresh", 0) >= every - 1


def _topic_to_row(base_url: str, topic: dict, content: str) -> dict:
    """Flatten a listed topic + its markdown into a document row.

    Only ``id``/``url``/``title``/``content`` are kept, so activity counters
    (views, likes, bumped_at) that change without the text changing do not
    churn the content-hash data_id.
    """
    topic_id = topic["id"]
    slug = topic.get("slug") or "topic"
    return {
        "id": str(topic_id),
        "url": f"{base_url}/t/{slug}/{topic_id}",
        "title": topic.get("title") or "",
        "content": content,
    }


def _api_error(response, path: str) -> DiscourseAPIError:
    detail = ""
    try:
        errors = (response.json() or {}).get("errors")
        if errors:
            detail = "; ".join(map(str, errors))
    except Exception:
        detail = (response.text or "")[:200]
    if response.status_code in (401, 403):
        detail = (detail + " " if detail else "") + "(check the forum URL and API key/username)"
    return DiscourseAPIError(response.status_code, path, detail)
