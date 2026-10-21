"""monday.com connector for cognee: a ``dlt`` source that turns boards into memory.

Sync monday.com boards into cognee, incrementally and with forget-on-delete.
The resource is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_monday import monday_source

    await cognee.remember(
        monday_source(board_ids=[1234567890]),  # token from MONDAY_API_TOKEN
        dataset_name="monday",
        primary_key="id",
        write_disposition="merge",   # incremental upserts and deletes
        max_rows_per_table=0,        # no row cap; a board exceeds the default 50
    )

Design
------
* **Documents** - each item becomes one document on cognee's document path, in
  the shape of the Linear connector in cognee core: board, group, state,
  assignee, parent for sub-items, the other columns, the item description, and
  the updates with their replies. Sub-items live on their own board, which is
  synced with its parent board.
* **Incremental sync** - every run lists each board's items with cheap stamps:
  the item's ``updated_at`` and the id and ``updated_at`` of every update and
  reply. Posting or editing an update does not move the item's ``updated_at``
  and is not in the activity log, so the update stamps are what catch new,
  edited and deleted comments. Only items whose stamps changed are fetched.
* **Forget-on-delete** - an item missing from a complete listing is looked up
  by id, and forgotten with a ``_deleted`` row only when monday.com confirms it
  is archived, deleted, gone, or moved off the synced boards. A deleted or
  archived board, or one dropped from ``board_ids``, takes its items with it.
* **Errors never delete** - a board the token cannot see is reported as
  failed, never as empty. A failing board keeps its state while the others
  finish, and the run only raises when no board finished or the token is
  refused.
* **Rate limits** - monday.com's ``RateLimit`` header and ``retry_in_seconds``
  are honored, and listing queries are sized to stay inside the per-minute
  complexity budget.

monday.com has a single GraphQL endpoint, so the declarative ``rest_api`` source
does not fit the sync above (list, compare, fetch what changed, confirm what is
gone). The client is still built on dlt's REST toolkit: ``RESTClient`` for the
requests, dlt's retrying session for rate limits and server errors, and dlt's
cursor paginator, which writes monday.com's page cursor into the query variables.

Limitations
-----------
* monday.com gives no signal when only an item's description is edited, so a
  description is re-read when its item changes for any other reason, or on a
  ``full_refresh``.
* The newest 100 updates of an item are read. Files and their contents are not.
"""

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.sources.helpers.requests.retry import Client
from dlt.sources.helpers.rest_client.client import RESTClient
from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator

logger = get_logger("monday_connector")

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


def monday_source(
    board_ids: list[str | int] | str | int | None = None,
    *,
    token: str | None = None,
    full_refresh: bool = False,
    resource_name: str = "monday_items",
    check_active: Callable[[], None] | None = None,
    client: Any = None,
):
    """Return a ``dlt`` resource yielding one row per new or changed monday.com item.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``,
    ``primary_key="id"`` and ``max_rows_per_table=0``.

    Args:
        board_ids: Boards to sync, by id (the number in the board's URL). Their
            sub-item boards are synced with them. ``MONDAY_BOARD_IDS`` takes a
            comma-separated list.
        token: A personal API token or an OAuth access token with read access to
            the boards. Falls back to ``MONDAY_API_TOKEN``.
        full_refresh: Re-read every item, for example to pick up edited item
            descriptions. Unchanged documents are not re-processed by cognee.
        resource_name: Stable dlt resource name. Give each account its own name
            when several sync into one dataset, so their state stays separate.
            A name that already holds another account's sync is refused.
        check_active: Optional host authorization checkpoint, run before every
            API call and every row.
        client: A pre-built client, mainly for tests.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "The monday.com connector requires a cognee build with table-scoped dlt "
            "document cleanup. Upgrade cognee before syncing."
        )

    boards = _normalize_board_ids(board_ids)
    if client is None:
        # Built here, so a missing or malformed token fails when the source is
        # created, not halfway through a pipeline run.
        resolved = token or os.environ.get("MONDAY_API_TOKEN")
        if not resolved:
            raise MondayAuthError(
                "A monday.com API token is required: pass token= or set MONDAY_API_TOKEN."
            )
        client = _MondayClient(resolved, check_active=check_active)

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, and cognee's orphan
        # cleanup then forgets their documents.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def monday_items():
        stats.clear()
        rows = _iter_rows(client, boards, dlt.current.resource_state(), stats, full_refresh)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = monday_items()
    # Opt into the document ingestion path: each item becomes a text document
    # that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, "monday")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics: counts only, never names or content.
    resource.cognee_sync_stats = stats
    return resource


_LIST_BOARDS_QUERY = """
query ListBoards($page: Int!, $limit: Int!) {
  boards(page: $page, limit: $limit, state: active) { id name type workspace { name } }
}"""
_LIST_BOARDS_PAGE_SIZE = 100


def list_boards(token: str | None = None, *, client: Any = None) -> list[dict]:
    """The active boards a token can read, as ``{"id", "name", "workspace"}``.

    Use it to find the ids for ``board_ids``. Sub-item boards are left out:
    they are synced together with their parent board.
    """
    if client is None:
        resolved = token or os.environ.get("MONDAY_API_TOKEN")
        if not resolved:
            raise MondayAuthError(
                "A monday.com API token is required: pass token= or set MONDAY_API_TOKEN."
            )
        client = _MondayClient(resolved)

    boards, page = [], 1
    while True:
        variables = {"page": page, "limit": _LIST_BOARDS_PAGE_SIZE}
        found = client.query(_LIST_BOARDS_QUERY, variables)["boards"] or []
        boards.extend(
            {
                "id": str(board["id"]),
                "name": board.get("name") or "",
                "workspace": (board.get("workspace") or {}).get("name") or "",
            }
            for board in found
            if board.get("type") != "sub_items_board"
        )
        if len(found) < _LIST_BOARDS_PAGE_SIZE:
            return boards
        page += 1


def _normalize_board_ids(board_ids) -> list[str]:
    if board_ids is None:
        board_ids = os.environ.get("MONDAY_BOARD_IDS", "").split(",")
    elif isinstance(board_ids, (str, int)):
        board_ids = [board_ids]
    cleaned = [str(board_id).strip() for board_id in board_ids if str(board_id).strip()]
    if not cleaned:
        # An empty selection would forget every board synced before, so refuse it.
        raise ValueError("board_ids is empty. Pass boards like board_ids=[1234567890].")
    if not all(board_id.isdigit() for board_id in cleaned):
        raise ValueError("board_ids must be numeric monday.com board ids.")
    return list(dict.fromkeys(cleaned))


# ---------------------------------------------------------------------------
# GraphQL client (module-private)
# ---------------------------------------------------------------------------


class _MondayClient:
    """A thin GraphQL client over dlt's ``RESTClient``.

    monday.com reports most failures inside an HTTP 200 response, as an
    ``errors`` list next to (or instead of) ``data``. Every response goes through
    ``_raise_for_errors`` so such a reply can never be read as an empty result.
    """

    def __init__(
        self,
        token: str,
        session: Any = None,
        check_active: Callable[[], None] | None = None,
    ):
        headers = {"Authorization": _clean_token(token), "API-Version": API_VERSION}
        self._rest = RESTClient(
            base_url=API_URL, headers=headers, session=session or _retrying_session()
        )
        # A host's authorization checkpoint, run before every request so a
        # revoked connection stops the sync at the next call, not at its end.
        self._check_active = check_active or (lambda: None)

    def query(self, query: str, variables: dict | None = None) -> dict:
        """Run one query and return its ``data``."""
        self._check_active()
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
        while True:
            # Pages are requested lazily, so this runs before each request.
            self._check_active()
            page = next(pages, None)
            if page is None:
                return
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

# The account the token belongs to, which the saved state is tied to.
_ACCOUNT_QUERY = "query Account { account { id } }"

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
    is synced with it, and remembers which board it belongs to.

    A board that fails keeps its saved state and is retried on the next run,
    while the other boards finish. Raising would make dlt drop the state of the
    whole run, so boards that did finish would lose their progress. The run only
    raises when no board finished, or when the token itself is refused.
    """
    selected = list(dict.fromkeys(str(board_id) for board_id in board_ids))
    _claim_state(client, state)
    saved = state.setdefault("boards", {})
    boards, failed = _load_boards(client, selected), set()
    failed.update(board_id for board_id in selected if board_id not in boards)

    parent_of = {
        sub_id: str(board["id"])
        for board in _active(boards)
        for sub_id in _subitem_board_ids(board)
    }
    subitem_boards = _load_boards(client, [i for i in parent_of if i not in boards])
    failed.update(i for i in parent_of if i not in boards and i not in subitem_boards)
    boards.update(subitem_boards)

    # A board deleted or archived upstream, or no longer selected, takes all of
    # its items with it. A board that failed to load is never forgotten.
    for board_id in list(saved):
        if _is_gone(board_id, saved[board_id], boards, selected, failed):
            yield from _forget_items(saved.pop(board_id).get("items", {}), stats)

    synced = 0
    for board in _active(boards):
        board_id = str(board["id"])
        board_state = saved.setdefault(board_id, {})
        if board_id in parent_of:
            board_state["parent"] = parent_of[board_id]
        try:
            yield from _sync_board(client, board, board_state, stats, full_refresh, boards)
            synced += 1
        except MondayAuthError:
            raise
        except (MondayAPIError, requests.RequestException) as error:
            failed.add(board_id)
            logger.warning(
                "monday.com: a board failed to sync (%s); it keeps its state and is "
                "retried on the next run.",
                type(error).__name__,
            )

    if failed:
        stats["failed"] = len(failed)
    logger.info(
        "monday.com: %d item(s) scanned, %d changed, %d forgotten, %d board(s) failed.",
        stats.get("scanned", 0),
        stats.get("changed", 0),
        stats.get("deleted", 0),
        len(failed),
    )
    if failed and not synced:
        raise MondayAPIError(
            f"No monday.com board could be synced: {len(failed)} board(s) were not found, "
            "are not shared with this token, or failed. Nothing was forgotten."
        )


def _claim_state(client, state: dict) -> None:
    """Tie the saved state to one monday.com account, or refuse another one.

    Boards in the state that are not selected get forgotten, so a second account
    syncing under the same resource name would forget the first one's boards,
    and the next run of the first would forget the second's.
    """
    account = (client.query(_ACCOUNT_QUERY).get("account") or {}).get("id")
    if not account:
        raise MondayAPIError("monday.com did not say which account the token belongs to.")
    if state.setdefault("account", str(account)) != str(account):
        raise MondayAPIError(
            "This resource_name already holds the sync of another monday.com account. "
            "Give each account its own resource_name. Nothing was read or forgotten."
        )


def _is_gone(board_id, board_state, boards, selected, failed) -> bool:
    """Whether a previously synced board should now be forgotten."""
    root = board_state.get("parent") or board_id
    if board_id in failed or root in failed:
        return False
    if root not in selected:
        return True
    board = boards.get(board_id)
    return board is None or board.get("state") != "active"


def _active(boards: dict[str, dict]) -> list[dict]:
    return [board for board in boards.values() if board.get("state") == "active"]


def _load_boards(client, board_ids: list[str]) -> dict[str, dict]:
    """Board names, states and columns, for the boards monday.com returned.

    A board the token cannot see is simply missing from the answer, and the
    caller treats it as failed: that must never be read as a board whose items
    were all deleted.
    """
    if not board_ids:
        return {}
    found = client.query(_BOARDS_QUERY, {"ids": board_ids})["boards"] or []
    boards = {str(board["id"]): board for board in found}
    return {board_id: boards[board_id] for board_id in board_ids if board_id in boards}


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
