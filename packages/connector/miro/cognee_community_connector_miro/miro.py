"""Miro data-source connector for cognee.

Miro boards are spatial canvases, so storing every board item as an unrelated
row loses useful context. This connector renders one prose document per frame
and one additional document for unframed items. The documents opt into
cognee's normal cognify path through ``DOCUMENT_SOURCE_ATTR``.

The Miro API cannot filter boards or items by modification time and exposes no
delete feed. We therefore list the selected boards on every run, compare each
board's ``modifiedAt`` value with dlt resource state, and fully walk only boards
that changed. Changed documents are merged and documents missing from a
complete walk are emitted as hard-delete tombstones. State advances only after
all required pages have been read, so an interrupted request cannot turn a
partial response into deletions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("miro_connector")

MIRO_API_BASE_URL = "https://api.miro.com/v2/"
MIRO_SOURCE_NAME = "miro"
MIRO_TABLE_NAME = "miro_documents"
SUPPORTED_ITEM_TYPES = frozenset({"shape", "sticky_note", "text"})

_BLOCK_TAGS = frozenset({"br", "div", "li", "ol", "p", "tr", "ul"})
_WHITESPACE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """Convert Miro's small rich-text HTML subset into readable plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class _MiroRESTSource:
    """Read Miro through dlt's declarative REST API source."""

    def __init__(self, access_token: str, session: Any = None) -> None:
        self.access_token = access_token
        self.session = session

    def list_boards(
        self, *, team_id: str | None = None, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 50}
        if team_id:
            params["team_id"] = team_id
        if project_id:
            params["project_id"] = project_id
        if team_id or project_id:
            params["sort"] = "last_modified"

        config = {
            "client": self._client_config(),
            "resources": [
                {
                    "name": "boards",
                    "endpoint": {
                        "path": "boards",
                        "params": params,
                        "paginator": {
                            "type": "offset",
                            "limit": 50,
                            "total_path": "total",
                        },
                        "data_selector": "data",
                    },
                }
            ],
        }
        return self._extract(config, "boards")

    def list_items(self, board_id: str) -> list[dict[str, Any]]:
        config = {
            "client": self._client_config(),
            "resources": [
                {
                    "name": "items",
                    "endpoint": {
                        "path": f"boards/{board_id}/items",
                        "params": {"limit": 50},
                        "paginator": {
                            "type": "cursor",
                            "cursor_path": "cursor",
                            "cursor_param": "cursor",
                        },
                        "data_selector": "data",
                    },
                }
            ],
        }
        return self._extract(config, "items")

    def _client_config(self) -> dict[str, Any]:
        client: dict[str, Any] = {
            "base_url": MIRO_API_BASE_URL,
            "auth": {"type": "bearer", "token": self.access_token},
            "headers": {"Accept": "application/json"},
        }
        if self.session is not None:
            client["session"] = self.session
        return client

    @staticmethod
    def _extract(config: dict[str, Any], resource_name: str) -> list[dict[str, Any]]:
        try:
            from dlt.sources.rest_api import rest_api_resources
        except ImportError as exc:  # pragma: no cover - guarded by public factory
            raise ImportError(_install_hint()) from exc

        resources = rest_api_resources(config)
        resource = next(item for item in resources if item.name == resource_name)
        return list(resource)


def miro_source(
    access_token: str | None = None,
    *,
    board_ids: Iterable[str] | None = None,
    team_id: str | None = None,
    project_id: str | None = None,
    client: Any = None,
):
    """Return a dlt resource that syncs selected Miro boards into cognee.

    Args:
        access_token: Miro OAuth2 bearer token. Falls back to
            ``MIRO_ACCESS_TOKEN``. A token with ``boards:read`` is sufficient.
        board_ids: Optional board IDs to ingest. When omitted, every board in
            the team/project scope visible to the token is selected.
        team_id: Optional Miro team filter for board discovery.
        project_id: Optional Miro project/space filter for board discovery.
        client: Pre-built client implementing ``list_boards`` and ``list_items``;
            intended for offline tests.

    The returned resource uses merge/upsert with hard-delete tombstones. Pass it
    directly to ``cognee.remember`` with a dedicated dataset and
    ``write_disposition="merge"``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_install_hint()) from exc

    resolved_token = access_token or os.environ.get("MIRO_ACCESS_TOKEN")
    if client is None and not resolved_token:
        raise ValueError("Miro access token required: pass access_token= or set MIRO_ACCESS_TOKEN.")

    selected_ids = {str(board_id) for board_id in board_ids or ()}

    @dlt.resource(
        name=MIRO_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def miro_documents():
        rest_client = client or _MiroRESTSource(resolved_token)
        resource_state = dlt.current.resource_state()
        yield from _sync_rows(
            rest_client,
            resource_state,
            selected_ids=selected_ids,
            team_id=team_id,
            project_id=project_id,
        )

    resource = miro_documents
    setattr(resource, DOCUMENT_SOURCE_ATTR, MIRO_SOURCE_NAME)
    return resource


def _sync_rows(
    client: Any,
    state: dict[str, Any],
    *,
    selected_ids: set[str] | None = None,
    team_id: str | None = None,
    project_id: str | None = None,
):
    """Yield the incremental document delta and commit state on success only."""
    selected_ids = selected_ids or set()
    previous_boards = state.get("boards") or {}

    boards = client.list_boards(team_id=team_id, project_id=project_id)
    current_boards = {
        str(board["id"]): board
        for board in boards
        if board.get("id") is not None and (not selected_ids or str(board["id"]) in selected_ids)
    }

    next_boards: dict[str, Any] = {}
    changed = 0
    deleted = 0

    for board_id, board in current_boards.items():
        modified_at = board.get("modifiedAt") or board.get("modified_at")
        previous = previous_boards.get(board_id) or {}
        previous_documents = previous.get("documents") or {}

        if modified_at is not None and previous and previous.get("modified_at") == modified_at:
            next_boards[board_id] = previous
            continue

        items = client.list_items(board_id)
        documents = _board_documents(board, items)
        document_hashes = {row["id"]: _row_hash(row) for row in documents}

        for row in documents:
            if previous_documents.get(row["id"]) != document_hashes[row["id"]]:
                changed += 1
                yield row

        for document_id in previous_documents.keys() - document_hashes.keys():
            deleted += 1
            yield {"id": document_id, "_deleted": True}

        next_boards[board_id] = {
            "modified_at": modified_at,
            "documents": document_hashes,
        }

    for board_id in previous_boards.keys() - current_boards.keys():
        for document_id in previous_boards[board_id].get("documents") or {}:
            deleted += 1
            yield {"id": document_id, "_deleted": True}

    state["boards"] = next_boards
    logger.info(
        "Miro: %d selected board(s), %d changed document(s), %d deletion(s).",
        len(current_boards),
        changed,
        deleted,
    )


def _board_documents(board: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    board_id = str(board["id"])
    board_name = _plain_text(str(board.get("name") or "Untitled board"))
    board_url = str(board.get("viewLink") or (board.get("links") or {}).get("self") or "")
    frames = {
        str(item["id"]): item
        for item in items
        if item.get("type") == "frame" and item.get("id") is not None
    }
    grouped: dict[str | None, list[dict[str, Any]]] = {frame_id: [] for frame_id in frames}
    grouped[None] = []

    for item in items:
        if item.get("type") not in SUPPORTED_ITEM_TYPES or not _item_text(item):
            continue
        parent_id = _parent_id(item)
        grouped[parent_id if parent_id in frames else None].append(item)

    documents: list[dict[str, Any]] = []
    for frame_id, frame in sorted(frames.items(), key=lambda pair: _position_key(pair[1])):
        frame_items = grouped[frame_id]
        if not frame_items:
            continue
        documents.append(
            _document_row(
                document_id=f"{board_id}:frame:{frame_id}",
                board_id=board_id,
                board_name=board_name,
                board_url=board_url,
                frame_id=frame_id,
                frame_title=_frame_title(frame),
                items=frame_items,
            )
        )

    if grouped[None]:
        documents.append(
            _document_row(
                document_id=f"{board_id}:unframed",
                board_id=board_id,
                board_name=board_name,
                board_url=board_url,
                frame_id=None,
                frame_title="Unframed items",
                items=grouped[None],
            )
        )

    return documents


def _document_row(
    *,
    document_id: str,
    board_id: str,
    board_name: str,
    board_url: str,
    frame_id: str | None,
    frame_title: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    lines = [f"# Board: {board_name}", "", f"## Frame: {frame_title}"]
    for item in sorted(items, key=_position_key):
        label = item["type"].replace("_", " ").title()
        lines.append(f"- [{label}] {_item_text(item)}")

    return {
        "id": document_id,
        "board_id": board_id,
        "frame_id": frame_id,
        "url": board_url,
        "title": f"{board_name} — {frame_title}",
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _frame_title(frame: dict[str, Any]) -> str:
    data = frame.get("data") or {}
    return _plain_text(str(data.get("title") or frame.get("title") or "Untitled frame"))


def _item_text(item: dict[str, Any]) -> str:
    data = item.get("data") or {}
    value = data.get("content") or (data.get("text") if item.get("type") == "shape" else None)
    return _plain_text(str(value or ""))


def _parent_id(item: dict[str, Any]) -> str | None:
    parent = item.get("parent") or {}
    value = parent.get("id") if isinstance(parent, dict) else None
    value = value or item.get("parentId") or item.get("parent_id")
    return str(value) if value else None


def _position_key(item: dict[str, Any]) -> tuple[float, float, str]:
    position = item.get("position") or {}
    return (_number(position.get("y")), _number(position.get("x")), str(item.get("id") or ""))


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def _row_hash(row: dict[str, Any]) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _install_hint() -> str:
    return (
        "The Miro connector requires dlt. Install this package with: "
        "uv pip install cognee-community-connector-miro"
    )
