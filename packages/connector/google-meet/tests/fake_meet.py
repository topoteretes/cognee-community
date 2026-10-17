"""An in-memory Google Meet and Drive API for tests.

Mimics the googleapiclient surface the connector uses, in the same style as
the Google Calendar connector's tests: ``meet.conferenceRecords().list(...)
.execute()``, ``meet.spaces().get(...)`` and ``drive.files().get(...)``.

It copies what the real API showed in a live check: lists come newest first,
an empty list is ``{}`` with no list key at all, and every list call pages.
"""

import itertools
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

RETENTION = timedelta(days=30)


class HttpError(Exception):
    """Mimics googleapiclient.errors.HttpError: the status sits on ``resp``.

    Like the real one, its text embeds the request URI, which holds the
    conference or Doc id.
    """

    def __init__(self, status: int, uri: str = "", reason: str | None = None):
        super().__init__(f'<HttpError {status} when requesting {uri} returned "error">')
        self.resp = type("Resp", (), {"status": status})()
        # Like googleapiclient 2.x: a list of dicts, each with the error's reason.
        self.error_details = [{"reason": reason}] if reason else []


def install_google_errors(monkeypatch) -> None:
    """Make ``from googleapiclient.errors import HttpError`` yield the fake class.

    The connector imports ``HttpError`` when a request fails, so the tests
    catch the same class the fakes raise.
    """
    monkeypatch.setitem(sys.modules, "googleapiclient.errors", SimpleNamespace(HttpError=HttpError))


def ts(moment: datetime) -> str:
    """A Meet timestamp: UTC with microseconds, like the live API returns."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


class _Collection:
    """One API collection: ``list`` pages through ``items(params)``."""

    def __init__(self, fake, method: str, key: str, items, children=None, get=None):
        self.fake, self.method, self.key = fake, method, key
        self._items, self._children, self._get = items, children or {}, get

    def list(self, **params):
        return _Request(lambda: self.fake._list(self.method, self.key, self._items, params))

    def get(self, **params):
        return _Request(lambda: self.fake._call(self.method + ".get", params, self._get))

    def __getattr__(self, name):
        if name in self._children:
            return self._children[name]
        raise AttributeError(name)


class FakeMeet:
    def __init__(self, drive=None, page_size: int = 2):
        # Transcript Docs are also created in ``drive``, like Meet saves them.
        self.drive = drive
        self.page_size = page_size
        self.spaces_by_name: dict[str, dict] = {}
        self.records: dict[str, dict] = {}
        self.participants: dict[str, list[dict]] = {}
        self.transcripts: dict[str, list[dict]] = {}
        self.recordings: dict[str, list[dict]] = {}
        self.fail_next: list[int] = []
        # Any request naming one of these ids (a conference id, say) fails:
        # id -> status, or (status, reason).
        self.errors: dict[str, int | tuple[int, str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self._pages: dict[str, list[dict]] = {}
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------
    # Helpers that change the fake (what happens in a real meeting)
    # ------------------------------------------------------------------
    def add_space(self, code: str = "abc-mnop-xyz") -> str:
        name = f"spaces/space{next(self._ids)}"
        self.spaces_by_name[name] = {
            "name": name,
            "meetingUri": f"https://meet.google.com/{code}",
            "meetingCode": code,
            "config": {"accessType": "TRUSTED"},
        }
        return name

    def start_conference(self, space: str, start: datetime) -> str:
        conference_id = f"conf{next(self._ids)}"
        self.records[conference_id] = {
            "name": f"conferenceRecords/{conference_id}",
            "startTime": ts(start),
            "space": space,
        }
        self.participants[conference_id] = []
        self.transcripts[conference_id] = []
        self.recordings[conference_id] = []
        return conference_id

    def end_conference(self, conference_id: str, end: datetime) -> None:
        record = self.records[conference_id]
        record["endTime"] = ts(end)
        record["expireTime"] = ts(end + RETENTION)

    def add_participant(self, conference_id, display_name, sessions, kind="signedinUser"):
        """``sessions`` is a list of (start, end) pairs; end may be None."""
        person_id = next(self._ids)
        name = f"conferenceRecords/{conference_id}/participants/{person_id}"
        identity = {"displayName": display_name}
        if kind == "signedinUser":
            identity["user"] = f"users/{person_id}"
        ends = [end for _start, end in sessions]
        participant = {
            "name": name,
            kind: identity,
            "earliestStartTime": ts(min(start for start, _end in sessions)),
            "_sessions": [
                {
                    "name": f"{name}/participantSessions/{i}",
                    "startTime": ts(start),
                    **({"endTime": ts(end)} if end else {}),
                }
                for i, (start, end) in enumerate(sessions)
            ],
        }
        if all(ends):
            participant["latestEndTime"] = ts(max(ends))
        self.participants[conference_id].append(participant)
        return name

    def add_transcript(self, conference_id, entries=(), state="FILE_GENERATED", doc_id=None):
        """``entries`` is a list of (participant name, text, time)."""
        transcript_id = f"t{next(self._ids)}"
        doc_id = doc_id or f"doc-{transcript_id}"
        if self.drive is not None:
            self.drive.add(doc_id)
        name = f"conferenceRecords/{conference_id}/transcripts/{transcript_id}"
        self.transcripts[conference_id].append(
            {
                "name": name,
                "state": state,
                "docsDestination": {
                    "document": doc_id,
                    "exportUri": f"https://docs.google.com/document/d/{doc_id}/view",
                },
                "_entries": [
                    {
                        "name": f"{name}/entries/{i}",
                        "participant": participant,
                        "text": text,
                        "languageCode": "en-US",
                        "startTime": ts(when),
                        "endTime": ts(when + timedelta(seconds=5)),
                    }
                    for i, (participant, text, when) in enumerate(entries)
                ],
            }
        )
        return name

    def set_transcript_state(self, transcript_name: str, state: str) -> None:
        conference_id = transcript_name.split("/")[1]
        for transcript in self.transcripts[conference_id]:
            if transcript["name"] == transcript_name:
                transcript["state"] = state

    def add_recording(self, conference_id: str) -> None:
        self.recordings[conference_id].append(
            {"name": f"conferenceRecords/{conference_id}/recordings/r1", "state": "FILE_GENERATED"}
        )

    def expire(self, conference_id: str) -> None:
        """Meet deletes the record 30 days after the conference ends."""
        del self.records[conference_id]

    # ------------------------------------------------------------------
    # googleapiclient surface
    # ------------------------------------------------------------------
    def conferenceRecords(self):
        sessions = _Collection(self, "sessions", "participantSessions", self._sessions_of)
        participants = _Collection(
            self,
            "participants",
            "participants",
            self._participants_of,
            children={"participantSessions": lambda: sessions},
        )
        entries = _Collection(self, "entries", "transcriptEntries", self._entries_of)
        transcripts = _Collection(
            self,
            "transcripts",
            "transcripts",
            self._transcripts_of,
            children={"entries": lambda: entries},
        )
        recordings = _Collection(self, "recordings", "recordings", self._recordings_of)
        return _Collection(
            self,
            "records",
            "conferenceRecords",
            self._records,
            children={
                "participants": lambda: participants,
                "transcripts": lambda: transcripts,
                "recordings": lambda: recordings,
            },
            get=self._get_record,
        )

    def spaces(self):
        return _Collection(self, "spaces", "spaces", None, get=self._get_space)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def calls_to(self, method: str) -> list[dict]:
        return [params for name, params in self.calls if name == method]

    def _call(self, method, params, fn):
        self.calls.append((method, dict(params)))
        uri = f"https://meet.googleapis.com/v2/{method}?{params}"
        if self.fail_next:
            raise HttpError(self.fail_next.pop(0), uri)
        for target, error in self.errors.items():
            if target in str(params):
                status, reason = error if isinstance(error, tuple) else (error, None)
                raise HttpError(status, uri, reason)
        return fn(params)

    def _list(self, method, key, items_fn, params):
        def run(params):
            # googleapiclient drops arguments that are None.
            params = {k: v for k, v in params.items() if v is not None}
            if "pageToken" in params:
                items = self._pages.pop(params["pageToken"])
            else:
                items = items_fn(params)
            limit = min(self.page_size, params.get("pageSize", self.page_size))
            page, rest = items[:limit], items[limit:]
            response = {key: [_public(item) for item in page]} if page else {}
            if rest:
                token = f"page{next(self._ids)}"
                self._pages[token] = rest
                response["nextPageToken"] = token
            return response

        return self._call(method, params, run)

    def _records(self, params):
        records = list(self.records.values())
        query = params.get("filter")
        if query:
            prefix = 'start_time>="'
            assert query.startswith(prefix) and query.endswith('"'), query
            since = query[len(prefix) : -1]
            assert since.endswith("Z"), "filter times must be UTC"
            cutoff = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ")
            records = [
                r
                for r in records
                if datetime.strptime(r["startTime"], "%Y-%m-%dT%H:%M:%S.%fZ") >= cutoff
            ]
        return sorted(records, key=lambda r: r["startTime"], reverse=True)

    def _get_record(self, params):
        conference_id = params["name"].split("/")[1]
        if conference_id not in self.records:
            raise HttpError(404, str(params))
        return dict(self.records[conference_id])

    def _get_space(self, params):
        return dict(self.spaces_by_name[params["name"]])

    def _participants_of(self, params):
        conference_id = params["parent"].split("/")[1]
        people = self.participants.get(conference_id, [])
        return sorted(people, key=lambda p: p["earliestStartTime"], reverse=True)

    def _sessions_of(self, params):
        conference_id = params["parent"].split("/")[1]
        for participant in self.participants[conference_id]:
            if participant["name"] == params["parent"]:
                return sorted(participant["_sessions"], key=lambda s: s["startTime"], reverse=True)
        raise HttpError(404, str(params))

    def _transcripts_of(self, params):
        return list(self.transcripts.get(params["parent"].split("/")[1], []))

    def _entries_of(self, params):
        conference_id = params["parent"].split("/")[1]
        for transcript in self.transcripts[conference_id]:
            if transcript["name"] == params["parent"]:
                return list(transcript["_entries"])
        raise HttpError(404, str(params))

    def _recordings_of(self, params):
        return list(self.recordings.get(params["parent"].split("/")[1], []))


def _public(item: dict) -> dict:
    """Drop the fake's own bookkeeping fields (they start with an underscore)."""
    return {k: v for k, v in item.items() if not k.startswith("_")}


class _Files:
    def __init__(self, fake):
        self.fake = fake

    def get(self, fileId, fields=None):
        return _Request(lambda: self.fake._get(fileId, fields))


class FakeDrive:
    """Only ``files().get``: the connector uses Drive to see if a Doc still exists."""

    def __init__(self):
        self.files_by_id: dict[str, dict] = {}
        self.fail_next: list[int] = []
        # Doc id -> status, or (status, reason): requests for that Doc fail.
        self.errors: dict[str, int | tuple[int, str]] = {}
        self.calls: list[str] = []

    def add(self, doc_id: str) -> None:
        self.files_by_id[doc_id] = {"id": doc_id, "trashed": False}

    def trash(self, doc_id: str) -> None:
        self.files_by_id[doc_id]["trashed"] = True

    def delete(self, doc_id: str) -> None:
        del self.files_by_id[doc_id]

    def files(self):
        return _Files(self)

    def _get(self, doc_id, fields):
        self.calls.append(doc_id)
        assert fields == "id,trashed", "only ask for what is needed"
        uri = f"https://www.googleapis.com/drive/v3/files/{doc_id}?fields={fields}"
        if self.fail_next:
            raise HttpError(self.fail_next.pop(0), uri)
        if doc_id in self.errors:
            error = self.errors[doc_id]
            status, reason = error if isinstance(error, tuple) else (error, None)
            raise HttpError(status, uri, reason)
        if doc_id not in self.files_by_id:
            raise HttpError(404, uri)
        return dict(self.files_by_id[doc_id])
