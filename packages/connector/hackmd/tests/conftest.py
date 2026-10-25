"""In-memory stand-in for the HackMD API, shared by the test modules.

Notes are shaped like the live API's answers: listings carry every field with an
empty ``content``, ``lastChangedAt`` is in milliseconds, and ``folderPaths`` lists
the folders from the top one down.
"""

import itertools
import re

import pytest

from cognee_community_connector_hackmd.hackmd import (
    HackMDAPIError,
    HackMDAuthError,
    HackMDQuotaError,
    HackMDRateLimitedError,
)

TEAM = "eng-team"


class FakeHackMD:
    """Implements ``get`` like HackMD: personal and team note listings and single notes."""

    def __init__(self):
        self.personal: dict[str, dict] = {}
        self.teams: dict[str, dict[str, dict]] = {TEAM: {}}
        self.calls: list[str] = []
        self.bad_token = False
        self.revoke_after: int | None = None
        self.errors: dict[str, int] = {}
        self.quota_used_up: set[str] = set()
        self.rate_limited: set[str] = set()
        self.listing_body = None
        self._clock = itertools.count(1_792_960_520_000, 1000)
        self._ids = itertools.count(1)

    def add_note(self, title, content, *, team=None, tags=(), folders=(), note_id=None):
        note_id = note_id or f"note{next(self._ids):04d}"
        parent = None
        folder_paths = []
        for index, name in enumerate(folders):
            folder_id = f"{note_id}-folder-{index}"
            folder_paths.append(
                {
                    "id": folder_id,
                    "name": name,
                    "icon": None,
                    "parentId": parent,
                    "color": None,
                    "clientId": f"client-{folder_id}",
                }
            )
            parent = folder_id
        stamp = next(self._clock)
        note = {
            "id": note_id,
            "title": title,
            "tags": list(tags),
            "description": "",
            "createdAt": stamp,
            "titleUpdatedAt": stamp,
            "tagsUpdatedAt": stamp,
            "publishType": "view",
            "publishedAt": None,
            "permalink": None,
            "publishLink": f"https://hackmd.io/@someone/{note_id}",
            "shortId": note_id,
            "lastChangedAt": stamp,
            "lastChangeUser": {
                "name": "Someone",
                "userPath": "someone",
                "photo": "",
                "biography": None,
            },
            "userPath": None if team else "someone",
            "teamPath": team,
            "readPermission": "guest" if team else "owner",
            "writePermission": "owner",
            "folderPaths": folder_paths,
            "content": content,
        }
        (self.teams[team] if team else self.personal)[note_id] = note
        return note

    def edit(self, note, content=None, **fields):
        """Change a note the way an edit in HackMD does, moving ``lastChangedAt``."""
        if content is not None:
            note["content"] = content
        note.update(fields)
        note["lastChangedAt"] = next(self._clock)

    def _listing(self, notes):
        # Listings never carry the content.
        return [{**note, "content": ""} for note in notes.values()]

    def get(self, path):
        self.calls.append(path)
        if self.bad_token or (
            self.revoke_after is not None and len(self.calls) > self.revoke_after
        ):
            raise HackMDAuthError("HackMD rejected the API token (HTTP 401)")
        if any(key in path for key in self.quota_used_up):
            raise HackMDQuotaError("HackMD API quota exceeded (HTTP 429)")
        if any(key in path for key in self.rate_limited):
            raise HackMDRateLimitedError("HackMD rate limit exceeded (HTTP 429)")
        for key, status in self.errors.items():
            if key in path:
                raise HackMDAPIError(f"HackMD request failed: HTTP {status}", status=status)
        match = re.fullmatch(r"(?:/teams/([^/]+))?/notes(?:/([^/]+))?", path)
        if not match:
            raise HackMDAPIError("HackMD request failed: HTTP 404", status=404)
        team, note_id = match.groups()
        if team is not None and team not in self.teams:
            # A team the token is not a member of answers 404, like HackMD.
            raise HackMDAPIError("HackMD request failed: HTTP 404", status=404)
        notes = self.teams[team] if team is not None else self.personal
        if note_id is None:
            if self.listing_body is not None:
                return self.listing_body
            return self._listing(notes)
        if note_id not in notes:
            raise HackMDAPIError("HackMD request failed: HTTP 404", status=404)
        return dict(notes[note_id])

    def note_reads(self):
        return [p for p in self.calls if re.search(r"/notes/[^/]+$", p)]


@pytest.fixture
def hackmd():
    return FakeHackMD()
