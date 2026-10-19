"""An in-memory Microsoft Graph for tests, served over HTTP with ``responses``.

It answers the endpoints the connector calls, so the real dlt ``RESTClient``,
the ``@odata.nextLink`` paginator and dlt's retry logic all run unchanged:

* ``/me/mailFolders/{folder}/messages/delta`` — first round and delta rounds
* ``/me/mailFolders/{folder}``                — folder lookup
* ``/me/calendarView/delta``                  — calendar window rounds
* ``/me/events/{id}``                         — event lookup

Every change made through the helpers is appended to a change log, which is
what delta tokens replay, collapsed to the latest state per item like Graph.
"""

import itertools
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import responses

BASE = "https://graph.test/v1.0"
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0000000")


class FakeGraph:
    def __init__(self, page_size: int = 2, base: str = BASE):
        self.page_size = page_size
        # A second fake on another base URL stands in for a second mailbox.
        self.base = base
        self.folders: dict[str, dict[str, dict]] = {"inbox": {}, "sentitems": {}, "archive": {}}
        self.mail_log: dict[str, list[tuple[str, str, dict | None]]] = {
            name: [] for name in self.folders
        }
        self.events: dict[str, dict] = {}
        self.event_log: list[tuple[str, str]] = []
        self.expired_tokens: set[str] = set()
        self.fail_next: list[tuple[int, dict, dict]] = []  # (status, body, headers)
        self.requests: list = []
        self._pages: dict[str, tuple[list[dict], str]] = {}
        self._ids = itertools.count(1)
        # $select of each folder's first round; Graph keeps it in the delta link.
        self._select: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Helpers that change the fake mailbox (and record the change)
    # ------------------------------------------------------------------
    def put_message(self, folder: str, subject: str, body: str = "", **fields) -> str:
        message_id = fields.pop("id", None) or f"AAMk-msg-{next(self._ids)}"
        message = {
            "id": message_id,
            "subject": subject,
            "from": {"emailAddress": {"name": "Meera Joshi", "address": "meera@example.com"}},
            "toRecipients": [{"emailAddress": {"name": "Lay", "address": "lay@example.com"}}],
            "ccRecipients": [],
            "sentDateTime": "2026-05-20T09:00:00Z",
            "receivedDateTime": "2026-05-20T09:00:05Z",
            "conversationId": "AAQk-thread-1",
            "webLink": f"https://outlook.live.com/owa/?ItemID={message_id}",
            "hasAttachments": False,
            "body": {"contentType": "text", "content": body},
            "isRead": False,
            **fields,
        }
        self.folders[folder][message_id] = message
        self.mail_log[folder].append(("upsert", message_id, message))
        return message_id

    def edit_message(self, folder: str, message_id: str, **fields) -> None:
        message = {**self.folders[folder][message_id], **fields}
        self.folders[folder][message_id] = message
        self.mail_log[folder].append(("upsert", message_id, message))

    def move_message(self, message_id: str, source: str, target: str) -> None:
        """Immutable ids: the message keeps its id in the target folder."""
        message = self.folders[source].pop(message_id)
        self.mail_log[source].append(("removed", message_id, None))
        self.folders.setdefault(target, {})[message_id] = message
        self.mail_log.setdefault(target, []).append(("upsert", message_id, message))

    def delete_message(self, message_id: str, folder: str) -> None:
        """Deleting in Outlook moves the message to Deleted Items."""
        self.move_message(message_id, folder, "deleteditems")

    def delete_folder(self, folder: str) -> None:
        del self.folders[folder]

    def put_event(self, subject: str, start: datetime, **fields) -> str:
        event_id = fields.pop("id", None) or f"AAMk-evt-{next(self._ids)}"
        event = {
            "id": event_id,
            "subject": subject,
            "start": {"dateTime": _iso(start), "timeZone": "UTC"},
            "end": {"dateTime": _iso(start + timedelta(hours=1)), "timeZone": "UTC"},
            "isAllDay": False,
            "isCancelled": False,
            "location": {"displayName": "Pune office"},
            "organizer": {"emailAddress": {"name": "Meera Joshi", "address": "meera@example.com"}},
            "attendees": [
                {
                    "emailAddress": {"name": "Lay", "address": "lay@example.com"},
                    "status": {"response": "accepted"},
                }
            ],
            "body": {"contentType": "text", "content": ""},
            "webLink": f"https://outlook.live.com/calendar/item/{event_id}",
            "_start": start,
            **fields,
        }
        self.events[event_id] = event
        self.event_log.append(("upsert", event_id))
        return event_id

    def put_series(self, subject: str, start: datetime, days: int, **fields) -> str:
        """A daily series: the master with the details, plus one bare entry
        per occurrence carrying only its id, type and times, as Graph sends it."""
        master_id = self.put_event(
            subject,
            start,
            type="seriesMaster",
            recurrence={
                "pattern": {"type": "daily", "interval": 1},
                "range": {
                    "type": "endDate",
                    "startDate": start.strftime("%Y-%m-%d"),
                    "endDate": (start + timedelta(days=days - 1)).strftime("%Y-%m-%d"),
                },
            },
            **fields,
        )
        for day in range(days):
            moment = start + timedelta(days=day)
            occurrence_id = f"{master_id}-occ-{day}"
            self.events[occurrence_id] = {
                "id": occurrence_id,
                "type": "occurrence",
                "seriesMasterId": master_id,
                "start": {"dateTime": _iso(moment), "timeZone": "UTC"},
                "end": {"dateTime": _iso(moment + timedelta(minutes=30)), "timeZone": "UTC"},
                "_start": moment,
            }
            self.event_log.append(("upsert", occurrence_id))
        return master_id

    def hide_from_calendar_view(self, event_id: str) -> None:
        """Keep an event fetchable by id but out of calendarView listings,
        like a series master whose own start is before the window."""
        self.events[event_id]["_start"] = self.events[event_id]["_start"] - timedelta(days=3650)

    def edit_event(self, event_id: str, **fields) -> None:
        if "start" in fields:
            start = fields.pop("start")
            fields["_start"] = start
            fields["start"] = {"dateTime": _iso(start), "timeZone": "UTC"}
            fields["end"] = {"dateTime": _iso(start + timedelta(hours=1)), "timeZone": "UTC"}
        self.events[event_id] = {**self.events[event_id], **fields}
        self.event_log.append(("upsert", event_id))

    def delete_event(self, event_id: str) -> None:
        del self.events[event_id]
        self.event_log.append(("removed", event_id))

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def install(self, mock: responses.RequestsMock) -> None:
        # Looked up on every request, so a test can wrap _handle after install.
        mock.add_callback(
            responses.GET, re.compile(re.escape(self.base) + r"/.*"), lambda r: self._handle(r)
        )

    def _handle(self, request):
        self.requests.append(request)
        if self.fail_next:
            status, body, headers = self.fail_next.pop(0)
            return status, headers, json.dumps(body)
        url = urlparse(request.url)
        path = url.path[len(urlparse(self.base).path) :]
        query = {k: v[0] for k, v in parse_qs(url.query).items()}

        if "$skiptoken" in query:
            items, final = self._pages.pop(query["$skiptoken"])
            return self._page(items, final)
        match = re.fullmatch(r"/me/mailFolders/([^/]+)/messages/delta", path)
        if match:
            return self._mail_delta(match.group(1), query)
        match = re.fullmatch(r"/me/mailFolders/([^/]+)", path)
        if match:
            if match.group(1) not in self.folders:
                return self._error(404, "ErrorItemNotFound")
            return 200, {}, json.dumps({"id": match.group(1)})
        if path == "/me/calendarView/delta":
            return self._calendar_delta(query)
        match = re.fullmatch(r"/me/events/([^/]+)", path)
        if match:
            event = self.events.get(match.group(1))
            if event is None:
                return self._error(404, "ErrorItemNotFound")
            return 200, {}, json.dumps(self._public(event))
        return self._error(400, "BadRequest")

    def _mail_delta(self, folder: str, query: dict):
        if folder not in self.folders:
            return self._error(404, "ErrorItemNotFound")
        token = query.get("$deltatoken")
        if token in self.expired_tokens:
            return self._error(410, "syncStateNotFound")
        if token is None:
            if "$select" in query:
                self._select[folder] = ["id", *query["$select"].split(",")]
            items = list(self.folders[folder].values())
            since = re.search(r"receivedDateTime ge (\S+)", query.get("$filter", ""))
            if since:
                items = [m for m in items if m["receivedDateTime"] >= since.group(1)]
        else:
            items = self._collapse_mail(folder, int(token.split("~")[1]))
        fields = self._select.get(folder)
        if fields:
            items = [
                m if "@removed" in m else {k: v for k, v in m.items() if k in fields} for m in items
            ]
        position = len(self.mail_log[folder])
        final = (
            f"{self.base}/me/mailFolders/{folder}/messages/delta?$deltatoken={folder}~{position}"
        )
        return self._page(items, final)

    def _collapse_mail(self, folder: str, position: int) -> list[dict]:
        latest: dict[str, dict] = {}
        for op, message_id, message in self.mail_log[folder][position:]:
            if op == "removed":
                latest[message_id] = {"id": message_id, "@removed": {"reason": "deleted"}}
            else:
                latest[message_id] = message
        return list(latest.values())

    def _calendar_delta(self, query: dict):
        token = query.get("$deltatoken")
        if token in self.expired_tokens:
            return self._error(410, "syncStateNotFound")
        if token is None:
            start, end = query["startDateTime"], query["endDateTime"]
            items = [self._public(e) for e in self.events.values() if self._in(e, start, end)]
        else:
            _, position, start, end = token.split("~")
            items = []
            seen: dict[str, str] = {}
            for op, event_id in self.event_log[int(position) :]:
                seen[event_id] = op
            for event_id, op in seen.items():
                event = self.events.get(event_id)
                if op == "removed" or event is None or not self._in(event, start, end):
                    # Graph reports deletes and events that left the window alike.
                    items.append({"id": event_id, "@removed": {"reason": "deleted"}})
                else:
                    items.append(self._public(event))
        final = (
            f"{self.base}/me/calendarView/delta?$deltatoken=cal~{len(self.event_log)}~{start}~{end}"
        )
        return self._page(items, final)

    @staticmethod
    def _in(event: dict, start: str, end: str) -> bool:
        stamp = event["_start"].strftime("%Y-%m-%dT%H:%M:%SZ")
        return start <= stamp < end

    @staticmethod
    def _public(event: dict) -> dict:
        return {k: v for k, v in event.items() if not k.startswith("_")}

    def _page(self, items: list[dict], final_link: str):
        page, rest = items[: self.page_size], items[self.page_size :]
        body: dict = {"value": page}
        if rest:
            token = f"skip{len(self._pages)}-{len(self.requests)}"
            self._pages[token] = (rest, final_link)
            body["@odata.nextLink"] = f"{self.base}/next?$skiptoken={token}"
        else:
            body["@odata.deltaLink"] = final_link
        return 200, {}, json.dumps(body)

    @staticmethod
    def _error(status: int, code: str):
        return status, {}, json.dumps({"error": {"code": code, "message": code}})
