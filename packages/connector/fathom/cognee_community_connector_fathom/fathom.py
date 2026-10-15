"""Fathom API client for retrieving meeting data."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import requests

API_BASE_URL = "https://api.fathom.ai/external/v1"


class FathomClient:
    """Small client for the Fathom external API."""

    def __init__(
        self,
        api_key: str,
        *,
        include_transcripts: bool = False,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("A Fathom API key is required.")

        self.include_transcripts = include_transcripts
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "X-Api-Key": api_key,
                "Accept": "application/json",
            }
        )

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(
            f"{API_BASE_URL}/{endpoint.lstrip('/')}",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Unexpected Fathom API response: expected a JSON object.")
        return payload

    def iter_meetings(
        self,
        *,
        created_after: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield meetings across all cursor-paginated API pages."""
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            params: dict[str, Any] = {}
            if created_after:
                params["created_after"] = created_after
            if cursor:
                params["cursor"] = cursor
            if self.include_transcripts:
                params["include_transcript"] = "true"

            payload = self._get("meetings", params)
            meetings = payload.get("items", payload.get("meetings", []))
            if not isinstance(meetings, list):
                raise ValueError("Unexpected Fathom API response: meetings must be a list.")

            for meeting in meetings:
                if isinstance(meeting, dict):
                    yield meeting

            next_cursor = payload.get("next_cursor")
            if not next_cursor:
                break
            if not isinstance(next_cursor, str):
                raise ValueError("Unexpected Fathom API response: next_cursor must be a string.")
            if next_cursor in seen_cursors:
                raise ValueError("Fathom API returned a repeated pagination cursor.")

            seen_cursors.add(next_cursor)
            cursor = next_cursor
