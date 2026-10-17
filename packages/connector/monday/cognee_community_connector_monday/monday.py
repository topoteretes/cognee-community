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
    return Client(raise_for_status=False, retry_condition=_is_rate_limited, **client_kwargs).session


def _is_rate_limited(response, exception) -> bool:
    if response is None or response.status_code != 200:
        return False
    return any("retry_in_seconds" in _extensions(error) for error in _errors(response))


def _raise_for_errors(response, *args, **kwargs) -> None:
    """Raise on an HTTP error or a GraphQL error; return quietly on success."""
    if response.status_code in (401, 403):
        raise MondayAuthError(
            f"monday.com refused the token (HTTP {response.status_code}). "
            "Check that it is valid and can read the selected boards."
        )
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
