"""Trello connector for cognee — a ``dlt`` source that turns boards into memory.

Sync Trello boards — cards, descriptions, comments, checklists, and board
structure — into cognee, incrementally and with forget-on-deletion — "ask my
boards".  Like the sibling Confluence connector this builds entirely on the
existing DLT ingestion subsystem; the source produced here is handed directly
to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_trello import trello_source

    await cognee.remember(
        trello_source(
            board_ids=["<board id or short link from the board URL>"],
            api_key="…",   # or TRELLO_API_KEY
            token="…",     # or TRELLO_TOKEN
        ),
        dataset_name="my_boards",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by card id
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — Trello API key + token (read scope). Pass ``api_key`` / ``token``
  or set ``TRELLO_API_KEY`` / ``TRELLO_TOKEN``. They are sent as query
  parameters on every request, and the connector only issues ``GET`` requests.
* **Documents** — one document per open card (description, comments, and
  checklists rendered to markdown, tagged with its list) and one overview
  document per board (name, description, lists, labels).
* **Primary key** — the Trello card id (or board id for overview documents).
  Combined with ``write_disposition="merge"`` this gives idempotent upserts,
  and cognee's content-hash ``data_id`` keeps unchanged documents from being
  re-cognified.
* **Incremental cursor** — the board's *actions feed*, per the issue spec.
  Cards carry no reliable updated timestamp, so each run fetches the actions
  since the last stored action id and re-syncs the cards those actions touch
  (comment edits, checklist updates, moves, ... all surface as card-affecting
  actions). The cursor is persisted in dlt's per-resource state, so re-running
  ``remember`` resumes where it left off and re-embeds only the delta. Cards
  added to the board between runs are caught by the sweep even if their
  actions scrolled past the cursor; card documents are also content-hashed,
  so a re-emit of unchanged content is a no-op downstream.
* **Forget-on-delete** — each run does a cheap id sweep of every fetched
  board's open cards and compares it against the ids seen on the previous run
  (kept in resource state). Cards that vanished — deleted or archived — are
  emitted with the ``_deleted`` hard-delete marker; dlt removes those rows on
  ``merge`` and cognee's existing ``orphan_cleanup`` purges them from the
  graph + vector + relational stores. Removing a board from ``board_ids``
  tombstones all of its documents. A board that fails to fetch is skipped for
  the run (its documents are never tombstoned on unseen evidence), and a
  single card that fails to fetch is retried on the next run.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50). For real boards pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

.. note::
   Archiving a card removes it from the board's open-card listing, so it is
   treated as an upstream deletion and forgotten from memory. Un-archiving it
   re-ingests it under the same stable id.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("trello_connector")

# dlt resource / staging-table name, and the system_metadata["source"] tag
# stamped on every document this connector produces.
TRELLO_SOURCE_NAME = "trello"
TRELLO_TABLE_NAME = "trello_documents"

_API_BASE = "https://api.trello.com/1"

# A card document carries up to this many comments; older comment history is
# summarized by count rather than ingested in full.
_MAX_COMMENTS_PER_CARD = 100

_BOARD_FIELDS = "name,desc,url"
_CARD_FIELDS = "name,desc,shortUrl,idList"


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(api_key: str, token: str) -> Any:
    """Build a ``requests`` session authenticated with a Trello key + token.

    ``requests`` is imported lazily so it stays an optional dependency.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(
            'The Trello connector requires "requests". Install the connector:\n'
            '    pip install "cognee-community-connector-trello"'
        ) from exc

    session = requests.Session()
    # Trello's simplest auth: key + token query parameters on every request.
    session.params = {"key": api_key, "token": token}
    return session


def _api_get(session: Any, path: str, params: dict | None = None) -> Any:
    """GET a Trello API path and return the decoded JSON (object or array)."""
    response = session.get(f"{_API_BASE}{path}", params=params or {})
    response.raise_for_status()
    return response.json()


def _paginate_actions(session: Any, board_id: str, since: str | None) -> list[dict]:
    """Fetch a board's actions newer than ``since``, following pagination.

    The actions endpoint returns a bare array and pages backwards via
    ``before``; a full page means more pages may exist below it. ``since`` is
    an action id or ISO date (Trello accepts both).
    """
    params: dict[str, Any] = {"limit": 1000}
    if since:
        params["since"] = since
    actions = _api_get(session, f"/boards/{board_id}/actions", params)
    if not isinstance(actions, list) or len(actions) < 1000:
        return actions if isinstance(actions, list) else []

    all_actions = list(actions)
    while True:
        oldest = all_actions[-1]
        page_params: dict[str, Any] = {"limit": 1000, "before": oldest["date"]}
        if since:
            page_params["since"] = since
        page = _api_get(session, f"/boards/{board_id}/actions", page_params)
        if not isinstance(page, list) or not page:
            break
        all_actions.extend(page)
        if len(page) < 1000:
            break
    return all_actions


# ---------------------------------------------------------------------------
# Document rendering
# ---------------------------------------------------------------------------
def _board_content(board: dict, list_names: list[str], labels: list[dict]) -> str:
    """Render the board overview document (name, description, lists, labels)."""
    lines = [board.get("name") or "Untitled board"]
    if board.get("desc"):
        lines.append("")
        lines.append(board["desc"])
    if list_names:
        lines.append("")
        lines.append("## Lists")
        lines.extend(f"- {name}" for name in list_names if name)
    if labels:
        lines.append("")
        lines.append("## Labels")
        lines.extend(
            f"- {label['name']} ({label['color']})" for label in labels if label.get("name")
        )
    return "\n".join(lines).strip()


def _card_content(card: dict, comments: list[dict], checklists: list[dict], list_name: str) -> str:
    """Render a card document (description, list, comments, checklists)."""
    lines = [card.get("name") or "Untitled card"]
    if card.get("desc"):
        lines.append("")
        lines.append(card["desc"])
    if list_name:
        lines.append("")
        lines.append(f"In list: {list_name}")
    if comments:
        lines.append("")
        lines.append("## Comments")
        for comment in comments:
            username = (comment.get("memberCreator") or {}).get("username") or "unknown"
            text = ((comment.get("data") or {}).get("text") or "").strip()
            if text:
                lines.append(f"- **{username}**: {text}")
    for checklist in checklists:
        items = checklist.get("checkItems") or []
        if not checklist.get("name") and not items:
            continue
        lines.append("")
        lines.append(f"## {checklist.get('name') or 'Checklist'}")
        lines.extend(
            f"- [{'x' if item.get('state') == 'complete' else ' '}] {item.get('name', '')}"
            for item in items
        )
    return "\n".join(lines).strip()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _board_row(board_id: str, content: str, board: dict) -> dict[str, Any]:
    return {
        "id": board_id,
        "title": board.get("name") or board_id,
        "content": content,
        "url": board.get("url") or "",
        "_deleted": False,
    }


def _card_row(card: dict, content: str) -> dict[str, Any]:
    return {
        "id": card["id"],
        "title": card.get("name") or card["id"],
        "content": content,
        "url": card.get("shortUrl") or "",
        "_deleted": False,
    }


def _deleted_row(item_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a document by id."""
    return {"id": item_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _list_name_map(session: Any, board_id: str) -> dict[str, str]:
    return {
        list_["id"]: list_.get("name") or ""
        for list_ in _api_get(session, f"/boards/{board_id}/lists", {"fields": "name"})
    }


def _fetch_card_document(
    session: Any, card_id: str, list_names: dict[str, str]
) -> tuple[dict, str] | None:
    """Fetch a card with its comments and checklists, rendered to a document row.

    Returns ``(row, content_hash)``, or ``None`` when the card could not be
    fetched (the caller keeps the card's prior state and retries next run
    instead of treating the failure as a change or a deletion).
    """
    try:
        card = _api_get(session, f"/cards/{card_id}", {"fields": _CARD_FIELDS})
        comments = _api_get(
            session,
            f"/cards/{card_id}/actions",
            {
                "filter": "commentCard",
                "limit": _MAX_COMMENTS_PER_CARD,
                "fields": "data,memberCreator",
            },
        )
        checklists = _api_get(
            session,
            f"/cards/{card_id}/checklists",
            {"fields": "name", "checkItem_fields": "name,state"},
        )
    except Exception as exc:
        logger.warning("Trello: skipping card %s (fetch failed): %s", card_id, exc)
        return None
    if not isinstance(card, dict) or not card.get("id"):
        return None
    content = _card_content(
        card, comments or [], checklists or [], list_names.get(card.get("idList"), "")
    )
    return _card_row(card, content), _content_hash(content)


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_boards(
    session: Any,
    state: dict,
    board_ids: list[str],
    *,
    include_board_documents: bool = True,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new/changed board and card documents, plus hard-delete markers.

    One actions fetch per board (since the stored cursor) identifies the cards
    whose content may have changed; an open-card sweep per board drives
    deletion detection; the board overview is re-emitted when its structure
    hash changes. All state (``boards``/``cards``) is advanced in ``state`` so
    the next run is a no-op when nothing changed. A board whose fetch fails is
    skipped for the run — its documents are neither emitted nor tombstoned on
    that evidence.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(synced_boards=0, skipped_boards=0, emitted=0, deleted=0)

    # Fetching the same board twice would double-emit rows; dedupe, keep order.
    seen_ids: list[str] = []
    for board_id in board_ids:
        if board_id not in seen_ids:
            seen_ids.append(board_id)

    boards_state: dict[str, dict] = dict(state.get("boards", {}))
    cards_state: dict[str, dict] = dict(state.get("cards", {}))
    current_board_ids = set(seen_ids)
    synced_boards: set[str] = set()

    for board_id in seen_ids:
        board_state = dict(boards_state.get(board_id, {}))
        known_cards = {
            card_id: meta for card_id, meta in cards_state.items() if meta.get("board") == board_id
        }
        try:
            board = _api_get(session, f"/boards/{board_id}", {"fields": _BOARD_FIELDS})
            list_names = _list_name_map(session, board_id)
            labels = _api_get(session, f"/boards/{board_id}/labels", {"fields": "name,color"})
            open_cards = _api_get(session, f"/boards/{board_id}/cards", {"fields": "id"})
            actions = _paginate_actions(session, board_id, board_state.get("last_action_id"))
        except Exception as exc:
            stats["skipped_boards"] += 1
            logger.warning("Trello: skipping board %s (fetch failed): %s", board_id, exc)
            continue

        synced_boards.add(board_id)
        present_cards = {c["id"] for c in open_cards if isinstance(c, dict) and c.get("id")}

        # Board overview document: re-emitted only when its structure changes.
        if include_board_documents:
            content = _board_content(board, list_names.values(), labels or [])
            board_hash = _content_hash(content)
            if board_hash != board_state.get("board_hash"):
                stats["emitted"] += 1
                yield _board_row(board_id, content, board)
            board_state["board_hash"] = board_hash

        # Cards possibly changed: anything the actions feed touched, plus any
        # card we have never seen (a card added before the cursor's window, or
        # beyond the actions page cap, is still ingested by the sweep).
        affected = {
            action["data"]["card"]["id"]
            for action in actions
            if isinstance(action, dict) and (action.get("data") or {}).get("card", {}).get("id")
        }
        changed = (affected | (present_cards - set(known_cards))) & present_cards
        for card_id in sorted(changed):
            fetched = _fetch_card_document(session, card_id, list_names)
            if fetched is None:
                continue  # retried next run; never tombstoned off a failed fetch
            row, content_hash = fetched
            previous = known_cards.get(card_id)
            if previous is None or content_hash != previous.get("hash"):
                stats["emitted"] += 1
                yield row
            cards_state[card_id] = {"hash": content_hash, "board": board_id}

        # Deletion detection: a known card absent from the open-card sweep is
        # gone upstream (deleted or archived) — emit the hard-delete marker so
        # dlt's merge drops it and cognee's orphan_cleanup forgets it.
        deleted = set(known_cards) - present_cards
        for card_id in sorted(deleted):
            cards_state.pop(card_id, None)
            stats["deleted"] += 1
            yield _deleted_row(card_id)

        newest_action = actions[0]["id"] if actions and isinstance(actions[0], dict) else None
        if newest_action:
            board_state["last_action_id"] = newest_action
        boards_state[board_id] = board_state
        stats["synced_boards"] += 1

    # Boards removed from the configuration: their documents are no longer
    # wanted, regardless of what this run's fetches did.
    for card_id, meta in list(cards_state.items()):
        if meta.get("board") not in current_board_ids:
            cards_state.pop(card_id, None)
            stats["deleted"] += 1
            yield _deleted_row(card_id)
    for board_id in boards_state:
        if board_id not in current_board_ids and include_board_documents:
            stats["deleted"] += 1
            yield _deleted_row(board_id)
    boards_state = {bid: s for bid, s in boards_state.items() if bid in current_board_ids}

    state["boards"] = boards_state
    state["cards"] = cards_state
    logger.info(
        "Trello: %d board(s) synced, %d document(s) emitted, %d deletion(s), %d board(s) skipped.",
        stats["synced_boards"],
        stats["emitted"],
        stats["deleted"],
        stats["skipped_boards"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def trello_source(
    board_ids: list[str],
    *,
    api_key: str | None = None,
    token: str | None = None,
    include_board_documents: bool = True,
    resource_name: str = TRELLO_TABLE_NAME,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Trello board/card documents for ``remember``.

    Args:
        board_ids: Board ids or short links to sync (the part after ``/b/``
            in the board URL).
        api_key: Trello API key. Falls back to ``TRELLO_API_KEY``.
        token: Trello token authorized for that key. Falls back to ``TRELLO_TOKEN``.
        include_board_documents: Also emit one overview document per board
            (name, description, lists, labels).
        resource_name: Stable dlt resource name. dlt state is keyed per
            resource name, so hosts syncing several board sets into one
            dataset should give each set its own name (and thereby its own
            incremental state and staging table).
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from ``api_key`` / ``token``.

    Returns:
        A ``dlt`` resource (``trello_documents``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Trello connector requires dlt. Install it with the connector:\n"
            '    pip install "cognee-community-connector-trello"'
        ) from exc

    api_key = api_key or os.environ.get("TRELLO_API_KEY")
    token = token or os.environ.get("TRELLO_TOKEN")
    if session is None and not (api_key and token):
        raise ValueError("trello_source requires api_key and token (or an injected session).")
    if not board_ids or not all(isinstance(bid, str) and bid.strip() for bid in board_ids):
        raise ValueError("board_ids must be a non-empty list of board id/short-link strings.")

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker (matching gmail/confluence):
        # rows where it is True are removed from the dlt destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def trello_documents():
        client = session or _make_session(api_key, token)
        resource_state = dlt.current.resource_state()
        yield from sync_boards(
            client,
            resource_state,
            board_ids,
            include_board_documents=include_board_documents,
            stats=stats,
        )

    resource = trello_documents()
    # Opt into the document ingestion path: each row (id/title/content/url)
    # becomes a text document that flows through normal cognify (LLM graph
    # extraction). resolve_dlt_sources reads this marker; it never imports
    # this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, TRELLO_SOURCE_NAME)
    # Host-readable diagnostics contain counts only, never board or card content.
    resource.cognee_sync_stats = stats
    return resource
