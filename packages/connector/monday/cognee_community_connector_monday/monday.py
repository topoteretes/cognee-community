"""DLT source for monday.com boards (incremental sync + forget-on-delete).

Work in progress. The planned design, based on tests against a real account:

* Each item becomes one document on cognee's document path, in the same shape
  as the Linear connector: a state line, an assignee line, a parent line for
  sub-items, the other columns, and the item's updates folded in.
* Every sync lists the ids and last-updated times of all active items, and
  fetches full details only for items that changed.
* Posting or editing an update does not change an item's last-updated time and
  is not in the activity log, so updates are read as their own stream.
* An id missing from a complete listing is looked up by id. Archived or deleted
  items get a ``_deleted`` marker so cognee forgets them. A failed or partial
  listing never deletes anything.

monday.com has a single GraphQL endpoint, so the declarative ``rest_api`` source
does not fit the sync above (list, compare, fetch what changed, confirm what is
gone). The client is still built on dlt's REST toolkit: ``RESTClient`` for the
requests, dlt's retrying session for rate limits and server errors, and dlt's
cursor paginator, which writes monday.com's page cursor into the query variables.
"""

import hashlib
import json
from collections.abc import Iterator
from typing import Any

from dlt.sources.helpers.requests.retry import Client
from dlt.sources.helpers.rest_client.client import RESTClient
from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator

API_URL = "https://api.monday.com/v2"
# Pinned so a schema change upstream can't silently alter parsing. 2026-07 is the
# current stable version, and every query in this module was checked against it.
API_VERSION = "2026-07"

# GraphQL error codes that mean the token itself is not usable.
_AUTH_ERROR_CODES = {"NOT_AUTHENTICATED", "UNAUTHORIZED", "USER_UNAUTHORIZED"}


class MondayAPIError(RuntimeError):
    """monday.com rejected a request. Messages never carry the token or variables."""


class MondayAuthError(MondayAPIError):
    """The token is missing, malformed, revoked, or lacks access."""


def monday_source(*args, **kwargs):
    """Create a dlt source that yields monday.com items as documents."""
    raise NotImplementedError("The monday.com connector is still being built.")


# ---------------------------------------------------------------------------
# GraphQL client (module-private)
# ---------------------------------------------------------------------------


class _MondayClient:
    """A thin GraphQL client over dlt's ``RESTClient``.

    monday.com reports most failures inside an HTTP 200 response, as an
    ``errors`` list next to (or instead of) ``data``. Every response goes through
    ``_raise_for_errors`` so such a reply can never be read as an empty result.
    """

    def __init__(self, token: str, session: Any = None):
        headers = {"Authorization": _clean_token(token), "API-Version": API_VERSION}
        self._rest = RESTClient(
            base_url=API_URL, headers=headers, session=session or _retrying_session()
        )

    def query(self, query: str, variables: dict | None = None) -> dict:
        """Run one query and return its ``data``."""
        response = self._rest.request(
            "", method="POST", json={"query": query, "variables": variables or {}}
        )
        _raise_for_errors(response)
        return response.json()["data"]

    def paginate(
        self, query: str, variables: dict, cursor_path: str, items_path: str
    ) -> Iterator[dict]:
        """Yield items across cursor pages of one query.

        The query must take a ``$cursor`` variable and return the next cursor at
        ``cursor_path``; a null cursor ends the walk.
        """
        paginator = JSONResponseCursorPaginator(
            cursor_path=cursor_path, cursor_body_path="variables.cursor"
        )
        pages = self._rest.paginate(
            "",
            method="POST",
            json={"query": query, "variables": dict(variables)},
            paginator=paginator,
            data_selector=items_path,
            hooks={"response": [_raise_for_errors]},
        )
        for page in pages:
            yield from page


def _retrying_session(**client_kwargs):
    """dlt's retrying session, also retrying monday.com's in-body rate limit errors.

    HTTP 429 and 5xx are retried by dlt with backoff, honoring ``Retry-After``.
    A rate limit can also arrive as a 200 whose error carries ``retry_in_seconds``.
    ``client_kwargs`` override dlt's retry settings (tests turn the backoff off).
    """
    session = Client(
        raise_for_status=False, retry_condition=_is_rate_limited, **client_kwargs
    ).session
    session.hooks["response"].append(_set_retry_after)
    return session


def _is_rate_limited(response, exception) -> bool:
    if response is None or response.status_code != 200:
        return False
    return any("retry_in_seconds" in _extensions(error) for error in _errors(response))


def _set_retry_after(response, *args, **kwargs):
    """Tell dlt how long monday.com wants us to wait, as a ``Retry-After`` header.

    When the per-minute complexity budget runs out, monday.com answers 429 with
    an HTML page and no ``Retry-After``; the wait is only in the ``RateLimit``
    header, such as ``"complexityMinute";r=0;t=46``. An in-body rate limit
    carries ``retry_in_seconds`` instead. Without this, dlt's default backoff
    gives up after about 15 seconds, long before the budget refills.
    """
    if "Retry-After" in response.headers:
        return response
    wait = None
    if response.status_code == 429:
        wait = _exhausted_rate_limit_wait(response.headers.get("RateLimit"))
    elif _is_rate_limited(response, None):
        wait = max(int(_extensions(e).get("retry_in_seconds") or 0) for e in _errors(response))
    if wait:
        response.headers["Retry-After"] = str(wait)
    return response


def _exhausted_rate_limit_wait(header: str | None) -> int | None:
    """Seconds until the longest exhausted policy in a ``RateLimit`` header resets.

    The header lists policies like ``"minuteRate";r=1331, "complexityMinute";r=0;t=46``,
    where ``r`` is what is left and ``t`` the seconds until it resets.
    """
    waits = []
    for policy in (header or "").split(","):
        params = dict(part.strip().split("=", 1) for part in policy.split(";")[1:] if "=" in part)
        if params.get("r") == "0" and params.get("t", "").isdigit():
            waits.append(int(params["t"]))
    return max(waits, default=None)


def _raise_for_errors(response, *args, **kwargs) -> None:
    """Raise on an HTTP error or a GraphQL error; return quietly on success."""
    if response.status_code in (401, 403):
        raise MondayAuthError(
            f"monday.com refused the token (HTTP {response.status_code}). "
            "Check that it is valid and can read the selected boards."
        )
    if response.status_code == 429:
        raise MondayAPIError("monday.com rate limit still exceeded after retrying (HTTP 429).")
    if response.status_code != 200:
        raise MondayAPIError(f"monday.com returned HTTP {response.status_code}.")

    errors = _errors(response)
    if not errors:
        if not isinstance(_json(response).get("data"), dict):
            raise MondayAPIError("monday.com returned a response without data.")
        return

    codes = sorted({str(_extensions(error).get("code") or "UNKNOWN") for error in errors})
    message = "; ".join(str(error.get("message", "")) for error in errors)
    error_class = MondayAuthError if _AUTH_ERROR_CODES.intersection(codes) else MondayAPIError
    raise error_class(f"monday.com rejected the query ({', '.join(codes)}): {message}")


def _errors(response) -> list[dict]:
    body = _json(response)
    errors = body.get("errors") or []
    # Some errors still come in the older shape, a top-level error_code.
    if not errors and body.get("error_code"):
        errors = [
            {"message": body.get("error_message", ""), "extensions": {"code": body["error_code"]}}
        ]
    return [error for error in errors if isinstance(error, dict)]


def _extensions(error: dict) -> dict:
    extensions = error.get("extensions")
    return extensions if isinstance(extensions, dict) else {}


def _json(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _clean_token(token: str | None) -> str:
    """Validate a token without ever echoing it.

    Personal API tokens and OAuth access tokens are both sent as is in the
    ``Authorization`` header.
    """
    cleaned = (token or "").strip()
    if not cleaned:
        raise MondayAuthError("A monday.com API token is required.")
    if not cleaned.isascii() or any(character.isspace() for character in cleaned):
        raise MondayAuthError("The monday.com API token contains spaces or non-ASCII characters.")
    return cleaned


# ---------------------------------------------------------------------------
# Rendering: one document per item, deterministic, no volatile fields.
#
# The shape follows the Linear connector in cognee core ("Label: value" lines,
# then the text, then the discussion), so work items from both trackers read
# the same way in the graph.
# ---------------------------------------------------------------------------

ID_PREFIX = "monday:"

# Columns that are never written into a document:
# * name and subtasks are rendered as the title and the Parent line instead.
# * last_updated changes on every edit and time_tracking while a timer runs, so
#   they would make every sync look like a change.
# * file text is a list of signed download links, and button has no value.
_SKIPPED_COLUMN_TYPES = {"name", "subtasks", "last_updated", "time_tracking", "file", "button"}

# Preferred titles for the columns rendered as State and Assignee, in order.
# These are monday.com's default titles for its status and people columns.
_STATE_TITLES = ("status",)
_ASSIGNEE_TITLES = ("owner", "person", "assignee", "people")

# Columns holding a comma separated set, where order carries no meaning.
_MULTI_VALUE_COLUMN_TYPES = {"people", "dropdown", "tags"}

# Description blocks, rendered as markdown so headings and lists survive. The
# API names block types with spaces ("large title", "bulleted list").
_BLOCK_PREFIX = {
    "large title": "# ",
    "medium title": "## ",
    "small title": "### ",
    "bulleted list": "- ",
    "numbered list": "1. ",
    "check list": "- [ ] ",
    "quote": "> ",
}


def _render_item(item: dict, board: dict, updates: list[dict]) -> dict:
    """Render one monday.com item, with its updates, as a document row.

    ``board`` carries the board name and its columns in board order; ``updates``
    are the item's updates with their replies. Only text a person entered is
    used, never ``updated_at`` or a column's raw value (that JSON carries a
    ``changed_at`` time), so an unchanged item renders to the same text.
    """
    name = str(item.get("name") or "").strip() or "Untitled item"
    columns = _columns_by_id(item)
    # One status and one people column become the State and Assignee lines, as
    # in the Linear connector. Any other column keeps its own title.
    state_column, state = _pick_column(board, columns, "status", _STATE_TITLES)
    assignee_column, assignee = _pick_column(board, columns, "people", _ASSIGNEE_TITLES)
    fields = [
        ("Board", str(board.get("name") or "").strip()),
        ("Group", str((item.get("group") or {}).get("title") or "").strip()),
        ("State", state),
        ("Assignee", assignee),
        ("Parent", str((item.get("parent_item") or {}).get("name") or "").strip()),
    ]
    used = {state_column, assignee_column}
    # Ordered by column id, not board position: dragging a column on the board
    # must not change the text of every item and re-extract the whole board.
    for column in sorted(board.get("columns") or [], key=lambda c: str(c.get("id"))):
        if column.get("id") in used or column.get("type") in _SKIPPED_COLUMN_TYPES:
            continue
        fields.append((str(column.get("title") or "").strip(), columns.get(column.get("id"), "")))

    parts = [f"{label}: {value}" for label, value in fields if label and value]
    description = _render_description(item.get("description"))
    if description:
        parts.extend(["", description])
    discussion = _render_updates(updates)
    if discussion:
        parts.extend(["", "Updates:", *discussion])

    return {
        "id": f"{ID_PREFIX}{item['id']}",
        "title": name,
        "content": "\n".join(parts).strip() or name,
        "url": item.get("url") or "",
        "_deleted": False,
    }


def _columns_by_id(item: dict) -> dict[str, str]:
    """Readable value of each column. Formula, mirror and connected-board
    columns leave ``text`` empty and carry their value in ``display_value``."""
    values = {}
    for column in item.get("column_values") or []:
        value = " ".join(str(column.get("display_value") or column.get("text") or "").split())
        if column.get("type") in _MULTI_VALUE_COLUMN_TYPES and value:
            # Sorted, so reordering the same people or labels is not a change.
            value = ", ".join(sorted(part.strip() for part in value.split(",")))
        values[column.get("id")] = value
    return values


def _pick_column(
    board: dict, columns: dict[str, str], column_type: str, titles: tuple[str, ...]
) -> tuple[str, str]:
    """The column of a type that plays the State or Assignee role, as
    (column id, value).

    A column with one of the usual titles wins, then the lowest column id.
    Board position is never used: dragging a second status column to the front
    must not turn it into the item's state.
    """
    candidates = [c for c in board.get("columns") or [] if c.get("type") == column_type]
    if not candidates:
        return "", ""

    def rank(column):
        title = str(column.get("title") or "").strip().lower()
        return (titles.index(title) if title in titles else len(titles), str(column.get("id")))

    chosen = min(candidates, key=rank)
    return chosen.get("id"), columns.get(chosen.get("id"), "")


def _render_description(description: Any) -> str:
    """The item description, a small monday doc, as markdown in block order."""
    blocks = (description or {}).get("blocks") if isinstance(description, dict) else None
    lines = []
    for block in sorted(blocks or [], key=lambda b: (float(b.get("position") or 0), str(b["id"]))):
        line = _render_block(block)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _render_block(block: dict) -> str:
    """One description block. Its content is JSON with the text as a list of
    ``deltaFormat`` inserts, and list items carry an ``indentation`` level."""
    content = block.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return content.strip()
    if not isinstance(content, dict):
        return ""
    inserts = content.get("deltaFormat") or []
    text = "".join(str(part.get("insert") or "") for part in inserts if isinstance(part, dict))
    text = " ".join(text.split())
    if not text:
        return ""
    indent = "  " * int(content.get("indentation") or 0)
    return indent + _BLOCK_PREFIX.get(block.get("type"), "") + text


def _render_updates(updates: list[dict]) -> list[str]:
    """Updates oldest first, each followed by its replies."""
    lines = []
    for update in _oldest_first(updates):
        line = _update_line(update)
        if line:
            lines.append(line)
            lines.extend(
                f"  Reply from {reply}"
                for reply in map(_update_line, _oldest_first(update.get("replies")))
                if reply
            )
    return lines


def _oldest_first(updates: list[dict] | None) -> list[dict]:
    return sorted(updates or [], key=lambda u: (str(u.get("created_at") or ""), str(u.get("id"))))


def _update_line(update: dict) -> str:
    body = " ".join(str(update.get("text_body") or "").split())
    if not body:
        return ""
    author = str((update.get("creator") or {}).get("name") or "").strip() or "Unknown"
    return f"{author} ({str(update.get('created_at') or '')[:10]}): {body}"


# ---------------------------------------------------------------------------
# Change detection
#
# Every sync lists each board's items with a few cheap stamps: the item's
# updated_at, plus the id and updated_at of each update and reply. Posting or
# editing an update does not move the item's updated_at, so the update stamps
# are what catch new, edited and deleted comments. Items whose stamps differ
# from the last sync are then fetched in full, a hundred per call.
#
# An item description has no cheap change signal at all (the item's updated_at
# does not move and its blocks only carry a date), so a description is re-read
# whenever its item changes for any other reason, and on a full refresh.
# ---------------------------------------------------------------------------

# monday.com's maximum page size for items_page, and for items(ids).
_LISTING_PAGE_SIZE = 500
_FETCH_CHUNK = 100

# state: all, because an archived or deleted board otherwise vanishes from the
# answer exactly like a board the token cannot see, and its items still report
# themselves active. With it, a deleted board comes back with state "deleted".
_BOARDS_QUERY = """
query Boards($ids: [ID!]) {
  boards(ids: $ids, state: all) { id name state columns { id title type settings_str } }
}"""

# Measured on API 2026-07: a 500 item page with these stamps stays far inside
# the per-minute complexity budget. Asking for description blocks here does not:
# that alone used up the whole minute's budget.
_STAMPS_QUERY = """
query ItemStamps($board: [ID!], $limit: Int!, $cursor: String) {
  boards(ids: $board) {
    items_page(limit: $limit, cursor: $cursor) {
      cursor
      items { id updated_at updates(limit: 100) { id updated_at replies { id updated_at } } }
    }
  }
}"""

_ITEM_STATES_QUERY = """
query ItemStates($ids: [ID!], $limit: Int!) {
  items(ids: $ids, limit: $limit, exclude_nonactive: false) { id state board { id } }
}"""

_ITEMS_QUERY = """
query ItemDetails($ids: [ID!], $limit: Int!) {
  items(ids: $ids, limit: $limit) {
    id name url state board { id } group { title } parent_item { name }
    description { blocks { id type position content } }
    column_values {
      id type text
      ... on FormulaValue { display_value }
      ... on MirrorValue { display_value }
      ... on BoardRelationValue { display_value }
      ... on DependencyValue { display_value }
    }
    updates(limit: 100) {
      id text_body created_at creator { name }
      replies { id text_body created_at creator { name } }
    }
  }
}"""


def _iter_rows(client, board_ids: list[str], state: dict, stats: dict, full_refresh=False):
    """Yield a row for every new or changed item, and a ``_deleted`` row for every
    item that is gone, on the selected boards.

    Sub-items live on their own board, so each selected board's sub-item board
    is synced with it. ``state`` is dlt's resource state; it is only written
    once a board's rows have all been yielded.
    """
    boards = _load_boards(client, board_ids)
    subitem_ids = [i for board in _active(boards) for i in _subitem_board_ids(board)]
    boards.update(_load_boards(client, [i for i in subitem_ids if i not in boards]))
    saved = state.setdefault("boards", {})

    # A board that was deleted or archived upstream, or is no longer selected,
    # takes all of its items with it.
    for board_id in list(saved):
        if board_id not in boards or boards[board_id].get("state") != "active":
            yield from _forget_items(saved.pop(board_id).get("items", {}), stats)

    for board in _active(boards):
        board_state = saved.setdefault(str(board["id"]), {})
        yield from _sync_board(client, board, board_state, stats, full_refresh, boards)


def _active(boards: dict[str, dict]) -> list[dict]:
    return [board for board in boards.values() if board.get("state") == "active"]


def _load_boards(client, board_ids: list[str]) -> dict[str, dict]:
    """Board names, states and columns. A board the token cannot see raises:
    monday.com leaves it out of the answer rather than returning an error, and
    that must never be read as a board whose items were all deleted."""
    if not board_ids:
        return {}
    found = client.query(_BOARDS_QUERY, {"ids": board_ids})["boards"] or []
    boards = {str(board["id"]): board for board in found}
    missing = [board_id for board_id in board_ids if board_id not in boards]
    if missing:
        raise MondayAPIError(
            f"{len(missing)} selected board(s) were not found or are not shared with this token."
        )
    return {board_id: boards[board_id] for board_id in board_ids}


def _subitem_board_ids(board: dict) -> list[str]:
    """The boards holding this board's sub-items, from its subtasks column."""
    ids = []
    for column in board.get("columns") or []:
        if column.get("type") != "subtasks":
            continue
        try:
            settings = json.loads(column.get("settings_str") or "{}")
        except ValueError:
            continue
        ids.extend(str(board_id) for board_id in settings.get("boardIds") or [])
    return ids


def _sync_board(client, board, board_state, stats, full_refresh, boards):
    board_id = str(board["id"])
    stamps = _list_stamps(client, board_id)
    seen = board_state.setdefault("items", {})
    changed = [i for i, stamp in stamps.items() if full_refresh or seen.get(i) != stamp]
    stats["scanned"] = stats.get("scanned", 0) + len(stamps)

    missing = [item_id for item_id in seen if item_id not in stamps]
    gone, moved = _confirm_gone(client, missing, board_id, boards)
    for item_id in moved:
        # Moved to another selected board, which lists it as new and emits it.
        del seen[item_id]
    yield from _forget_items({item_id: seen.pop(item_id) for item_id in gone}, stats)

    for item in _fetch_items(client, changed):
        # An item archived, deleted or moved after the listing is left for the
        # next sync, which will see it gone from this board.
        if item.get("state") != "active" or str((item.get("board") or {}).get("id")) != board_id:
            continue
        stats["changed"] = stats.get("changed", 0) + 1
        yield _render_item(item, board, item.get("updates") or [])

    seen.update(stamps)


def _list_stamps(client, board_id: str) -> dict[str, str]:
    """Every active item on a board, mapped to a short hash of its stamps."""
    items = client.paginate(
        _STAMPS_QUERY,
        {"board": [board_id], "limit": _LISTING_PAGE_SIZE},
        cursor_path="data.boards[0].items_page.cursor",
        items_path="data.boards[0].items_page.items",
    )
    return {str(item["id"]): _stamp(item) for item in items}


def _stamp(item: dict) -> str:
    parts = [str(item.get("updated_at") or "")]
    for update in sorted(item.get("updates") or [], key=lambda u: str(u.get("id"))):
        parts.append(f"{update.get('id')}@{update.get('updated_at')}")
        for reply in sorted(update.get("replies") or [], key=lambda r: str(r.get("id"))):
            parts.append(f"{reply.get('id')}@{reply.get('updated_at')}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _fetch_items(client, item_ids: list[str]) -> Iterator[dict]:
    for start in range(0, len(item_ids), _FETCH_CHUNK):
        chunk = item_ids[start : start + _FETCH_CHUNK]
        yield from client.query(_ITEMS_QUERY, {"ids": chunk, "limit": _FETCH_CHUNK})["items"] or []


def _confirm_gone(client, item_ids: list[str], board_id: str, boards: dict[str, dict]):
    """Look up items that dropped out of a complete listing before forgetting them.

    Returns (gone, moved). An item is gone when monday.com reports it archived
    or deleted, no longer returns it at all, or it moved to a board that is not
    being synced. An item that is still active on this same board contradicts
    the listing, so it is kept rather than forgotten on one signal.
    """
    gone, moved = [], []
    found = {}
    for start in range(0, len(item_ids), _FETCH_CHUNK):
        chunk = item_ids[start : start + _FETCH_CHUNK]
        answer = client.query(_ITEM_STATES_QUERY, {"ids": chunk, "limit": _FETCH_CHUNK})
        found.update({str(item["id"]): item for item in answer["items"] or []})

    for item_id in item_ids:
        item = found.get(item_id)
        if item is None or item.get("state") != "active":
            gone.append(item_id)
            continue
        now_on = str((item.get("board") or {}).get("id"))
        if now_on == board_id:
            continue
        if now_on in boards and boards[now_on].get("state") == "active":
            moved.append(item_id)
        else:
            gone.append(item_id)
    return gone, moved


def _forget_items(items: dict, stats: dict) -> Iterator[dict]:
    """``_deleted`` rows, which make cognee forget the documents on merge."""
    for item_id in sorted(items):
        stats["deleted"] = stats.get("deleted", 0) + 1
        yield {"id": f"{ID_PREFIX}{item_id}", "_deleted": True}
