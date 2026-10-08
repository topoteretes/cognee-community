"""Authentication and service client initialization for YouTube Data API v3."""

from __future__ import annotations

import os
from typing import Any

_EXTRA_HINT = (
    'The YouTube connector requires the "youtube" extra: pip install "cognee[youtube]" '
    "(provides google-api-python-client and youtube-transcript-api)."
)


def build_youtube_service(api_key: str | None = None) -> Any:
    """Build an authenticated YouTube Data API v3 client using an API key.

    Args:
        api_key: YouTube Data API key. When omitted, falls back to the
            ``YOUTUBE_API_KEY`` environment variable.

    Returns:
        A Google API Client discovery Resource for YouTube v3.

    Raises:
        ValueError: If no API key is provided and ``YOUTUBE_API_KEY`` is not set.
        ImportError: If ``googleapiclient`` is not installed.
    """
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_key = api_key or os.environ.get("YOUTUBE_API_KEY")
    if not resolved_key:
        raise ValueError(
            "YouTube API key required: pass api_key= or set YOUTUBE_API_KEY in the environment."
        )

    return build("youtube", "v3", developerKey=resolved_key, cache_discovery=False)
