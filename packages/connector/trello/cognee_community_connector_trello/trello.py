"""Trello connector for cognee: a ``dlt`` source that turns boards into memory.

Sync Trello boards (cards with their descriptions, checklists and comments, plus
each board's structure) into cognee, incrementally and with forget-on-delete.
The resource is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_trello import trello_source

    await cognee.remember(
        trello_source(board_ids=["<board id>"]),  # TRELLO_API_KEY / TRELLO_TOKEN from env
        dataset_name="trello",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Auth** is an API key plus a user token, sent in the ``Authorization`` header
  so the token never shows up in URLs or logs.
* **Rows** are flat ``{id, title, content, url, _deleted}``: one per card
  (``card:<id>``) and one per board (``board:<id>``). Card titles stay out of
  the board document, so a new card does not re-process the board.
* **Incremental.** Cards carry no reliable updated timestamp, so the board
  ``actions`` feed drives the sync: its newest action id is the cursor. Trello
  leaves some action types out of the feed (comment edits and deletions,
  checklist item and label changes), so every run also reads one snapshot of
  the board (cards, lists, labels, checklists) and diffs it by hash. Comments,
  the expensive part, are only read again when the feed has new actions, the
  board's ``dateLastActivity`` moved, or a card changed.
* **Forget-on-delete.** A card that is deleted or moved off the board, a board
  that is deleted or no longer visible, and a board removed from the selection
  are emitted as hard-delete tombstones. Any other failure raises, and dlt rolls
  the state of a failed run back.

Privacy
-------
A Trello token grants access to the whole account. Nothing is fetched until you
construct a source and call ``remember``. Authorize it read-only, and remember
that anyone with read access to the target dataset can read what was ingested.
"""

import copy
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = logging.getLogger(__name__)

API_URL = "https://api.trello.com/1"
_ACTIONS_PAGE = 1000
_TIMEOUT = 30.0
_RETRIES = 5
_MAX_RETRY_SLEEP = 10.0
_CARD_FIELDS = "id,name,desc,idList,labels,idMembers,start,due,dueComplete,closed,shortUrl"


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the key, token or body.
# ---------------------------------------------------------------------------
class TrelloSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class TrelloAuthError(TrelloSourceError):
    """Trello rejected the API key or token."""


class TrelloAPIError(TrelloSourceError):
    """Trello answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _retry_delay(response: Any, attempt: int) -> float:
    try:
        delay = float(response.headers.get("Retry-After"))
    except (TypeError, ValueError):
        delay = float(2**attempt)
    return min(max(delay, 0.0), _MAX_RETRY_SLEEP)


class TrelloClient:
    """Minimal synchronous REST client for the Trello API.

    ``get`` returns the decoded JSON body. Tests and hosts can pass any object
    with the same method to :func:`trello_source` instead.
    """

    def __init__(
        self,
        api_key: str,
        token: str,
        *,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        for value in (api_key, token):
            # Reject early and without echoing the value: httpx would put an
            # invalid header value into its exception.
            if not value or not value.isascii() or not value.isprintable() or " " in value:
                raise ValueError("The Trello API key or token is empty or has an invalid format")
        import httpx

        self._headers = {
            "Authorization": f'OAuth oauth_consumer_key="{api_key}", oauth_token="{token}"'
        }
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._sleep = sleep

    def __repr__(self) -> str:
        return "TrelloClient(credentials=<redacted>)"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import httpx

        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(API_URL + path, params=params, headers=self._headers)
            except httpx.TransportError as exc:
                if last:
                    raise TrelloAPIError(f"Trello request failed: {type(exc).__name__}") from None
                self._sleep(float(2**attempt))
                continue
            status = response.status_code
            if (status == 429 or status >= 500) and not last:
                self._sleep(_retry_delay(response, attempt))
                continue
            if status != 200:
                raise TrelloAPIError(f"Trello request failed: HTTP {status}", status=status)
            return response.json()
        raise TrelloAPIError("Trello request failed")  # pragma: no cover


# ---------------------------------------------------------------------------
# Rendering: deterministic, no volatile fields.
# ---------------------------------------------------------------------------
def _date(value: Any) -> str:
    return str(value)[:10] if value else ""


def _label_names(labels: list[dict]) -> list[str]:
    names = (str(label.get("name") or label.get("color") or "") for label in labels)
    return [name for name in names if name]


def _card_list(card: dict, board: dict) -> dict:
    return next((i for i in board.get("lists") or [] if i["id"] == card.get("idList")), {})


def _archived(card: dict, board: dict) -> bool:
    # Archiving a list or a board does not archive its cards in the API.
    return bool(card.get("closed") or _card_list(card, board).get("closed") or board.get("closed"))


def render_board(board: dict) -> dict[str, Any]:
    lists = sorted(board.get("lists") or [], key=lambda item: item.get("pos") or 0)
    members = sorted(name for m in board.get("members") or [] if (name := m.get("fullName")))
    labels = sorted(_label_names(board.get("labels") or []))
    parts = [f"Board: {board.get('name') or board['id']}"]
    if board.get("closed"):
        parts.append("Status: archived")
    description = str(board.get("desc") or "").strip()
    if description:
        parts.extend(["", description])
    if lists:
        parts.extend(["", "Lists:"])
        parts.extend(
            f"- {item.get('name')}" + (" (archived)" if item.get("closed") else "")
            for item in lists
        )
    if labels:
        parts.extend(["", f"Labels: {', '.join(labels)}"])
    if members:
        parts.append(f"Members: {', '.join(members)}")
    return {
        "id": f"board:{board['id']}",
        "title": str(board.get("name") or board["id"]),
        "content": "\n".join(parts),
        "url": str(board.get("url") or ""),
        "_deleted": False,
    }


def render_card(
    card: dict, board: dict, checklists: list[dict], comments: list[dict]
) -> dict[str, Any]:
    members = {m["id"]: str(m.get("fullName") or "") for m in board.get("members") or []}
    due = _date(card.get("due"))
    if due and card.get("dueComplete"):
        due += " (done)"
    fields = [
        ("Board", str(board.get("name") or "")),
        ("List", str(_card_list(card, board).get("name") or "")),
        ("Labels", ", ".join(_label_names(card.get("labels") or []))),
        ("Members", ", ".join(members[m] for m in card.get("idMembers") or [] if members.get(m))),
        ("Start", _date(card.get("start"))),
        ("Due", due),
        ("Status", "archived" if _archived(card, board) else ""),
    ]
    parts = [f"{label}: {value}" for label, value in fields if value]
    description = str(card.get("desc") or "").strip()
    if description:
        parts.extend(["", description])
    if checklists:
        parts.extend(["", "Checklists:"])
        for checklist in sorted(checklists, key=lambda c: c.get("pos") or 0):
            parts.append(f"{checklist.get('name') or 'Checklist'}:")
            for item in sorted(checklist.get("checkItems") or [], key=lambda i: i.get("pos") or 0):
                mark = "x" if item.get("state") == "complete" else " "
                parts.append(f"- [{mark}] {item.get('name')}")
    if comments:
        parts.extend(["", "Comments:"])
        for comment in sorted(comments, key=lambda c: c["id"]):
            author = str((comment.get("memberCreator") or {}).get("fullName") or "Unknown")
            text = str((comment.get("data") or {}).get("text") or "").strip()
            parts.append(f"{author} ({_date(comment.get('date'))}): {text}")
    return {
        "id": f"card:{card['id']}",
        "title": str(card.get("name") or "").strip() or "Untitled card",
        "content": "\n".join(parts),
        "url": str(card.get("shortUrl") or ""),
        "_deleted": False,
    }


def _digest(row: dict) -> str:
    return hashlib.md5(json.dumps(row, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _TrelloConfig:
    board_ids: tuple[str, ...]
    workspace_id: str | None
    include_archived: bool
    include_comments: bool
    include_checklists: bool
    full_resync: bool


def _board_ids(client: Any, config: _TrelloConfig) -> list[str]:
    if config.board_ids:
        return list(config.board_ids)
    params = {"filter": "all" if config.include_archived else "open", "fields": "id"}
    if config.workspace_id:
        boards = client.get(f"/organizations/{config.workspace_id}/boards", params)
    else:
        boards = client.get("/members/me/boards", params)
    return [str(board["id"]) for board in boards]


def _snapshot(client: Any, board_id: str) -> dict:
    """One request: the board with its lists, labels, members, cards and checklists."""
    return client.get(
        f"/boards/{board_id}",
        {
            "fields": "name,desc,closed,url,dateLastActivity",
            "lists": "all",
            "labels": "all",
            # The nested label list is capped at 50 unless asked for more.
            "labels_limit": 1000,
            "members": "all",
            "cards": "all",
            "card_fields": _CARD_FIELDS,
            "checklists": "all",
        },
    )


def _newest_action(client: Any, board_id: str, cursor: str | None) -> str | None:
    """The newest action id after ``cursor``, or None when nothing happened."""
    params: dict[str, Any] = {"limit": 2, "fields": "id"}
    if cursor:
        params["since"] = cursor
    actions = client.get(f"/boards/{board_id}/actions", params)
    # Action ids are Mongo ObjectIds, so newer ones compare greater.
    newer = [str(a["id"]) for a in actions if not cursor or str(a["id"]) > cursor]
    return max(newer, default=None)


def _comments(client: Any, board_id: str) -> dict[str, list[dict]]:
    by_card: dict[str, list[dict]] = {}
    before = None
    while True:
        params: dict[str, Any] = {"filter": "commentCard", "limit": _ACTIONS_PAGE}
        if before:
            params["before"] = before
        page = client.get(f"/boards/{board_id}/actions", params)
        for action in page:
            card_id = ((action.get("data") or {}).get("card") or {}).get("id")
            if card_id:
                by_card.setdefault(str(card_id), []).append(action)
        if len(page) < _ACTIONS_PAGE:
            return by_card
        oldest = min(str(a["id"]) for a in page)
        if oldest == before:
            raise TrelloAPIError("Trello action pagination did not advance")
        before = oldest


def _forget(board_id: str, entry: dict, stats: dict[str, int]) -> Iterator[dict]:
    for cid in entry["cards"]:
        stats["deleted"] += 1
        yield {"id": f"card:{cid}", "_deleted": True}
    if entry["board"]:
        stats["deleted"] += 1
        yield {"id": f"board:{board_id}", "_deleted": True}


def _sync_board(
    client: Any, config: _TrelloConfig, board_id: str, entry: dict, stats: dict[str, int]
) -> Iterator[dict]:
    """Yield one board's changed rows and tombstones, updating ``entry`` in place."""
    board = _snapshot(client, board_id)
    newest = _newest_action(client, board_id, entry["cursor"])
    activity = board.get("dateLastActivity")

    if board.get("closed") and not config.include_archived:
        yield from _forget(board_id, entry, stats)
        entry.update(board=None, cards={})
        return

    board_row = render_board(board)
    if _digest(board_row) != entry["board"]:
        entry["board"] = _digest(board_row)
        stats["emitted"] += 1
        yield board_row

    checklists: dict[str, list[dict]] = {}
    if config.include_checklists:
        for checklist in board.get("checklists") or []:
            checklists.setdefault(str(checklist.get("idCard")), []).append(checklist)
    cards = {
        str(card["id"]): card
        for card in board.get("cards") or []
        if config.include_archived or not _archived(card, board)
    }
    # A card's base is its document without comments. It catches the changes the
    # actions feed leaves out (checklist items, labels) without reading comments.
    bases = {
        cid: _digest(render_card(card, board, checklists.get(cid, []), []))
        for cid, card in cards.items()
    }
    known: dict[str, dict] = entry["cards"]
    changed = (
        config.full_resync
        or newest is not None
        or activity != entry["activity"]
        or any(known.get(cid, {}).get("base") != base for cid, base in bases.items())
    )
    comments = _comments(client, board_id) if config.include_comments and changed else None

    for cid, card in cards.items():
        previous = known.get(cid)
        if comments is None and previous and previous["base"] == bases[cid]:
            stats["unchanged"] += 1
            continue
        row = render_card(card, board, checklists.get(cid, []), (comments or {}).get(cid, []))
        known[cid] = {"base": bases[cid], "row": _digest(row)}
        if previous and previous["row"] == known[cid]["row"]:
            stats["unchanged"] += 1
            continue
        stats["emitted"] += 1
        yield row

    # Deleted, moved to another board, or archived while archived cards are excluded.
    for cid in [cid for cid in known if cid not in cards]:
        del known[cid]
        stats["deleted"] += 1
        yield {"id": f"card:{cid}", "_deleted": True}

    entry["cursor"] = newest or entry["cursor"]
    entry["activity"] = activity


def _iter_rows(
    client: Any, config: _TrelloConfig, state: dict, stats: dict[str, int]
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict."""
    # A bad key or token must fail loudly, never look like every board was deleted.
    try:
        client.get("/members/me", {"fields": "id"})
    except TrelloAPIError as exc:
        if exc.status in (400, 401):
            raise TrelloAuthError(
                f"Trello rejected the API key or token (HTTP {exc.status})"
            ) from None
        raise

    boards: dict[str, dict] = state.setdefault("boards", {})
    seen: set[str] = set()
    for board_id in _board_ids(client, config):
        # Work on a copy, so a board that turns out to be gone keeps its old state.
        entry = copy.deepcopy(boards.get(board_id)) or {
            "cursor": None,
            "activity": None,
            "board": None,
            "cards": {},
        }
        try:
            rows = list(_sync_board(client, config, board_id, entry, stats))
        except TrelloAPIError as exc:
            # Deleted, or no longer visible to this (valid) token.
            if exc.status not in (401, 404):
                raise
            stats["gone"] += 1
            continue
        boards[board_id] = entry
        seen.add(board_id)
        yield from rows

    # Deleted, hidden or deselected boards: forget the board and all its cards.
    for board_id in [b for b in boards if b not in seen]:
        yield from _forget(board_id, boards.pop(board_id), stats)

    logger.info(
        "Trello: %d row(s) emitted, %d deleted, %d board(s) gone.",
        stats["emitted"],
        stats["deleted"],
        stats["gone"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def trello_source(
    board_ids: list[str] | None = None,
    *,
    workspace_id: str | None = None,
    api_key: str | None = None,
    token: str | None = None,
    include_archived: bool = True,
    include_comments: bool = True,
    include_checklists: bool = True,
    full_resync: bool = False,
    resource_name: str = "trello_cards",
    check_active: Callable[[], None] | None = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per Trello card and board.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        board_ids: Boards to sync. Defaults to every board of ``workspace_id``,
            or every board the token's user belongs to.
        workspace_id: Sync every board of this workspace (id or name).
        api_key: Trello API key (``TRELLO_API_KEY``).
        token: Trello user token (``TRELLO_TOKEN``).
        include_archived: Keep archived cards, lists and boards, marked as archived.
            When False they are forgotten.
        include_comments: Add each card's comments to its document.
        include_checklists: Add each card's checklists to its document.
        full_resync: Read every board's comments again, whatever the feed says.
        resource_name: Staging table name. Use a different one per sync scope
            that shares a dataset.
        check_active: Optional host authorization checkpoint, called around each row.
        client: Pre-built client (see :class:`TrelloClient`). Mainly an injection
            point for tests and hosts.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError('The Trello connector requires dlt: pip install "cognee[dlt]".') from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Trello sync requires a cognee build with table-scoped DLT document cleanup."
        )

    if client is None:
        resolved_key = api_key or os.getenv("TRELLO_API_KEY")
        resolved_token = token or os.getenv("TRELLO_TOKEN")
        if not (resolved_key and resolved_token):
            raise ValueError(
                "Trello credentials missing: pass api_key and token or set "
                "TRELLO_API_KEY and TRELLO_TOKEN."
            )
        client = TrelloClient(resolved_key, resolved_token)

    config = _TrelloConfig(
        board_ids=tuple(str(b) for b in board_ids or ()),
        workspace_id=workspace_id,
        include_archived=include_archived,
        include_comments=include_comments,
        include_checklists=include_checklists,
        full_resync=full_resync,
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def trello_cards():
        stats.clear()
        stats.update(emitted=0, unchanged=0, deleted=0, gone=0)
        rows = _iter_rows(client, config, dlt.current.resource_state(), stats)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = trello_cards()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "trello")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
