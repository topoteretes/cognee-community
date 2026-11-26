"""Todoist /sync endpoint client with rate-limit resilience and cursor management."""

import asyncio
import httpx
from dataclasses import dataclass
from typing import List, Optional
from todoist_api_python.models import Task, Project, Comment


@dataclass
class SyncResult:
    """Result from Todoist /sync endpoint."""
    sync_token: str
    items: List[Task]
    projects: List[Project]
    comments: List[Comment]


class TodoistSyncClient:
    """
    Thin wrapper around Todoist's /sync endpoint for incremental sync.

    Uses raw httpx for full control over resource_types and sync_token.
    Handles rate limits (429) with Retry-After header and exponential backoff.
    """

    BASE_URL = "https://api.todoist.com/api/v1"
    SYNC_ENDPOINT = "/sync"
    MAX_RETRIES = 3
    BASE_BACKOFF = 1.0  # seconds

    def __init__(self, api_token: str):
        self.api_token = api_token
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def sync(
        self,
        sync_token: str = "",
        resource_types: Optional[List[str]] = None,
    ) -> SyncResult:
        """
        Perform a sync request to Todoist.

        Args:
            sync_token: Cursor from previous sync (empty string for initial full sync)
            resource_types: Resources to fetch (default: items, projects, comments)

        Returns:
            SyncResult with new sync_token and parsed resource lists.
        """
        if resource_types is None:
            resource_types = ["items", "projects", "comments"]

        import json
        payload = {
            "sync_token": sync_token,
            "resource_types": json.dumps(resource_types),
        }

        # Run async sync in event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(self._async_sync(payload))

    async def _async_sync(self, payload: dict) -> SyncResult:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(self.MAX_RETRIES + 1):
                try:
                    resp = await client.post(
                        f"{self.BASE_URL}{self.SYNC_ENDPOINT}",
                        data=payload,
                        headers=self._headers,
                    )

                    if resp.status_code == 429:
                        # Rate limited — respect Retry-After or exponential backoff
                        retry_after = resp.headers.get("Retry-After")
                        wait_time = float(retry_after) if retry_after else self.BASE_BACKOFF * (2 ** attempt)
                        await asyncio.sleep(wait_time)
                        continue

                    resp.raise_for_status()
                    data = resp.json()

                    return SyncResult(
                        sync_token=data["sync_token"],
                        items=[Task(**item) for item in data.get("items", [])],
                        projects=[Project(**proj) for proj in data.get("projects", [])],
                        comments=[Comment(**comm) for comm in data.get("comments", [])],
                    )

                except httpx.HTTPStatusError as e:
                    if e.response.status_code >= 500 and attempt < self.MAX_RETRIES:
                        await asyncio.sleep(self.BASE_BACKOFF * (2 ** attempt))
                        continue
                    raise

                except httpx.RequestError as e:
                    if attempt < self.MAX_RETRIES:
                        await asyncio.sleep(self.BASE_BACKOFF * (2 ** attempt))
                        continue
                    raise

        raise RuntimeError(f"Max retries ({self.MAX_RETRIES}) exceeded for Todoist sync")