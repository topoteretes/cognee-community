"""In-memory stand-in for the Slite REST API, shaped like real responses."""

from __future__ import annotations

from typing import Any

from cognee_community_connector_slite.slite import (
    SliteAuthError,
    SliteNotFoundError,
    SlitePayloadTooLargeError,
    SliteRateLimitedError,
)


class FakeSlite:
    """Simulates the Slite v1 REST API for hermetic unit testing."""

    def __init__(self, user_id: str = "u_alice", team_id: str = "t_acme"):
        self.user_id = user_id
        self.team_id = team_id
        self.notes: dict[str, dict[str, Any]] = {}
        self.children_map: dict[str, list[str]] = {}
        self.fail_paths: dict[str, Exception] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.rate_limit_remaining: int = 100
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-11-04T10:{self._clock // 60:02d}:{self._clock % 60:02d}.000Z"

    def add_note(
        self,
        note_id: str,
        title: str,
        content: str = "Default content",
        parent_id: str | None = None,
        review_state: str | None = None,
        channel: str | None = None,
        archived: bool = False,
        owner: str = "Alice",
    ) -> dict[str, Any]:
        stamp = self._stamp()
        note = {
            "id": note_id,
            "title": title,
            "content": content,
            "parentNoteId": parent_id,
            "reviewState": review_state,
            "channel": channel or "Company Wiki",
            "url": f"https://slite.com/app/docs/{note_id}",
            "owner": {"id": f"u_{owner.lower()}", "name": owner},
            "createdAt": stamp,
            "updatedAt": stamp,
            "lastEditedAt": stamp,
            "archivedAt": stamp if archived else None,
        }
        self.notes[note_id] = note
        if parent_id:
            self.children_map.setdefault(parent_id, []).append(note_id)
        return note

    def archive_note(self, note_id: str) -> None:
        if note_id in self.notes:
            self.notes[note_id]["archivedAt"] = self._stamp()

    def delete_note(self, note_id: str) -> None:
        self.notes.pop(note_id, None)

    def get_me(self) -> dict[str, Any]:
        self.calls.append(("GET", "me", None))
        if "me" in self.fail_paths:
            raise self.fail_paths["me"]
        return {
            "user": {"id": self.user_id, "name": "Alice Tester", "email": "alice@example.com"},
            "team": {"id": self.team_id, "name": "Acme Corp"},
        }

    def get_notes(
        self,
        parent_note_id: str | None = None,
        order_by: str = "lastEditedAt_DESC",
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.calls.append(("GET", "notes", {"parentNoteId": parent_note_id, "cursor": cursor}))
        matching = [
            n for n in self.notes.values()
            if (parent_note_id is None or n.get("parentNoteId") == parent_note_id)
        ]
        return {
            "notes": matching[:limit],
            "total": len(matching),
            "hasNextPage": False,
            "nextCursor": None,
        }

    def get_note_children(
        self,
        note_id: str,
        include_descendants: bool = True,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("GET", f"notes/{note_id}/children", {"cursor": cursor}))
        child_ids = self.children_map.get(note_id, [])
        all_descendants = []
        queue = list(child_ids)
        while queue:
            cid = queue.pop(0)
            if cid in self.notes:
                all_descendants.append(self.notes[cid])
                if include_descendants:
                    queue.extend(self.children_map.get(cid, []))

        return {
            "notes": all_descendants,
            "total": len(all_descendants),
            "hasNextPage": False,
            "nextCursor": None,
        }

    def get_note(self, note_id: str, format: str = "md") -> dict[str, Any]:
        endpoint = f"notes/{note_id}"
        self.calls.append(("GET", endpoint, {"format": format}))
        if endpoint in self.fail_paths:
            raise self.fail_paths[endpoint]
        if note_id not in self.notes:
            raise SliteNotFoundError(f"Note {note_id} not found")
        return dict(self.notes[note_id])

    def search_notes(
        self,
        parent_note_id: str | None = None,
        last_edited_after: str | None = None,
        last_updated_after: str | None = None,
        include_archived: bool = False,
        page: int = 0,
        hits_per_page: int = 50,
    ) -> dict[str, Any]:
        self.calls.append(("GET", "search-notes", {"page": page}))
        matching = [
            n for n in self.notes.values()
            if (include_archived or not n.get("archivedAt"))
        ]
        return {
            "notes": matching,
            "total": len(matching),
            "page": page,
            "totalPages": 1,
        }
