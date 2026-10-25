"""Trello connector for cognee: a ``dlt`` source that turns boards into memory.

Sync Trello boards (cards with their descriptions, checklists and comments, plus
each board's structure) into cognee, incrementally and with forget-on-delete.
The resource is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_trello import trello_source

    await cognee.remember(
        trello_source(board_ids=["<board id>"], api_key=key, token=token),
        dataset_name="trello",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Auth** is an API key plus a user token, sent in the ``Authorization`` header
  so the token never shows up in URLs or logs.
* **Rows** are flat ``{id, title, content, url, _deleted}`` plus the board as a
  node set: one per card (``card:<id>``) and one per board (``board:<id>``).
  Card titles stay out of the board document, so a new card does not re-process
  the board, and everything is sorted so the API's ordering never changes a row.
* **Incremental.** Cards carry no reliable updated timestamp, so the board
  ``actions`` feed drives the sync: its newest action id is the cursor. A board
  with no new action and the same ``dateLastActivity`` costs two requests. A
  changed board is read again (cards, lists, labels, checklists) and diffed card
  by card, which also catches the action types Trello leaves out of the feed
  (checklist items, labels, deleted comments through the comment count).
  Comments are read only for the cards that changed.
* **Forget-on-delete.** A deleted, hidden or deselected board is forgotten right
  after the board listing. Card tombstones are decided once a pass has visited
  every selected board, which can span several runs when the budget cuts them
  short, so a card moved between two boards is never lost. A board listed in
  ``board_ids`` that cannot be read raises instead of being forgotten.
* **A run is bounded.** On a rate limit, an expired token after progress, or a
  spent request budget it stops cleanly, keeps the boards and cards it finished,
  and reports why in ``cognee_sync_stats``; the next run starts after the last
  board it finished.

Privacy
-------
A Trello token grants access to the whole account. Nothing is fetched until you
construct a source and call ``remember``. Authorize it read-only, and remember
that anyone with read access to the target dataset can read what was ingested.
"""

import copy
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, NODE_SET_COLUMN

logger = get_logger("trello_connector")

API_URL = "https://api.trello.com/1"
# Largest ``limit`` the board actions endpoint accepts.
_ACTIONS_PAGE = 1000
# Card actions come in fixed pages of 50 and pages 0 to 19, so 1000 per card at most.
_CARD_ACTIONS_PAGE = 50
_CARD_ACTIONS_PAGES = 20
_CARD_ACTIONS_MAX = _CARD_ACTIONS_PAGE * _CARD_ACTIONS_PAGES
# Trello's gateway gives up on a request after 30 seconds.
_TIMEOUT = 30.0
# Attempts per request; backing off 1, 2, 4 and 8 seconds gives up after about 15.
_RETRIES = 5
# Trello allows 100 requests per 10 seconds per token, so Retry-After is normally
# under 10 seconds; a wait over 60 means a longer block and stops the run.
_MAX_RETRY_WAIT = 60.0
# 150 seconds of the per-token limit, enough for a few hundred changed cards.
DEFAULT_MAX_REQUESTS = 1500
# The card fields the documents use; badges carries the comment count.
_CARD_FIELDS = "id,name,desc,idList,labels,idMembers,start,due,dueComplete,closed,shortUrl,badges"
# Comment actions need only the text, the date and the author's name.
_COMMENT_PARAMS = {
    "filter": "commentCard",
    "fields": "data,date",
    "member": "false",
    "memberCreator_fields": "fullName",
}


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the key, token or body.
# ---------------------------------------------------------------------------
class TrelloSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class TrelloAuthError(TrelloSourceError):
    """Trello rejected the API key or token (HTTP 401)."""


class TrelloRateLimitedError(TrelloSourceError):
    """Trello kept answering HTTP 429."""


class TrelloAPIError(TrelloSourceError):
    """Trello answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _retry_after(response: Any) -> float | None:
    try:
        return max(float(response.headers.get("Retry-After")), 0.0)
    except (TypeError, ValueError):
        return None


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

        attempt = 0
        while True:
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(API_URL + path, params=params, headers=self._headers)
            except httpx.TransportError as exc:
                if last:
                    raise TrelloAPIError(f"Trello request failed: {type(exc).__name__}") from None
                self._sleep(float(2**attempt))
                attempt += 1
                continue
            status = response.status_code
            if status == 429:
                wait = _retry_after(response)
                if last or (wait is not None and wait > _MAX_RETRY_WAIT):
                    raise TrelloRateLimitedError("Trello rate limit exceeded (HTTP 429)")
                self._sleep(wait if wait is not None else float(2**attempt))
            elif status >= 500 and not last:
                self._sleep(float(2**attempt))
            elif status == 401:
                raise TrelloAuthError("Trello rejected the API key or token (HTTP 401)")
            elif status != 200:
                raise TrelloAPIError(f"Trello request failed: HTTP {status}", status=status)
            else:
                return response.json()
            attempt += 1


# ---------------------------------------------------------------------------
# Rendering: deterministic, no volatile fields.
# ---------------------------------------------------------------------------
def _date(value: Any) -> str:
    return str(value)[:10] if value else ""


def _ordered(items: list[dict]) -> list[dict]:
    """Trello's position order, with the id as tiebreak so the result is stable."""
    return sorted(items, key=lambda item: (item.get("pos") or 0, str(item.get("id"))))


def _label_names(labels: list[dict]) -> list[str]:
    names = (str(label.get("name") or label.get("color") or "") for label in labels)
    return sorted(name for name in names if name)


def _card_list(card: dict, board: dict) -> dict:
    return next((i for i in board.get("lists") or [] if i["id"] == card.get("idList")), {})


def _archived(card: dict, board: dict) -> bool:
    # Archiving a list or a board does not archive its cards in the API.
    return bool(card.get("closed") or _card_list(card, board).get("closed") or board.get("closed"))


def render_board(board: dict) -> dict[str, Any]:
    members = sorted(name for m in board.get("members") or [] if (name := m.get("fullName")))
    labels = _label_names(board.get("labels") or [])
    parts = [f"Board: {board.get('name') or board['id']}"]
    if board.get("closed"):
        parts.append("Status: archived")
    description = str(board.get("desc") or "").strip()
    if description:
        parts.extend(["", description])
    lists = _ordered(board.get("lists") or [])
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
        NODE_SET_COLUMN: [f"trello:{board['id']}"],
        "_deleted": False,
    }


def render_card(
    card: dict, board: dict, checklists: list[dict], comments: list[dict]
) -> dict[str, Any]:
    members = {m["id"]: str(m.get("fullName") or "") for m in board.get("members") or []}
    member_names = sorted(members[m] for m in card.get("idMembers") or [] if members.get(m))
    due = _date(card.get("due"))
    if due and card.get("dueComplete"):
        due += " (done)"
    fields = [
        ("Board", str(board.get("name") or "")),
        ("List", str(_card_list(card, board).get("name") or "")),
        ("Labels", ", ".join(_label_names(card.get("labels") or []))),
        ("Members", ", ".join(member_names)),
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
        for checklist in _ordered(checklists):
            parts.append(f"{checklist.get('name') or 'Checklist'}:")
            for item in _ordered(checklist.get("checkItems") or []):
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
        NODE_SET_COLUMN: [f"trello:{board['id']}"],
        "_deleted": False,
    }


def _comment_count(card: dict) -> int:
    return int((card.get("badges") or {}).get("comments") or 0)


def _digest(value: Any) -> str:
    return hashlib.md5(json.dumps(value, sort_keys=True).encode()).hexdigest()


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
    max_requests: int = DEFAULT_MAX_REQUESTS


class _CutShortError(Exception):
    """Internal: stop the run here, keeping the boards finished so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Walker:
    """One run over the selected boards, writing progress into ``state``."""

    def __init__(
        self,
        client: Any,
        config: _TrelloConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.requests = 0
        self.emitted = 0
        # Set once a board or card is saved; the budget never stops a run before that.
        self.saved = False
        # Part of every card's base, so changed include flags re-render the cards.
        self.options = [config.include_archived, config.include_comments, config.include_checklists]

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.saved and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.get(path, params)
        except TrelloRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except TrelloAuthError:
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    # -- reading -----------------------------------------------------------
    def _board_ids(self) -> list[str]:
        if self.config.board_ids:
            return list(self.config.board_ids)
        params = {"filter": "all" if self.config.include_archived else "open", "fields": "id"}
        if self.config.workspace_id:
            boards = self._get(f"/organizations/{self.config.workspace_id}/boards", params)
        else:
            boards = self._get("/members/me/boards", params)
        # A 200 that is not a list must not read as "no boards".
        if not isinstance(boards, list):
            raise TrelloAPIError("Trello answered without a board list")
        return [str(board["id"]) for board in boards]

    def _newest_action(self, board_id: str, cursor: str | None) -> str | None:
        """The newest action id after ``cursor``, or None when nothing happened."""
        params: dict[str, Any] = {"filter": "all", "limit": 2, "fields": "id"}
        if cursor:
            params["since"] = cursor
        actions = self._get(f"/boards/{board_id}/actions", params)
        # Action ids are Mongo ObjectIds, so newer ones compare greater.
        newer = [str(a["id"]) for a in actions if not cursor or str(a["id"]) > cursor]
        return max(newer, default=None)

    def _board(self, board_id: str) -> dict:
        return self._get(
            f"/boards/{board_id}",
            {
                "fields": "name,desc,closed,url,dateLastActivity",
                "lists": "all",
                "labels": "all",
                # The nested label list is capped at 50 unless asked for more.
                "labels_limit": 1000,
                "members": "all",
            },
        )

    def _cards(self, board_id: str) -> list[dict]:
        # The nested form has no documented cap; /boards/{id}/cards takes a limit of 1000.
        params = {"fields": "id", "cards": "all", "card_fields": _CARD_FIELDS}
        return self._get(f"/boards/{board_id}", params).get("cards") or []

    def _checklists(self, board_id: str) -> dict[str, list[dict]]:
        by_card: dict[str, list[dict]] = {}
        if self.config.include_checklists:
            for checklist in self._get(f"/boards/{board_id}/checklists"):
                by_card.setdefault(str(checklist.get("idCard")), []).append(checklist)
        return by_card

    def _board_feed(
        self, board_id: str, cards: dict[str, dict], wanted: set[str]
    ) -> dict[str, list[dict]] | None:
        """Comments of the ``wanted`` cards from the board feed, or None when reading
        them card by card costs fewer requests."""
        counts = [_comment_count(cards[cid]) for cid in wanted]
        total = sum(_comment_count(card) for card in cards.values())
        # The card endpoint stops at 1000 comments, so a bigger card needs the feed.
        if max(counts, default=0) <= _CARD_ACTIONS_MAX and (
            sum(1 for count in counts if count) <= total // _ACTIONS_PAGE + 1
        ):
            return None
        by_card: dict[str, list[dict]] = {}
        for action in self._board_comments(board_id):
            card_id = str(((action.get("data") or {}).get("card") or {}).get("id"))
            if card_id in wanted:
                by_card.setdefault(card_id, []).append(action)
        return by_card

    def _card_comments(self, card_id: str) -> list[dict] | None:
        """A card's comments, or None when the card is gone since the cards were read."""
        comments: list[dict] = []
        for page in range(_CARD_ACTIONS_PAGES):
            try:
                batch = self._get(f"/cards/{card_id}/actions", {**_COMMENT_PARAMS, "page": page})
            except TrelloAPIError as exc:
                if exc.status != 404:
                    raise
                return None
            comments.extend(batch)
            if len(batch) < _CARD_ACTIONS_PAGE:
                return comments
        raise TrelloAPIError(f"Trello card {card_id} has more comments than its endpoint returns")

    def _board_comments(self, board_id: str) -> Iterator[dict]:
        before = None
        while True:
            params: dict[str, Any] = {**_COMMENT_PARAMS, "limit": _ACTIONS_PAGE}
            if before:
                params["before"] = before
            page = self._get(f"/boards/{board_id}/actions", params)
            yield from page
            if len(page) < _ACTIONS_PAGE:
                return
            oldest = min(str(a["id"]) for a in page)
            if oldest == before:
                raise TrelloAPIError("Trello action pagination did not advance")
            before = oldest

    # -- one board ---------------------------------------------------------
    def _sync_board(self, board_id: str, entry: dict, rows: list[dict]) -> set[str]:
        """Append one board's changed rows to ``rows`` and return the ids of its cards.

        ``entry`` is updated as cards finish; its cursor moves only once the whole
        board is done, so a board left half way is read again on the next run.
        """
        # Read the cursor first, so nothing that happens during the read is skipped.
        newest = self._newest_action(board_id, entry["cursor"])
        board = self._board(board_id)
        if board.get("closed") and not self.config.include_archived:
            if entry["board"]:
                rows.append({"id": f"board:{board_id}", "_deleted": True})
            entry.update(board=None, cursor=newest or entry["cursor"])
            return set()

        board_row = render_board(board)
        board_changed = _digest(board_row) != entry["board"]
        if board_changed:
            entry["board"] = _digest(board_row)
            rows.append(board_row)
        changed = (
            self.config.full_resync
            or newest is not None
            or board_changed
            or board.get("dateLastActivity") != entry["activity"]
            or entry.get("options") != self.options
        )
        if not changed:
            self.stats["unchanged"] += len(entry["cards"])
            cards = set(entry["cards"])
        else:
            found = {
                str(card["id"]): card
                for card in self._cards(board_id)
                if self.config.include_archived or not _archived(card, board)
            }
            cards = set(found)
            if not self._card_rows(board_id, board, found, entry["cards"], rows):
                return cards
        entry.update(
            cursor=newest or entry["cursor"],
            activity=board.get("dateLastActivity"),
            options=self.options,
        )
        return cards

    def _card_rows(
        self, board_id: str, board: dict, cards: dict, known: dict, rows: list[dict]
    ) -> bool:
        """Append the changed cards' rows; False when a card had to be skipped."""
        checklists = self._checklists(board_id)
        # A card's base is its document without comments plus its comment count:
        # it moves on checklist and label edits, which the feed leaves out, and on
        # added or deleted comments, without reading any comment.
        bases = {
            cid: _digest(
                [
                    render_card(card, board, checklists.get(cid, []), []),
                    (card.get("badges") or {}).get("comments")
                    if self.config.include_comments
                    else None,
                    self.options,
                ]
            )
            for cid, card in cards.items()
        }
        changed = {
            cid
            for cid in cards
            if self.config.full_resync or (known.get(cid) or {}).get("base") != bases[cid]
        }
        feed = self._board_feed(board_id, cards, changed) if self.config.include_comments else {}
        complete = True
        for cid in sorted(cards):
            if cid not in changed:
                self.stats["unchanged"] += 1
                continue
            if feed is not None:
                comments = feed.get(cid, [])
            else:
                comments = self._card_comments(cid) if _comment_count(cards[cid]) else []
            if comments is None:
                logger.warning("Trello card %s answered 404; it is read again next run.", cid)
                complete = False
                continue
            row = render_card(cards[cid], board, checklists.get(cid, []), comments)
            previous = known.get(cid)
            known[cid] = {"base": bases[cid], "row": _digest(row)}
            self.saved = True
            if previous and previous["row"] == known[cid]["row"]:
                self.stats["unchanged"] += 1
                continue
            rows.append(row)
        return complete

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        # A bad key or token must fail loudly, never look like every board was deleted.
        self._get("/members/me", {"fields": "id"})
        boards: dict[str, dict] = self.state.setdefault("boards", {})
        configured = bool(self.config.board_ids)
        # Card deletions are decided once a pass has visited every board, which can
        # take several runs when the budget cuts them short.
        visit = self.state.setdefault("pass", {"present": {}, "orphans": []})
        ids = self._board_ids()
        if not configured and not ids and boards and not self.state.get("empty_listing"):
            # A board listing that is empty as a whole is confirmed by the next run.
            self.state["empty_listing"] = True
            logger.warning(
                "Trello listed no boards although %d were synced before; nothing is "
                "forgotten until the next run confirms it.",
                len(boards),
            )
            return
        self.state.pop("empty_listing", None)
        for board_id in [b for b in boards if b not in ids]:
            yield from self._drop_board(board_id, boards, visit)
        # Start after the board a cut-short run reached, so every one gets its turn.
        resume = self.state.get("resume")
        order = sorted(ids)
        if resume:
            order = [b for b in order if b > resume] + [b for b in order if b <= resume]
        for board_id in order:
            # Work on a copy, so a board that fails keeps its previous state.
            entry = copy.deepcopy(boards.get(board_id)) or {
                "cursor": None,
                "activity": None,
                "board": None,
                "cards": {},
            }
            rows: list[dict] = []
            try:
                cards = self._sync_board(board_id, entry, rows)
            except _CutShortError:
                # Keep the cards finished so far; the cursor has not moved.
                boards[board_id] = entry
                yield from self._emit(rows)
                raise
            except TrelloAPIError as exc:
                # Card requests handle their own 404, so this one is the board's.
                if exc.status != 404:
                    raise
                if configured:
                    raise TrelloAPIError(
                        f"Trello board {board_id} was not found or is not visible to this token",
                        status=404,
                    ) from None
                logger.warning("Trello board %s answered 404.", board_id)
                self.stats["gone"] += 1
                yield from self._drop_board(board_id, boards, visit)
                ids.remove(board_id)
                continue
            boards[board_id] = entry
            visit["present"][board_id] = sorted(cards)
            self.state["resume"] = board_id
            self.saved = True
            yield from self._emit(rows)
            # Checked after every board, since a budget stop can end the run at the next.
            if set(ids) <= set(visit["present"]):
                yield from self._forget(boards, visit, ids)
                visit = self.state["pass"] = {"present": {}, "orphans": []}
        if not ids and visit["orphans"]:
            yield from self._forget(boards, visit, ids)
            self.state["pass"] = {"present": {}, "orphans": []}

    def _emit(self, rows: list[dict]) -> Iterator[dict]:
        for row in rows:
            self.emitted += 1
            # An archived board's tombstone comes through here too.
            self.stats["deleted" if row.get("_deleted") else "emitted"] += 1
            yield row

    def _drop_board(self, board_id: str, boards: dict[str, dict], visit: dict) -> Iterator[dict]:
        """A deleted, hidden or deselected board: its document goes now, its cards wait
        for the end of the pass, in case one moved to another selected board."""
        logger.warning("Forgetting Trello board %s and its cards.", board_id)
        entry = boards.pop(board_id)
        visit["present"].pop(board_id, None)
        visit["orphans"] = sorted(set(visit["orphans"]) | set(entry["cards"]))
        if entry["board"]:
            self.stats["deleted"] += 1
            yield {"id": f"board:{board_id}", "_deleted": True}

    def _forget(self, boards: dict[str, dict], visit: dict, ids: list[str]) -> Iterator[dict]:
        """Card tombstones, decided once a pass has seen every board, so a card moved
        between two selected boards is never deleted."""
        present = {board_id: set(visit["present"][board_id]) for board_id in ids}
        live = set().union(*present.values())
        for board_id in ids:
            entry = boards[board_id]
            for cid in [cid for cid in entry["cards"] if cid not in present[board_id]]:
                del entry["cards"][cid]
                if cid not in live:
                    self.stats["deleted"] += 1
                    yield {"id": f"card:{cid}", "_deleted": True}
        for cid in visit["orphans"]:
            if cid not in live:
                self.stats["deleted"] += 1
                yield {"id": f"card:{cid}", "_deleted": True}


def _iter_rows(
    client: Any,
    config: _TrelloConfig,
    state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict."""
    walker = _Walker(client, config, state, stats, check_active)
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        # A spent budget is a planned pause, not a failure; failed_budget says why.
        if cut.reason != "budget":
            stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Trello sync stopped early (%s); it resumes on the next run.", cut.reason)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _default_resource_name(board_ids: list[str] | None, workspace_id: str | None) -> str:
    if board_ids and len(board_ids) > 1:
        raise ValueError(
            "Pass a resource_name when syncing several boards: it scopes this sync's "
            "state and cleanup, and two sources sharing one would delete each other's cards."
        )
    scope = board_ids[0] if board_ids else workspace_id or "my_boards"
    slug = re.sub(r"[^a-z0-9]+", "_", scope.lower()).strip("_")
    if slug != scope:
        # "Acme Co" and "acme-co" share a slug, so the raw value keeps them apart.
        slug += "_" + hashlib.sha256(scope.encode()).hexdigest()[:8]
    return "trello_" + slug


def trello_source(
    board_ids: list[str] | None = None,
    *,
    workspace_id: str | None = None,
    api_key: str | None = None,
    token: str | None = None,
    include_archived: bool = False,
    include_comments: bool = True,
    include_checklists: bool = True,
    full_resync: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per Trello card and board.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        board_ids: Boards to sync. A listed board that cannot be read raises. Defaults
            to every board of ``workspace_id``, or every board the token's user belongs to.
        workspace_id: Sync every board of this workspace (id or name).
        api_key: Trello API key.
        token: Trello user token.
        include_archived: Also ingest archived cards, lists and boards, marked as archived.
        include_comments: Add each card's comments to its document.
        include_checklists: Add each card's checklists to its document.
        full_resync: Re-render every card, reading all comments again.
        max_requests: Request budget of one run, checked before each request once a
            card or board is saved. A bigger account finishes over several runs, which
            visit the boards in turn; a spent budget sets ``failed_budget`` but not
            ``failed``.
        resource_name: Stable name of this sync, which scopes its state and cleanup.
            Defaults to one derived from the board or workspace; required when
            ``board_ids`` lists several boards.
        check_active: Optional host authorization checkpoint, called before each request.
        service: Pre-built client (see :class:`TrelloClient`). Mainly an injection
            point for tests and hosts. ``client`` is accepted as an alias.

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

    name = resource_name or _default_resource_name(board_ids, workspace_id)
    service = service or client
    if service is None:
        if not (api_key and token):
            raise ValueError(
                "Trello credentials missing: pass api_key and token, or a pre-built service."
            )
        service = TrelloClient(api_key=api_key, token=token)

    config = _TrelloConfig(
        board_ids=tuple(str(b) for b in board_ids or ()),
        workspace_id=workspace_id,
        include_archived=include_archived,
        include_comments=include_comments,
        include_checklists=include_checklists,
        full_resync=full_resync,
        max_requests=max_requests,
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def trello_cards():
        stats.clear()
        stats.update(emitted=0, unchanged=0, deleted=0, gone=0, failed=0)
        rows = _iter_rows(service, config, dlt.current.resource_state(), stats, check_active)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = trello_cards()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "trello")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
