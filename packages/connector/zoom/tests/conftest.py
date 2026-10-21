"""In-memory stand-in for the Zoom REST API, shared by the test modules."""

import re
from datetime import date, datetime, timezone

import pytest

from cognee_community_connector_zoom.zoom import ZoomAPIError

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


class FakeZoom:
    """Implements the two client methods the source uses: ``get`` and ``download``."""

    def __init__(self):
        self.users = {"active": [{"id": "u1", "display_name": "Priya Shah"}], "inactive": []}
        self.meetings: dict[str, dict] = {}
        self.files: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.downloads: list[str] = []
        self.fail_path: str | None = None
        self.gone_files: set[str] = set()
        self.drop_meetings_key = False

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
        file_status="completed",
        version="",
    ):
        files = []
        if media:
            files.append({"id": f"{uuid}-v", "file_type": "MP4", "status": status})
        for kind, text in (("TRANSCRIPT", transcript), ("CHAT", chat)):
            if text is None:
                continue
            url = f"https://us02web.zoom.us/rec/download/{uuid}/{kind.lower()}{version}"
            self.files[url] = text
            files.append(
                {
                    "id": f"{uuid}-{kind.lower()}{version}",
                    "file_type": kind,
                    "status": file_status,
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
            users = self.users[params["status"]]
            return {"users": users, "total_records": len(users), "next_page_token": ""}
        match = re.fullmatch(r"/users/([^/]+)/recordings", path)
        if match:
            start, stop = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
            if (stop - start).days >= 30:
                # Zoom rejects a range longer than one month.
                raise ZoomAPIError("Zoom request failed: HTTP 400 (code 300)", status=400, code=300)
            meetings = [
                m
                for m in self.meetings.values()
                if m["host_id"] == match.group(1)
                and start <= date.fromisoformat(m["start_time"][:10]) <= stop
            ]
            if self.drop_meetings_key:
                return {"next_page_token": ""}
            return {"meetings": meetings, "total_records": len(meetings), "next_page_token": ""}
        match = re.fullmatch(r"/users/([^/]+)", path)
        if match:
            for user in self.users["active"] + self.users["inactive"]:
                if user["id"] == match.group(1):
                    return user
            raise ZoomAPIError("Zoom request failed: HTTP 404 (code 1001)", status=404, code=1001)
        raise AssertionError(f"unexpected path {path}")

    def download(self, url):
        self.downloads.append(url)
        if url in self.gone_files:
            raise ZoomAPIError("Zoom request failed: HTTP 404", status=404)
        return self.files[url]


@pytest.fixture
def zoom():
    return FakeZoom()


@pytest.fixture
def clock(monkeypatch):
    """The source's clock, starting at ``NOW``; tests move ``clock[0]`` forward."""
    from cognee_community_connector_zoom import zoom as zoom_module

    moment = [NOW]
    monkeypatch.setattr(zoom_module, "_utcnow", lambda: moment[0])
    return moment
