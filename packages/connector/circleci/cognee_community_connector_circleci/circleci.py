"""CircleCI connector for cognee: pipeline, workflow and job outcomes as memory.

One document per pipeline. Failed jobs carry their failing tests, never build
logs. Incremental by pipeline ``created_at``; forget-on-delete via ``_deleted``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger

logger = get_logger("circleci_connector")

# CircleCI API v2. Overridable for CircleCI server installs.
DEFAULT_BASE_URL = "https://circleci.com/api/v2"

# Document-source tag: routes rows through cognify instead of the dlt-row path.
CIRCLECI_SOURCE_NAME = "circleci"

# Attempts per request (first try + retries) for rate-limited / transient failures.
_MAX_RETRIES = 5
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# requests waits forever without a timeout; a hung connection would stall the sync.
_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> requests.Session:
    """Build a ``requests`` session authenticated with a CircleCI personal API token."""
    session = requests.Session()
    session.headers.update({"Circle-Token": token, "Accept": "application/json"})
    return session


def _api_get(session: Any, base_url: str, path: str, params: dict | None = None) -> dict:
    """GET an API v2 path and return its JSON, retrying rate limits and transient errors.

    429 and 5xx responses wait for ``Retry-After`` (else exponential backoff), as
    do connection errors and timeouts. Any other error status, such as the 404
    for a project that no longer exists, raises ``requests.HTTPError`` at once so
    the caller decides what it means.
    """
    url = f"{base_url}{path}"
    attempt = 0
    while True:
        last_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = session.get(url, params=params or {}, timeout=_TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if last_attempt:
                raise
            reason, delay = repr(exc), _retry_after(None, attempt)
        else:
            if response.status_code not in _RETRY_STATUSES or last_attempt:
                response.raise_for_status()
                return response.json()
            reason, delay = f"HTTP {response.status_code}", _retry_after(response.headers, attempt)

        logger.warning(
            "CircleCI: %s on %s — retrying in %.1fs (%d/%d).",
            reason,
            path,
            delay,
            attempt + 1,
            _MAX_RETRIES - 1,
        )
        time.sleep(delay)
        attempt += 1


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else exponential backoff."""
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _paginate(session: Any, base_url: str, path: str, params: dict | None = None) -> Iterator[dict]:
    """Yield ``items`` across pages, following ``next_page_token``.

    Lazy on purpose: the pipeline listing is newest-first, so the sync can stop
    once it passes the cursor without fetching older pages.
    """
    params = dict(params or {})
    while True:
        data = _api_get(session, base_url, path, params)
        yield from data.get("items") or []
        page_token = data.get("next_page_token")
        if not page_token:
            return
        params["page-token"] = page_token


def circleci_source(
    *,
    project_slugs: list[str],
    token: str | None = None,
    branch: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields CircleCI pipelines for ``cognee.remember``.

    Args:
        project_slugs: Projects to sync, e.g. ``["gh/org/repo"]`` or
            ``["circleci/<org-id>/<project-id>"]`` for GitHub App projects.
        token: CircleCI personal API token. Falls back to ``CIRCLECI_TOKEN``.
        branch: Only sync pipelines on this branch.
        base_url: API base URL.
        session: Pre-built ``requests`` session (test injection point).
    """
    raise NotImplementedError
