"""Authentication and client builder for Splitwise API v3."""

from __future__ import annotations

import os
from typing import Any

_EXTRA_HINT = (
    'The Splitwise connector requires the "splitwise" extra: '
    'pip install "cognee[splitwise]" (provides httpx).'
)

DEFAULT_BASE_URL = "https://secure.splitwise.com/api/v3.0"


def build_splitwise_client(
    api_key: str | None = None,
    base_url: str | None = None,
    client: Any | None = None,
) -> Any:
    """Build an authenticated Splitwise API v3 client using a personal access token.

    Args:
        api_key: Splitwise personal access token. When omitted, falls back to the
            ``SPLITWISE_API_KEY`` environment variable.
        base_url: Optional base URL override (default: https://secure.splitwise.com/api/v3.0).
        client: Optional pre-configured ``httpx.Client`` (for testing).

    Returns:
        An ``httpx.Client`` with Authorization header set.

    Raises:
        ValueError: If no API key is provided and ``SPLITWISE_API_KEY`` is not set.
        ImportError: If ``httpx`` is not installed.
    """
    try:
        import httpx
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_key = api_key or os.environ.get("SPLITWISE_API_KEY")
    if not resolved_key:
        raise ValueError(
            "Splitwise API key required: pass api_key= or set SPLITWISE_API_KEY in the environment."
        )

    resolved_base = (base_url or DEFAULT_BASE_URL).rstrip("/")

    c = client or httpx.Client(base_url=resolved_base, timeout=30.0)
    c.headers.update({"Authorization": f"Bearer {resolved_key}"})
    return c
