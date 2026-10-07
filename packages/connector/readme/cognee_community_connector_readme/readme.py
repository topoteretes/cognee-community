"""ReadMe v2 source for cognee documents.

The connector syncs the guides in exactly one ReadMe branch (``stable`` by
default), its guide categories, and optionally the project's versionless
changelog entries.  A branch is deliberately required as a *single* value:
ingesting every version of a guide would create duplicate memories for the
same page.

ReadMe does not expose a change feed.  Each run therefore performs a cheap
category/page listing sweep to learn the current IDs and ``updated_at`` values.
Only pages whose revision changed have their complete body fetched and yielded.
The prior ID set is retained in dlt resource state; vanished entries are emitted
as hard-delete tombstones so the existing cognee orphan cleanup forgets them.
If a listing is empty after a prior nonempty sync, the connector refuses to
treat that as a deletion event: an expired key or temporary API failure must not
erase an entire memory dataset.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("readme_connector")

README_API_BASE_URL = "https://api.readme.com/v2"
README_SOURCE_NAME = "readme"
README_TABLE_NAME = "readme_documents"
_MAX_RETRIES = 4
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ReadMeClient:
    """Small standard-library ReadMe v2 client used by the source.

    Keeping transport here dependency-free makes the connector simple to
    install and keeps tests entirely offline.  The client only issues GET
    requests with a Bearer key.
    """

    def __init__(self, api_key: str, api_base_url: str = README_API_BASE_URL):
        if not api_key:
            raise ValueError("ReadMe API key required: pass api_key= or set README_API_KEY.")
        self._api_key = api_key
        self._api_base_url = api_base_url.rstrip("/")

    def get(self, path: str) -> dict[str, Any]:
        """Return a single resource from an API v2 URI or path."""
        payload = self._request_json(path)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"ReadMe returned a non-object resource for {path!r}.")
        return data

    def iter_collection(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield every item in a ReadMe v2 collection, following ``paging.next``."""
        next_path: str | None = path
        next_params = dict(params or {"per_page": 100})
        while next_path:
            payload = self._request_json(next_path, next_params)
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise ValueError(f"ReadMe returned a non-list collection for {next_path!r}.")
            yield from (item for item in data if isinstance(item, dict))
            paging = payload.get("paging") or {}
            next_path = paging.get("next")
            # ReadMe's next link already has its query string; do not duplicate
            # page/per_page on subsequent requests.
            next_params = {}

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self._url(path, params)
        for attempt in range(_MAX_RETRIES):
            try:
                request = Request(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self._api_key}",
                        "User-Agent": "cognee-readme-connector",
                    },
                )
                with urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError(f"ReadMe returned invalid JSON for {path!r}.")
                return payload
            except HTTPError as exc:
                if exc.code not in _RETRYABLE_STATUS_CODES or attempt == _MAX_RETRIES - 1:
                    raise
                self._sleep_before_retry(exc, attempt)
            except URLError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                self._sleep_before_retry(None, attempt)

        raise RuntimeError("ReadMe retry loop ended unexpectedly.")  # pragma: no cover

    def _url(self, path: str, params: dict[str, Any] | None) -> str:
        if path.startswith(("https://", "http://")):
            if not path.startswith(self._api_base_url + "/"):
                raise ValueError(
                    "ReadMe pagination URL points outside the configured API base URL."
                )
            url = path
        else:
            url = f"{self._api_base_url}/{path.lstrip('/')}"
        if params:
            return f"{url}{'&' if '?' in url else '?'}{urlencode(params, doseq=True)}"
        return url

    @staticmethod
    def _sleep_before_retry(error: HTTPError | None, attempt: int) -> None:
        retry_after = error.headers.get("Retry-After") if error is not None else None
        try:
            delay = float(retry_after) if retry_after is not None else float(2**attempt)
        except ValueError:
            delay = float(2**attempt)
        logger.warning(
            "ReadMe request failed; retrying in %.1fs (%d/%d).", delay, attempt + 1, _MAX_RETRIES
        )
        time.sleep(delay)


def _document_id(branch: str, kind: str, identifier: str) -> str:
    """Namespace IDs by one selected branch, avoiding cross-version collisions."""
    return f"readme:{branch}:{kind}:{identifier}"


def _revision(item: dict[str, Any]) -> str:
    """Return API ``updated_at`` or a stable fallback fingerprint.

    Category responses do not always expose ``updated_at``.  A canonical
    fingerprint lets those resources still participate in the incremental
    state machine without assuming a missing timestamp means unchanged.
    """
    updated_at = item.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        return updated_at
    canonical = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _content_body(item: dict[str, Any]) -> str:
    content = item.get("content") or {}
    if isinstance(content, dict):
        return str(content.get("body") or content.get("excerpt") or "")
    return ""


def _category_row(category: dict[str, Any], branch: str) -> dict[str, Any]:
    title = str(category.get("title") or "")
    return {
        "id": _document_id(branch, "category", title),
        "title": f"ReadMe category: {title}",
        "content": f"# ReadMe category\n\n{title}",
        "url": str(category.get("uri") or ""),
        "updated_at": str(category.get("updated_at") or ""),
        "kind": "category",
        "branch": branch,
        "_deleted": False,
    }


def _guide_row(guide: dict[str, Any], branch: str, category_title: str) -> dict[str, Any]:
    slug = str(guide.get("slug") or guide.get("title") or "")
    title = str(guide.get("title") or slug)
    return {
        "id": _document_id(branch, "guide", slug),
        "title": title,
        "content": f"# {title}\n\n{_content_body(guide)}".strip(),
        "url": str(guide.get("uri") or ""),
        "updated_at": str(guide.get("updated_at") or ""),
        "kind": "guide",
        "category": category_title,
        "branch": branch,
        "_deleted": False,
    }


def _changelog_row(entry: dict[str, Any]) -> dict[str, Any]:
    identifier = str(entry.get("slug") or entry.get("title") or entry.get("uri") or "")
    title = str(entry.get("title") or identifier)
    return {
        "id": _document_id("global", "changelog", identifier),
        "title": f"ReadMe changelog: {title}",
        "content": f"# {title}\n\n{_content_body(entry)}".strip(),
        "url": str(entry.get("uri") or ""),
        "updated_at": str(entry.get("updated_at") or ""),
        "kind": "changelog",
        "branch": "global",
        "_deleted": False,
    }


def _guide_listing_path(branch: str, category_title: str) -> str:
    return (
        f"/branches/{quote(branch, safe='')}/categories/guides/"
        f"{quote(category_title, safe='')}/pages"
    )


def _guide_path(branch: str, slug: str) -> str:
    return f"/branches/{quote(branch, safe='')}/guides/{quote(slug, safe='')}"


def _guide_revision(guide_summary: dict[str, Any], category_title: str) -> str:
    """Include category membership in the guide revision.

    ReadMe can move an otherwise unchanged page between categories.  That is a
    meaningful metadata change for an ingested document, even if the page's
    own ``updated_at`` happens not to move, so the cursor must not skip it.
    """
    return f"{_revision(guide_summary)}:category:{category_title}"


def _collect_current_rows(
    client: Any,
    *,
    branch: str,
    category_titles: set[str] | None,
    include_changelog: bool,
    previous_revisions: dict[str, str],
) -> dict[str, tuple[str, dict[str, Any] | None]]:
    """Collect one sweep, fetching a full guide only when it is new or changed.

    ``None`` rows are known-unchanged resources: their ID/revision remains in
    state but no body is fetched or emitted.  This is the key incremental
    property—listing pages is lightweight while guide bodies can be large.
    """
    current: dict[str, tuple[str, dict[str, Any] | None]] = {}
    categories_path = f"/branches/{quote(branch, safe='')}/categories/guides"
    for category in client.iter_collection(categories_path):
        category_title = str(category.get("title") or "")
        if not category_title or (
            category_titles is not None and category_title not in category_titles
        ):
            continue

        category_row = _category_row(category, branch)
        category_id = category_row["id"]
        category_revision = _revision(category)
        current[category_id] = (
            category_revision,
            category_row if previous_revisions.get(category_id) != category_revision else None,
        )

        for guide_summary in client.iter_collection(_guide_listing_path(branch, category_title)):
            slug = str(guide_summary.get("slug") or guide_summary.get("title") or "")
            if not slug:
                logger.warning(
                    "ReadMe guide without a slug/title in category %r; skipping.", category_title
                )
                continue
            guide_id = _document_id(branch, "guide", slug)
            summary_revision = _guide_revision(guide_summary, category_title)
            if previous_revisions.get(guide_id) == summary_revision:
                current[guide_id] = (summary_revision, None)
                continue

            # Category listings are allowed to omit body content.  Follow the
            # per-guide URI (or reconstruct the documented route) so every
            # changed page is ingested with its complete markdown body.
            detail_path = str(guide_summary.get("uri") or _guide_path(branch, slug))
            guide = client.get(detail_path)
            guide_row = _guide_row(guide, branch, category_title)
            current[guide_id] = (summary_revision, guide_row)

    if include_changelog:
        for entry in client.iter_collection("/changelogs"):
            entry_row = _changelog_row(entry)
            entry_id = entry_row["id"]
            entry_revision = _revision(entry)
            current[entry_id] = (
                entry_revision,
                entry_row if previous_revisions.get(entry_id) != entry_revision else None,
            )
    return current


def sync_readme(
    client: Any,
    state: dict[str, Any],
    *,
    branch: str = "stable",
    category_titles: list[str] | None = None,
    include_changelog: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield changed ReadMe records and tombstones for records that vanished.

    The state is advanced only after all pending rows have been yielded.  A
    network/API exception therefore leaves the earlier cursor intact and the
    next run safely retries, rather than recording a partial sweep.
    """
    if not branch.strip():
        raise ValueError("branch must name exactly one ReadMe branch (for example 'stable').")

    selected_categories = set(category_titles) if category_titles is not None else None
    previous_revisions = {
        str(resource_id): str(revision)
        for resource_id, revision in (state.get("revisions") or {}).items()
    }
    current = _collect_current_rows(
        client,
        branch=branch,
        category_titles=selected_categories,
        include_changelog=include_changelog,
        previous_revisions=previous_revisions,
    )

    # A zero-result sweep cannot distinguish an intentionally empty ReadMe
    # project from revoked auth / an unexpected scope.  Preserve existing
    # state rather than generating a destructive mass delete.
    if previous_revisions and not current:
        logger.warning(
            "ReadMe returned no selected records while %d were known; "
            "preserving state and skipping deletes.",
            len(previous_revisions),
        )
        return

    changed = 0
    for _revision_value, row in current.values():
        if row is not None:
            yield row
            changed += 1

    deleted_ids = sorted(set(previous_revisions) - set(current))
    for resource_id in deleted_ids:
        yield {"id": resource_id, "_deleted": True}

    state["revisions"] = {
        resource_id: revision for resource_id, (revision, _row) in current.items()
    }
    logger.info("ReadMe: %d changed record(s), %d deletion(s).", changed, len(deleted_ids))


def readme_source(
    *,
    api_key: str | None = None,
    branch: str = "stable",
    category_titles: list[str] | None = None,
    include_changelog: bool = True,
    api_base_url: str = README_API_BASE_URL,
    client: Any = None,
):
    """Return a dlt resource that syncs one ReadMe documentation branch.

    Args:
        api_key: ReadMe v2 key. Falls back to ``README_API_KEY``.
        branch: One ReadMe branch/version. Defaults to ``stable``; pass a
            single preview branch to index it instead of production.
        category_titles: Optional exact guide-category titles to ingest. Omit
            to ingest all guide categories visible to the key.
        include_changelog: Whether to ingest ReadMe's versionless changelog.
        api_base_url: ReadMe API v2 URL; injectable for self-contained tests.
        client: A pre-built client exposing ``get`` and ``iter_collection``;
            primarily a test injection point.

    Returns:
        A ``readme_documents`` dlt resource.  Pass it to ``cognee.remember``
        with ``primary_key='id'``, ``write_disposition='merge'`` and
        ``max_rows_per_table=0``.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImportError(
            'Install the ReadMe connector extra: pip install "cognee[readme]".'
        ) from exc

    if client is None:
        client = ReadMeClient(api_key or os.environ.get("README_API_KEY", ""), api_base_url)

    @dlt.resource(
        name=README_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def readme_documents():
        yield from sync_readme(
            client,
            dlt.current.resource_state(),
            branch=branch,
            category_titles=category_titles,
            include_changelog=include_changelog,
        )

    resource = readme_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, README_SOURCE_NAME)
    return resource
