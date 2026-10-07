"""In-memory stand-in for the Zoom REST API, shared by the test modules."""

import re
from datetime import UTC, date, datetime

import pytest

from cognee_community_connector_zoom.zoom import ZoomAPIError

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class FakeZoom:
    """Implements the two client methods the source uses: ``get`` and ``download``."""

    def __init__(self):
        self.users = {"active": [{"id": "u1", "display_name": "Priya Shah"}], "inactive": []}
        self.meetings: dict[str, dict] = {}
        self.files: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.downloads: list[str] = []
        self.fail_path: str | None = None

    def add_meeting(
        self,
        uuid,
        topic="Weekly planning",
        start="2026-10-05T10:00:00Z",
        *,
        host="u1",
        duration=30,
        transcript=None,
        chat=None,
        media=True,
        status="completed",
    ):
        files = []
        if media:
            files.append({"id": f"{uuid}-v", "file_type": "MP4", "status": status})
        for kind, text in (("TRANSCRIPT", transcript), ("CHAT", chat)):
            if text is None:
                continue
            url = f"https://zoom.test/rec/download/{uuid}/{kind.lower()}"
            self.files[url] = text
            files.append(
                {
                    "id": f"{uuid}-{kind.lower()}",
                    "file_type": kind,
                    "status": "completed",
                    "file_size": len(text),
                    "download_url": url,
                    "recording_start": start,
                }
            )
        self.meetings[uuid] = {
            "uuid": uuid,
            "id": 123456789,
            "topic": topic,
            "start_time": start,
            "duration": duration,
            "host_id": host,
            "share_url": f"https://zoom.test/rec/share/{uuid}",
            "recording_files": files,
        }
        return self.meetings[uuid]

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if self.fail_path and self.fail_path in path:
            raise ZoomAPIError("Zoom request failed: HTTP 503", status=503)
        if path == "/users":
            return {"users": self.users[params["status"]]}
        match = re.fullmatch(r"/users/([^/]+)/recordings", path)
        if match:
            start, stop = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
            meetings = [
                m
                for m in self.meetings.values()
                if m["host_id"] == match.group(1)
                and start <= date.fromisoformat(m["start_time"][:10]) <= stop
            ]
            return {"meetings": meetings, "next_page_token": ""}
        match = re.fullmatch(r"/users/([^/]+)", path)
        if match:
            for user in self.users["active"] + self.users["inactive"]:
                if user["id"] == match.group(1):
                    return user
            raise ZoomAPIError("Zoom request failed: HTTP 404 (code 1001)", status=404, code=1001)
        raise AssertionError(f"unexpected path {path}")

    def download(self, url):
        self.downloads.append(url)
        return self.files[url]


@pytest.fixture
def zoom():
    return FakeZoom()


@pytest.fixture
def fixed_now(monkeypatch):
    from cognee_community_connector_zoom import zoom as zoom_module

    monkeypatch.setattr(zoom_module, "_utcnow", lambda: NOW)
    return NOW
