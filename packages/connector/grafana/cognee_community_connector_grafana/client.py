"""Bounded, read-only Grafana HTTP transport."""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx


class GrafanaSyncError(RuntimeError):
    """The selected snapshot could not be verified; retain the previous corpus."""


@dataclass
class SyncStats:
    requests: int = 0
    dashboards: int = 0
    annotations: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    failures: int = 0


def ensure_active(callback):
    if callback is not None:
        try:
            callback()
        except Exception:
            raise GrafanaSyncError("Grafana sync cancelled by host") from None


class GrafanaClient:
    """Caller-owned clients must be closed; factory-owned clients close after extraction.

    Retry-After above the five-second per-client wait budget defers the sync.
    Responses and underlying exception strings are deliberately never logged.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        org_id: int,
        *,
        transport=None,
        check_active: Callable | None = None,
        sleep: Callable = time.sleep,
    ):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=20,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Grafana-Org-Id": str(org_id),
                "Accept": "application/json",
            },
            transport=transport,
        )
        self._waited = 0.0
        self._active = check_active
        self._sleep = sleep

    def close(self):
        self._client.close()

    def get(self, path: str, params=None):
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise GrafanaSyncError("Invalid Grafana read path")
        for attempt in range(3):
            ensure_active(self._active)
            response = None
            try:
                response = self._client.get(path.lstrip("/"), params=params)
            except httpx.TransportError:
                pass
            ensure_active(self._active)
            if response is not None and response.status_code not in (429, 500, 502, 503, 504):
                if response.status_code != 200:
                    raise GrafanaSyncError(f"Grafana read returned HTTP {response.status_code}")
                try:
                    return response.json()
                except ValueError:
                    raise GrafanaSyncError("Grafana returned invalid JSON") from None
            if attempt == 2:
                raise GrafanaSyncError("Grafana transient read failed after three attempts")
            try:
                delay = (
                    float(response.headers.get("Retry-After", 2**attempt))
                    if response is not None
                    else 2**attempt
                )
            except ValueError:
                raise GrafanaSyncError("Grafana Retry-After requires a later sync") from None
            if not math.isfinite(delay) or delay < 0 or self._waited + delay > 5:
                raise GrafanaSyncError("Grafana retry wait budget exceeded; retry a later sync")
            self._waited += delay
            while delay > 0:
                ensure_active(self._active)
                interval = min(delay, 0.25)
                self._sleep(interval)
                delay -= interval
        raise GrafanaSyncError("Grafana read failed")
