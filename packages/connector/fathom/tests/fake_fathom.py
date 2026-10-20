"""A stateful fake of ``GET /external/v1/meetings`` for the ``responses`` library.

It behaves like the documented API: 10 meetings per page, newest first, a page-number
cursor, ``created_after`` / ``created_before`` filters, and the summary, transcript,
action items and highlights left out unless their ``include_*`` flag is set. Every
request is recorded so tests can assert what was asked, and how heavy it was.
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import responses

from cognee_community_connector_fathom.fathom import BASE_URL

NOW = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
PAGE = 10
HEAVY = ("include_summary", "include_transcript")
OPTIONAL = {
    "include_summary": "default_summary",
    "include_transcript": "transcript",
    "include_action_items": "action_items",
    "include_highlights": "highlights",
}


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_meeting(recording_id: int, days_ago: float = 1.0, **overrides) -> dict:
    """A full API meeting. It carries e-mail addresses on purpose: none may reach a document."""
    created = NOW - timedelta(days=days_ago)
    meeting = {
        "title": f"Meeting {recording_id}",
        "meeting_title": f"Calendar {recording_id}",
        "meeting_type": "Customer call",
        "recording_id": recording_id,
        "url": f"https://fathom.video/calls/{recording_id}",
        "share_url": f"https://fathom.video/share/secret{recording_id}",
        "created_at": stamp(created),
        "scheduled_start_time": stamp(created - timedelta(minutes=2)),
        "scheduled_end_time": stamp(created + timedelta(minutes=58)),
        "recording_start_time": stamp(created - timedelta(minutes=1)),
        "recording_end_time": stamp(created + timedelta(minutes=44)),
        "calendar_invitees_domains_type": "one_or_more_external",
        "shared_with": "single_team",
        "transcript_language": "en",
        "calendar_invitees": [
            {
                "name": "Alice Johnson",
                "email": "alice.johnson@ourco.com",
                "email_domain": "ourco.com",
                "is_external": False,
            },
            {
                "name": "Jane Smith",
                "email": "jane.smith@acme.com",
                "email_domain": "acme.com",
                "is_external": True,
            },
        ],
        "recorded_by": {
            "name": "Alice Johnson",
            "email": "alice.johnson@ourco.com",
            "email_domain": "ourco.com",
            "team": "Sales",
        },
        "default_summary": {
            "template_name": "general",
            "markdown_formatted": f"## Summary\n\nWe agreed pricing for deal {recording_id}.\n",
        },
        "action_items": [
            {
                "description": f"Send the proposal {recording_id}",
                "user_generated": False,
                "completed": False,
                "recording_timestamp": "00:10:45",
                "recording_playback_url": "https://fathom.video/calls/x?timestamp=645",
                "assignee": {
                    "name": "Alice Johnson",
                    "email": "alice.johnson@ourco.com",
                    "team": "Sales",
                },
            }
        ],
        "transcript": [
            {
                "speaker": {
                    "display_name": "Jane Smith",
                    "matched_calendar_invitee_email": "jane.smith@acme.com",
                },
                "text": f"Ten percent off is fine for {recording_id}.",
                "timestamp": "00:05:32",
            }
        ],
        "highlights": [
            {"type": "Objection", "summary": "Price", "start_time": 16, "text": "Too pricey"}
        ],
    }
    meeting.update(overrides)
    return meeting


class FakeFathom:
    def __init__(self):
        self.meetings: dict[int, dict] = {}
        self.requests: list[dict] = []
        # status codes to answer before behaving, e.g. [429, 429]; or a callable(params)
        self.script: list = []
        self.retry_after: str | None = "3"
        # recording_id -> number of responses to hide it from (page shifts, mid-walk deletes)
        self.hide: dict[int, int] = {}
        # recording ids a detail (heavy) request leaves out, e.g. deleted a moment ago
        self.hide_in_details: set[int] = set()
        self.fail_when = None

    def add(self, recording_id: int, days_ago: float = 1.0, **overrides) -> dict:
        meeting = make_meeting(recording_id, days_ago, **overrides)
        self.meetings[recording_id] = meeting
        return meeting

    def remove(self, recording_id: int) -> None:
        del self.meetings[recording_id]

    def install(self, rsps: responses.RequestsMock) -> None:
        rsps.add_callback(responses.GET, f"{BASE_URL}/meetings", callback=self._handle)

    @property
    def heavy_requests(self) -> list[dict]:
        return [r for r in self.requests if r["heavy"]]

    def ids_requested(self) -> set[int]:
        return {i for r in self.requests for i in r["returned"]}

    def _handle(self, request):
        query = parse_qs(urlparse(request.url).query)
        params = {key: values if key.endswith("[]") else values[0] for key, values in query.items()}
        record = {
            "params": params,
            "heavy": any(params.get(flag) == "true" for flag in HEAVY),
            "returned": [],
            "key": request.headers.get("X-Api-Key"),
        }
        self.requests.append(record)

        if self.fail_when is not None and (status := self.fail_when(params, len(self.requests))):
            return (status, {}, "{}")
        if self.script:
            status = self.script.pop(0)
            if status != 200:
                headers = (
                    {"Retry-After": self.retry_after} if status == 429 and self.retry_after else {}
                )
                return (status, headers, "{}")

        items = list(self.meetings.values())
        if "created_after" in params:
            items = [m for m in items if m["created_at"] > params["created_after"]]
        if "created_before" in params:
            items = [m for m in items if m["created_at"] < params["created_before"]]
        items.sort(key=lambda m: (m["created_at"], m["recording_id"]), reverse=True)

        page = 1
        if "cursor" in params:
            page = json.loads(base64.b64decode(params["cursor"]))["page_num"]
        window = items[(page - 1) * PAGE : page * PAGE]
        more = len(items) > page * PAGE

        shown = []
        for meeting in window:
            if record["heavy"] and meeting["recording_id"] in self.hide_in_details:
                continue
            if self.hide.get(meeting["recording_id"], 0) > 0:
                self.hide[meeting["recording_id"]] -= 1
                continue
            out = {k: v for k, v in meeting.items() if k not in OPTIONAL.values()}
            for flag, key in OPTIONAL.items():
                if params.get(flag) == "true":
                    out[key] = meeting.get(key)
            shown.append(out)
            record["returned"].append(meeting["recording_id"])

        cursor = base64.b64encode(json.dumps({"page_num": page + 1}).encode()).decode()
        body = {"limit": PAGE, "next_cursor": cursor if more else None, "items": shown}
        return (200, {}, json.dumps(body))
