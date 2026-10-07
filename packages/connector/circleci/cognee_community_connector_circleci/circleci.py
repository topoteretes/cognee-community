"""CircleCI connector for cognee: pipeline, workflow and job outcomes as memory.

One document per pipeline. Failed jobs carry their failing tests, never build
logs. Incremental by pipeline ``created_at``; forget-on-delete via ``_deleted``.
"""

from __future__ import annotations

from typing import Any

from cognee.shared.logging_utils import get_logger

logger = get_logger("circleci_connector")

# CircleCI API v2. Overridable for CircleCI server installs.
DEFAULT_BASE_URL = "https://circleci.com/api/v2"

# Document-source tag: routes rows through cognify instead of the dlt-row path.
CIRCLECI_SOURCE_NAME = "circleci"


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
