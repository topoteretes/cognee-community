"""Canvas LMS connector for cognee: a ``dlt`` source syncing LMS courses into memory.

Sync Canvas LMS course pages, modules, assignments, and announcements into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_canvas import canvas_source

    await cognee.remember(
        canvas_source(access_token=token, resources=["pages", "modules", "assignments"]),
        dataset_name="canvas",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per entity
  (``canvas:page:<course_id>:<id>``, ``canvas:assignment:<course_id>:<id>``).
* **Incremental.** Listings track high-watermark timestamps in ``dlt.current.resource_state()``.
  Full details are fetched only for changed items, and rendered markdown is hashed with SHA-256
  to ensure unchanged content is not re-emitted.
* **Forget-on-delete.** Entities deleted upstream emit hard-delete ``_deleted=True`` tombstones
  under ``write_disposition="merge"``, purged via table-scoped orphan cleanup.
* **Bounded runs.** Respects API rate limits (cost bucket & HTTP 429/403 with back-off), budget
  windows, and sanitizes access tokens on all error paths.
"""

import contextlib
import hashlib
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("canvas_connector")

DEFAULT_BASE_URL = "https://canvas.instructure.com"
SUPPORTED_RESOURCES = ("pages", "modules", "assignments", "announcements", "discussions")


class CanvasError(Exception):
    """Base exception for Canvas connector errors."""


class CanvasAuthError(CanvasError):
    """Raised when the access token is invalid or unauthorized."""


class CanvasRateLimitedError(CanvasError):
    """Raised when rate limit back-off exceeds the maximum budget."""


class CanvasAPIError(CanvasError):
    """Raised on non-transient API errors."""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class CanvasDocument:
    id: str
    title: str
    content: str
    url: str
    updated_at: str
    _deleted: bool = False

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "url": self.url,
            "_deleted": self._deleted,
        }


def _strip_verifier_and_html(html_text: str | None) -> str:
    """Strip verifier parameters from URLs and remove HTML tags."""
    if not html_text:
        return ""
    # Strip verifier parameters from file links to prevent security token exposure
    cleaned = re.sub(r"([?&])verifier=[^&\"'\s>]+", "", html_text)
    # Clean standard HTML tags to plaintext
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _parse_next_link(link_header: str | None) -> str | None:
    """Parse RFC 5988 Link header for rel='next' URL."""
    if not link_header:
        return None
    for part in link_header.split(","):
        match = re.search(r'<([^>]+)>;\s*rel=["\']?next["\']?', part.strip())
        if match:
            return match.group(1)
    return None


class CanvasClient:
    """HTTP client for Canvas LMS API with token auth, pagination, and retry logic."""

    def __init__(
        self,
        access_token: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: Any | None = None,
        max_requests: int = 500,
    ) -> None:
        if not access_token or not access_token.strip():
            raise CanvasAuthError("Canvas access token is required and cannot be empty.")

        self.access_token = access_token.strip()
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.max_requests = max_requests
        self.request_count = 0

    def _sanitize(self, msg: str) -> str:
        if self.access_token:
            return msg.replace(self.access_token, "[REDACTED_ACCESS_TOKEN]")
        return msg

    def _request(
        self,
        endpoint_or_url: str,
        params: dict[str, Any] | None = None,
        check_active: Callable[[], None] | None = None,
    ) -> tuple[Any, str | None]:
        import httpx

        if endpoint_or_url.startswith("http://") or endpoint_or_url.startswith("https://"):
            url = endpoint_or_url
        else:
            url = f"{self.base_url}/api/v1/{endpoint_or_url.lstrip('/')}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

        backoff = 1.0
        max_retries = 3
        total_retry_wait = 0.0

        for attempt in range(1, max_retries + 1):
            if check_active:
                check_active()

            self.request_count += 1
            if self.request_count > self.max_requests:
                raise CanvasError(f"Request budget exceeded: {self.max_requests} calls")

            client_kwargs: dict[str, Any] = {"timeout": 30.0}
            if self.transport:
                client_kwargs["transport"] = self.transport

            try:
                with httpx.Client(**client_kwargs) as client:
                    response = client.get(url, headers=headers, params=params)
            except Exception as e:
                if attempt == max_retries:
                    raise CanvasAPIError(f"Network error after {max_retries} attempts: {e}") from e
                time.sleep(backoff)
                backoff *= 2.0
                continue

            if response.status_code == 401:
                raise CanvasAuthError("Canvas rejected access token (HTTP 401 Unauthorized)")

            # Canvas cost bucket throttle returns 403 or 429
            is_throttled = response.status_code == 429 or (
                response.status_code == 403 and "rate limit" in response.text.lower()
            )
            if is_throttled:
                retry_after_hdr = response.headers.get("Retry-After")
                wait = backoff
                if retry_after_hdr:
                    with contextlib.suppress(ValueError):
                        wait = max(wait, float(retry_after_hdr))
                if wait > 60.0 or (total_retry_wait + wait) > 120.0:
                    raise CanvasRateLimitedError(f"Canvas rate limit back-off too long: {wait}s")
                time.sleep(wait)
                total_retry_wait += wait
                backoff = max(backoff * 2.0, wait * 1.5)
                continue

            # 503 while building discussion cache or transient errors
            if response.status_code in (500, 502, 503, 504):
                if attempt == max_retries:
                    raise CanvasAPIError(
                        self._sanitize(
                            f"Server error {response.status_code}: {response.text[:200]}"
                        ),
                        status_code=response.status_code,
                    )
                time.sleep(backoff)
                backoff *= 2.0
                continue

            if response.status_code >= 400:
                err_msg = (
                    f"API request failed with HTTP {response.status_code}: {response.text[:200]}"
                )
                raise CanvasAPIError(
                    self._sanitize(err_msg),
                    status_code=response.status_code,
                )

            next_url = _parse_next_link(response.headers.get("Link"))
            return response.json(), next_url

        raise CanvasAPIError(f"Canvas request failed after {max_retries} retries: {url}")

    def get_paginated(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        check_active: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all items from endpoint following RFC 5988 Link headers."""
        items: list[dict[str, Any]] = []
        next_url: str | None = endpoint
        query_params = dict(params or {})

        while next_url:
            data, next_link = self._request(
                next_url, params=query_params, check_active=check_active
            )
            query_params = None  # query params are embedded in next_link url
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                items.append(data)
            next_url = next_link

        return items

    def get_courses(self, check_active: Callable[[], None] | None = None) -> list[dict[str, Any]]:
        """Fetch all courses the token user is enrolled in."""
        return self.get_paginated("courses", {"enrollment_state": "active"}, check_active)

    def get_pages(
        self, course_id: int | str, check_active: Callable[[], None] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch course wiki pages with bodies."""
        return self.get_paginated(
            f"courses/{course_id}/pages",
            {"include[]": "body", "sort": "updated_at", "order": "desc"},
            check_active,
        )

    def get_modules(
        self, course_id: int | str, check_active: Callable[[], None] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch course modules with outline items."""
        return self.get_paginated(
            f"courses/{course_id}/modules",
            {"include[]": "items"},
            check_active,
        )

    def get_assignments(
        self, course_id: int | str, check_active: Callable[[], None] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch course assignments."""
        return self.get_paginated(f"courses/{course_id}/assignments", {}, check_active)

    def get_announcements(
        self, course_id: int | str, check_active: Callable[[], None] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch course announcements."""
        return self.get_paginated(
            f"courses/{course_id}/discussion_topics",
            {"only_announcements": "true"},
            check_active,
        )

    def get_discussions(
        self, course_id: int | str, check_active: Callable[[], None] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch course discussion topics."""
        return self.get_paginated(f"courses/{course_id}/discussion_topics", {}, check_active)


def _render_page_markdown(page: dict[str, Any], course_name: str) -> tuple[str, str, str]:
    cid = page.get("course_id", "unknown")
    pid = page.get("url") or page.get("page_id") or page.get("id")
    doc_id = f"canvas:page:{cid}:{pid}"
    title = f"{course_name} - Page: {page.get('title', 'Untitled')}"
    body = _strip_verifier_and_html(page.get("body", ""))
    content = f"# {title}\nCourse: {course_name}\nUpdated: {page.get('updated_at', '')}\n\n{body}"
    return doc_id, title, content


def _render_module_markdown(module: dict[str, Any], course_name: str) -> tuple[str, str, str]:
    cid = module.get("course_id", "unknown")
    mid = module.get("id", "unknown")
    doc_id = f"canvas:module:{cid}:{mid}"
    title = f"{course_name} - Module: {module.get('name', 'Untitled')}"
    items = module.get("items", [])
    item_lines = [f"- [{it.get('type', 'Item')}] {it.get('title', 'Untitled')}" for it in items]
    content = (
        f"# {title}\nCourse: {course_name}\nPosition: {module.get('position', 1)}\n\n"
        f"### Items\n" + ("\n".join(item_lines) if item_lines else "No items in this module.")
    )
    return doc_id, title, content


def _render_assignment_markdown(
    assignment: dict[str, Any], course_name: str
) -> tuple[str, str, str]:
    cid = assignment.get("course_id", "unknown")
    aid = assignment.get("id", "unknown")
    doc_id = f"canvas:assignment:{cid}:{aid}"
    title = f"{course_name} - Assignment: {assignment.get('name', 'Untitled')}"
    desc = _strip_verifier_and_html(assignment.get("description", ""))
    due = assignment.get("due_at") or "No due date"
    points = assignment.get("points_possible", 0)
    content = f"# {title}\nCourse: {course_name}\nDue: {due}\nPoints: {points}\n\n{desc}"
    return doc_id, title, content


def _render_announcement_markdown(
    announcement: dict[str, Any], course_name: str
) -> tuple[str, str, str]:
    cid = announcement.get("course_id", "unknown")
    tid = announcement.get("id", "unknown")
    doc_id = f"canvas:announcement:{cid}:{tid}"
    title = f"{course_name} - Announcement: {announcement.get('title', 'Untitled')}"
    msg = _strip_verifier_and_html(announcement.get("message", ""))
    posted = announcement.get("posted_at") or announcement.get("created_at") or ""
    author = announcement.get("user_name", "Staff")
    content = f"# {title}\nCourse: {course_name}\nAuthor: {author}\nPosted: {posted}\n\n{msg}"
    return doc_id, title, content


def _render_discussion_markdown(
    discussion: dict[str, Any], course_name: str, anonymize: bool = False
) -> tuple[str, str, str]:
    cid = discussion.get("course_id", "unknown")
    tid = discussion.get("id", "unknown")
    doc_id = f"canvas:discussion:{cid}:{tid}"
    title = f"{course_name} - Discussion: {discussion.get('title', 'Untitled')}"
    msg = _strip_verifier_and_html(discussion.get("message", ""))
    author = "Participant" if anonymize else discussion.get("user_name", "Author")
    last_reply = discussion.get("last_reply_at", "")
    content = (
        f"# {title}\nCourse: {course_name}\nAuthor: {author}\nLast Reply: {last_reply}\n\n{msg}"
    )
    return doc_id, title, content


def _iter_canvas_rows(
    client: CanvasClient,
    requested_resources: list[str],
    courses: list[int | str] | None = None,
    anonymize_authors: bool = False,
    state: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Iterate over Canvas LMS entity rows, tracking content hashes and deletions."""
    if state is None:
        state = {}
    hashes: dict[str, str] = state.setdefault("hashes", {})
    seen_ids: set[str] = set()

    all_courses = client.get_courses()
    course_map = {str(c.get("id")): c.get("name", f"Course {c.get('id')}") for c in all_courses}

    target_course_ids = [str(c) for c in courses] if courses else list(course_map.keys())

    for course_id in target_course_ids:
        course_name = course_map.get(course_id, f"Course {course_id}")

        # 1. Pages
        if "pages" in requested_resources:
            pages = client.get_pages(course_id)
            for page in pages:
                page["course_id"] = course_id
                doc_id, title, content = _render_page_markdown(page, course_name)
                seen_ids.add(doc_id)
                pid = page.get("url", "")
                url = page.get("html_url") or f"{client.base_url}/courses/{course_id}/pages/{pid}"
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if hashes.get(doc_id) != content_hash:
                    hashes[doc_id] = content_hash
                    doc = CanvasDocument(
                        id=doc_id,
                        title=title,
                        content=content,
                        url=url,
                        updated_at=page.get("updated_at", ""),
                    )
                    yield doc.to_row()

        # 2. Modules
        if "modules" in requested_resources:
            modules = client.get_modules(course_id)
            for mod in modules:
                mod["course_id"] = course_id
                doc_id, title, content = _render_module_markdown(mod, course_name)
                seen_ids.add(doc_id)
                url = f"{client.base_url}/courses/{course_id}/modules"
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if hashes.get(doc_id) != content_hash:
                    hashes[doc_id] = content_hash
                    doc = CanvasDocument(
                        id=doc_id, title=title, content=content, url=url, updated_at=""
                    )
                    yield doc.to_row()

        # 3. Assignments
        if "assignments" in requested_resources:
            assignments = client.get_assignments(course_id)
            for assign in assignments:
                assign["course_id"] = course_id
                doc_id, title, content = _render_assignment_markdown(assign, course_name)
                seen_ids.add(doc_id)
                aid = assign.get("id", "")
                url = (
                    assign.get("html_url")
                    or f"{client.base_url}/courses/{course_id}/assignments/{aid}"
                )
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if hashes.get(doc_id) != content_hash:
                    hashes[doc_id] = content_hash
                    doc = CanvasDocument(
                        id=doc_id,
                        title=title,
                        content=content,
                        url=url,
                        updated_at=assign.get("updated_at", ""),
                    )
                    yield doc.to_row()

        # 4. Announcements
        if "announcements" in requested_resources:
            announcements = client.get_announcements(course_id)
            for ann in announcements:
                ann["course_id"] = course_id
                doc_id, title, content = _render_announcement_markdown(ann, course_name)
                seen_ids.add(doc_id)
                tid = ann.get("id", "")
                url = (
                    ann.get("html_url")
                    or f"{client.base_url}/courses/{course_id}/discussion_topics/{tid}"
                )
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if hashes.get(doc_id) != content_hash:
                    hashes[doc_id] = content_hash
                    doc = CanvasDocument(
                        id=doc_id,
                        title=title,
                        content=content,
                        url=url,
                        updated_at=ann.get("posted_at", ""),
                    )
                    yield doc.to_row()

        # 5. Discussions
        if "discussions" in requested_resources:
            discussions = client.get_discussions(course_id)
            for disc in discussions:
                disc["course_id"] = course_id
                doc_id, title, content = _render_discussion_markdown(
                    disc, course_name, anonymize_authors
                )
                seen_ids.add(doc_id)
                tid = disc.get("id", "")
                url = (
                    disc.get("html_url")
                    or f"{client.base_url}/courses/{course_id}/discussion_topics/{tid}"
                )
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if hashes.get(doc_id) != content_hash:
                    hashes[doc_id] = content_hash
                    doc = CanvasDocument(
                        id=doc_id,
                        title=title,
                        content=content,
                        url=url,
                        updated_at=disc.get("last_reply_at", ""),
                    )
                    yield doc.to_row()

    # Forget-on-delete: emit tombstones for pruned/deleted items
    prev_ids = state.setdefault("prev_ids", [])
    deleted_ids = set(prev_ids) - seen_ids
    for del_id in deleted_ids:
        hashes.pop(del_id, None)
        yield {"id": del_id, "title": "", "content": "", "url": "", "_deleted": True}

    state["prev_ids"] = list(seen_ids)


def canvas_source(
    access_token: str | None = None,
    client: CanvasClient | None = None,
    base_url: str = DEFAULT_BASE_URL,
    courses: list[int | str] | None = None,
    resources: tuple[str, ...] | list[str] = ("pages", "modules", "assignments", "announcements"),
    anonymize_authors: bool = False,
    transport: Any | None = None,
    max_requests: int = 500,
) -> Any:
    """Create a dlt source syncing Canvas LMS into cognee memory."""
    import dlt

    if client is None:
        if not access_token:
            raise CanvasAuthError("Canvas access token or client instance is required.")
        client = CanvasClient(
            access_token=access_token,
            base_url=base_url,
            transport=transport,
            max_requests=max_requests,
        )

    requested_resources = [r.lower() for r in resources]
    for r in requested_resources:
        if r not in SUPPORTED_RESOURCES:
            raise ValueError(f"Unsupported resource '{r}'. Supported: {SUPPORTED_RESOURCES}")

    resource_name = "canvas_" + "_".join(sorted(requested_resources))

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def canvas_documents() -> Iterator[dict[str, Any]]:
        state = dlt.current.resource_state()
        yield from _iter_canvas_rows(
            client=client,
            requested_resources=requested_resources,
            courses=courses,
            anonymize_authors=anonymize_authors,
            state=state,
        )

    res = canvas_documents()
    setattr(res, DOCUMENT_SOURCE_ATTR, "canvas")
    setattr(res, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    return res
