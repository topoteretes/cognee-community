"""Miro data-source connector for cognee.

Miro boards are spatial canvases, so storing every board item as an unrelated
row loses useful context. This connector renders one prose document per frame
and one additional document for unframed items. The documents opt into
cognee's normal cognify path through ``DOCUMENT_SOURCE_ATTR``.

The Miro API exposes no delete feed, so every run produces a full snapshot with
``write_disposition="replace"``. Rendered board documents are cached in dlt
source state: unchanged boards are re-emitted from the cache, while changed
boards are fetched again. A changed board is read consistently by checking its
``modifiedAt`` value after all item pages have been fetched. If it changes
during the read, the connector retries once and then aborts the entire snapshot
rather than allowing a mixed-version response to drive deletion.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("miro_connector")

MIRO_API_BASE_URL = "https://api.miro.com/v2/"
MIRO_SOURCE_NAME = "miro"
MIRO_TABLE_NAME = "miro_documents"
SUPPORTED_ITEM_TYPES = frozenset({"shape", "sticky_note", "text"})
MAX_SNAPSHOT_ATTEMPTS = 2
_STATE_CACHE_KEY = "miro_snapshot_cache"
_STATE_CACHE_VERSION = 1

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


class MiroBoardNotFoundError(LookupError):
    """The requested Miro board was confirmed absent with HTTP 404."""


class MiroSnapshotChangedError(RuntimeError):
    """A board changed repeatedly while its item snapshot was being read."""


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
        # This is Miro's stable, board-wide inventory endpoint. Supplying
        # parent_item_id would narrow the same route to one frame and make
        # deletion reconciliation incomplete.
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

    def get_board(self, board_id: str) -> dict[str, Any]:
        """Fetch one board through dlt's declarative single-page REST resource."""
        config = {
            "client": self._client_config(),
            "resources": [
                {
                    "name": "board",
                    "endpoint": {
                        "path": f"boards/{board_id}",
                        "paginator": "single_page",
                        "data_selector": "$",
                    },
                }
            ],
        }
        try:
            rows = self._extract(config, "board")
        except Exception as exc:
            if _http_status(exc) == 404:
                raise MiroBoardNotFoundError(f"Miro board {board_id!r} was not found") from exc
            raise

        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise RuntimeError(f"Miro returned an invalid response for board {board_id!r}")
        return rows[0]

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
        access_token: Miro OAuth2 bearer token. When omitted, dlt resolves
            ``sources.miro.access_token`` from its secret providers. The legacy
            ``MIRO_ACCESS_TOKEN`` environment variable remains a fallback. A
            token with ``boards:read`` is sufficient.
        board_ids: Optional board IDs to ingest. When omitted, every board in
            the team/project scope visible to the token is selected.
        team_id: Optional Miro team filter for board discovery.
        project_id: Optional Miro project/space filter for board discovery.
        client: Pre-built client implementing ``list_boards``, ``get_board``,
            and ``list_items``; intended for offline tests.

    The returned resource is a full snapshot. Pass it directly to
    ``cognee.remember`` with a dedicated dataset and
    ``write_disposition="replace"``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_install_hint()) from exc

    resolved_token = (
        access_token
        or dlt.secrets.get("sources.miro.access_token")
        or os.environ.get("MIRO_ACCESS_TOKEN")
    )
    if client is None and not resolved_token:
        raise ValueError(
            "Miro access token required: pass access_token= or configure "
            "sources.miro.access_token through a dlt secret provider."
        )

    if isinstance(board_ids, (str, bytes)):
        raise TypeError("board_ids must be an iterable of board IDs, not a single string")

    selected_ids: set[str] | None = None
    if board_ids is not None:
        selected_ids = {
            str(board_id).strip()
            for board_id in board_ids
            if board_id is not None and str(board_id).strip()
        }
        if not selected_ids:
            raise ValueError("board_ids cannot be empty; omit it to sync every visible board")

    @dlt.resource(
        name=MIRO_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def miro_documents():
        rest_client = client or _MiroRESTSource(resolved_token)
        yield from _sync_rows(
            rest_client,
            selected_ids=selected_ids,
            team_id=team_id,
            project_id=project_id,
            # dlt resets resource state for ``replace`` loads, while source
            # state survives them. Keep the full-snapshot cache in source state.
            state=dlt.current.source_state(),
        )

    resource = miro_documents
    setattr(resource, DOCUMENT_SOURCE_ATTR, MIRO_SOURCE_NAME)
    # Cognee includes the dataset name when deriving this source's dlt pipeline
    # identity, so incremental cache state cannot leak between Miro datasets.
    setattr(resource, PIPELINE_SCOPE_ATTR, MIRO_SOURCE_NAME)
    return resource


def _sync_rows(
    client: Any,
    *,
    selected_ids: set[str] | None = None,
    team_id: str | None = None,
    project_id: str | None = None,
    state: dict[str, Any] | None = None,
):
    """Build and yield one complete, consistent snapshot of the selected boards."""
    scope = _scope_key(selected_ids, team_id, project_id)
    cached_boards = _cached_boards(state, scope)
    boards = _selected_boards(
        client,
        selected_ids=selected_ids,
        team_id=team_id,
        project_id=project_id,
        known_board_ids=set(cached_boards),
    )

    # Buffer every document before yielding. If any board changes repeatedly or
    # any request fails, dlt receives no partial snapshot and therefore cannot
    # replace valid rows with incomplete data.
    snapshot: list[dict[str, Any]] = []
    next_cache: dict[str, dict[str, Any]] = {}
    synced_boards = 0
    reused_boards = 0
    for board in boards:
        board_id = str(board["id"])
        initial_modified_at = _required_modified_at(board)
        cached = cached_boards.get(board_id)
        documents: list[dict[str, Any]] | None = None
        final_modified_at = initial_modified_at

        if cached is not None and cached.get("modified_at") == initial_modified_at:
            cached_documents = cached.get("documents")
            if isinstance(cached_documents, list) and all(
                isinstance(document, dict) for document in cached_documents
            ):
                documents = cached_documents
                reused_boards += 1

        if documents is None:
            result = _consistent_board_documents(client, board)
            if result is None:
                continue
            final_board, documents = result
            final_modified_at = _required_modified_at(final_board)

        next_cache[board_id] = {
            "modified_at": final_modified_at,
            "documents": documents,
        }
        snapshot.extend(documents)
        synced_boards += 1

    if state is not None:
        # dlt commits source state with the load package. Updating only after
        # every board has been buffered keeps the prior cache on any API or
        # consistency failure.
        state[_STATE_CACHE_KEY] = {
            "version": _STATE_CACHE_VERSION,
            "scope": scope,
            "boards": next_cache,
        }

    logger.info(
        "Miro: snapshotted %d board(s), %d document(s); reused %d unchanged board(s).",
        synced_boards,
        len(snapshot),
        reused_boards,
    )
    yield from snapshot


def _scope_key(
    selected_ids: set[str] | None,
    team_id: str | None,
    project_id: str | None,
) -> dict[str, Any]:
    """Describe the configured selection so state is never reused across scopes."""
    if selected_ids is not None:
        return {"mode": "explicit", "board_ids": sorted(selected_ids)}
    return {
        "mode": "discovery",
        "team_id": team_id,
        "project_id": project_id,
    }


def _cached_boards(
    state: dict[str, Any] | None, scope: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    if state is None:
        return {}
    cache = state.get(_STATE_CACHE_KEY)
    if not isinstance(cache, dict):
        return {}
    if cache.get("version") != _STATE_CACHE_VERSION or cache.get("scope") != scope:
        return {}
    boards = cache.get("boards")
    if not isinstance(boards, dict):
        return {}
    return {
        str(board_id): value
        for board_id, value in boards.items()
        if isinstance(value, dict)
    }


def _selected_boards(
    client: Any,
    *,
    selected_ids: set[str] | None,
    team_id: str | None,
    project_id: str | None,
    known_board_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve the configured scope without confusing API errors with deletion."""
    if selected_ids is None:
        listed = client.list_boards(team_id=team_id, project_id=project_id)
        boards = {str(board["id"]): board for board in listed}

        # Offset pagination can move while boards are edited. A board omitted
        # from one listing is not deletion evidence: directly check every
        # previously snapshotted board before allowing it to leave a replace
        # snapshot. Only a confirmed 404 removes it; all other errors abort.
        for board_id in sorted((known_board_ids or set()) - boards.keys()):
            try:
                boards[board_id] = client.get_board(board_id)
            except MiroBoardNotFoundError:
                logger.info("Miro: previously discovered board %s was deleted.", board_id)
        return list(boards.values())

    boards: list[dict[str, Any]] = []
    for board_id in sorted(selected_ids):
        try:
            boards.append(client.get_board(board_id))
        except MiroBoardNotFoundError:
            logger.info("Miro: selected board %s no longer exists; omitting it.", board_id)
    return boards


def _consistent_board_documents(
    client: Any, initial_board: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Return a board snapshot, retrying once if ``modifiedAt`` changes mid-read."""
    board = initial_board
    board_id = str(board["id"])

    for attempt in range(MAX_SNAPSHOT_ATTEMPTS):
        initial_modified_at = _required_modified_at(board)
        items = client.list_items(board_id)
        try:
            final_board = client.get_board(board_id)
        except MiroBoardNotFoundError:
            # A confirmed 404 means the board disappeared during this snapshot.
            # Omitting it is correct under full-snapshot replace semantics.
            logger.info("Miro: board %s was deleted while it was being read.", board_id)
            return None

        final_modified_at = _required_modified_at(final_board)
        if final_modified_at == initial_modified_at:
            return final_board, _board_documents(final_board, items)

        board = final_board
        logger.warning(
            "Miro: board %s changed while being read; retrying snapshot (%d/%d).",
            board_id,
            attempt + 1,
            MAX_SNAPSHOT_ATTEMPTS,
        )

    raise MiroSnapshotChangedError(
        f"Miro board {board_id!r} changed during both snapshot attempts; sync aborted"
    )


def _required_modified_at(board: dict[str, Any]) -> str:
    modified_at = board.get("modifiedAt") or board.get("modified_at")
    if not modified_at:
        raise RuntimeError(f"Miro board {board.get('id')!r} has no modifiedAt value")
    return str(modified_at)


def _http_status(exc: BaseException) -> int | None:
    """Find an HTTP response status through dlt's extraction exception chain."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        if response is not None and getattr(response, "status_code", None) is not None:
            return int(response.status_code)
        current = current.__cause__ or current.__context__
    return None


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


def _install_hint() -> str:
    return (
        "The Miro connector requires dlt. Install this package with: "
        "uv pip install cognee-community-connector-miro"
    )
