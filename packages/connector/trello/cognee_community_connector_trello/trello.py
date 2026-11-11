"""Trello Data-Source Connector for Cognee.

Ingests Trello boards, lists, cards, checklists, and comment threads.
Features incremental cursor sync on dateLastActivity and forget-on-delete tombstones.
Resolves issue: cognee#6471.
"""

import os
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Any, Optional, Iterator, Union, Set
from datetime import datetime, timezone

# Optional dlt import for Cognee ecosystem compatibility
try:
    import dlt
    _has_dlt = True
except ImportError:
    dlt = None
    _has_dlt = False

def _optional_source(func):
    if _has_dlt and hasattr(dlt, "source"):
        return dlt.source(func)
    return func

def _optional_resource(*args, **kwargs):
    def decorator(func):
        if _has_dlt and hasattr(dlt, "resource"):
            return dlt.resource(*args, **kwargs)(func)
        return func
    return decorator


class TrelloClient:
    """Client for the Trello REST API v1."""

    BASE_URL = "https://api.trello.com/1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        user_agent: Optional[str] = None
    ):
        self.api_key = api_key or os.getenv("TRELLO_API_KEY")
        self.token = token or os.getenv("TRELLO_TOKEN")
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.user_agent = user_agent or "TrelloCogneeConnector/1.0 (https://github.com/topoteretes/cognee-community)"

    def _http_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = params or {}
        if self.api_key:
            params["key"] = self.api_key
        if self.token:
            params["token"] = self.token

        query_str = urllib.parse.urlencode(params)
        full_url = f"{self.base_url}/{endpoint.lstrip('/')}?{query_str}" if query_str else f"{self.base_url}/{endpoint.lstrip('/')}"

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json"
        }
        req = urllib.request.Request(full_url, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise PermissionError("Trello authentication failed: invalid api_key or token.")
            if e.code == 404:
                raise ValueError(f"Trello resource not found: {endpoint}")
            raise

    def get_member_boards(self) -> List[Dict[str, Any]]:
        """Returns all open boards the authenticated user or token has access to."""
        return self._http_get("members/me/boards", {"filter": "open", "fields": "name,desc,url,closed"})

    def get_board_lists(self, board_id: str) -> List[Dict[str, Any]]:
        """Returns all lists within a board."""
        return self._http_get(f"boards/{board_id}/lists", {"fields": "name,closed,pos"})

    def get_board_cards(
        self,
        board_id: str,
        since: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetches all open cards on a board with checklists and comment actions.
        Supports cursor filtering via 'since' parameter (ISO8601 or dateLastActivity).
        """
        params: Dict[str, Any] = {
            "fields": "id,name,desc,idList,due,dueComplete,dateLastActivity,labels,url,closed",
            "checklists": "all",
            "actions": "commentCard",
            "limit": 1000
        }
        if since:
            params["since"] = since

        return self._http_get(f"boards/{board_id}/cards", params)

    def fetch_board_data(
        self,
        board_id: str,
        since: Optional[str] = None,
        previous_card_ids: Optional[Set[str]] = None
    ) -> Dict[str, Any]:
        """
        Ingests a Trello board, resolves list names, aggregates card documents,
        and generates deletion tombstones for cards deleted since last sync.
        """
        # Fetch list mapping
        lists = self.get_board_lists(board_id)
        list_map = {lst["id"]: lst.get("name", "Unknown List") for lst in lists}

        # Fetch cards
        cards = self.get_board_cards(board_id, since=since)

        # Track active card IDs for forget-on-delete
        current_card_ids = set()
        card_documents = []
        max_activity = since or "1970-01-01T00:00:00.000Z"

        for c in cards:
            cid = c.get("id")
            current_card_ids.add(cid)

            card_name = c.get("name", "Untitled Card")
            desc = c.get("desc", "").strip()
            list_name = list_map.get(c.get("idList"), "Default List")
            due = c.get("due")
            due_complete = c.get("dueComplete", False)
            last_activity = c.get("dateLastActivity", "")
            card_url = c.get("url", f"https://trello.com/c/{cid}")

            if last_activity and last_activity > max_activity:
                max_activity = last_activity

            # Extract labels
            labels = [lbl.get("name") or lbl.get("color", "") for lbl in c.get("labels", []) if lbl]

            # Extract checklists
            checklists_summary = []
            for cl in c.get("checklists", []):
                cl_name = cl.get("name", "Checklist")
                items = []
                for item in cl.get("checkItems", []):
                    status = "✅" if item.get("state") == "complete" else "⬜"
                    items.append(f"  - {status} {item.get('name')}")
                checklists_summary.append(f"- **{cl_name}**:\n" + "\n".join(items))

            # Extract comment threads
            comments_summary = []
            for act in c.get("actions", []):
                if act.get("type") == "commentCard":
                    text = act.get("data", {}).get("text", "")
                    author = act.get("memberCreator", {}).get("fullName") or act.get("memberCreator", {}).get("username", "Unknown")
                    date = act.get("date", "")
                    comments_summary.append(f"- **{author}** ({date}): {text}")

            # Construct clean markdown text for Cognee graph completion & vector search
            text_doc = (
                f"# Card: {card_name}\n"
                f"- **Board ID**: {board_id}\n"
                f"- **List / Stage**: {list_name}\n"
                f"- **Due Date**: {due if due else 'None'}"
                f"{' (Completed)' if due_complete else (' (Pending)' if due else '')}\n"
                f"- **Labels**: {', '.join(labels) if labels else 'None'}\n"
                f"- **Last Activity**: {last_activity}\n"
                f"- **URL**: {card_url}\n\n"
            )
            if desc:
                text_doc += f"## Description\n{desc}\n\n"
            if checklists_summary:
                text_doc += "## Checklists\n" + "\n".join(checklists_summary) + "\n\n"
            if comments_summary:
                text_doc += "## Discussion & Comments\n" + "\n".join(comments_summary) + "\n"

            card_documents.append({
                "id": f"trello_card_{cid}",
                "card_id": cid,
                "board_id": board_id,
                "list_id": c.get("idList"),
                "list_name": list_name,
                "name": card_name,
                "due": due,
                "due_complete": due_complete,
                "labels": labels,
                "date_last_activity": last_activity,
                "url": card_url,
                "text": text_doc,
                "_deleted": False
            })

        # Tombstones for forget-on-delete
        tombstones = []
        if previous_card_ids:
            deleted_ids = previous_card_ids - current_card_ids
            for del_id in deleted_ids:
                tombstones.append({
                    "id": f"trello_card_{del_id}",
                    "card_id": del_id,
                    "board_id": board_id,
                    "_deleted": True
                })

        return {
            "board_id": board_id,
            "cursor": max_activity,
            "current_card_ids": list(current_card_ids),
            "cards": card_documents,
            "tombstones": tombstones
        }


def trello_data(
    board_ids: Optional[Union[str, List[str]]] = None,
    client: Optional[TrelloClient] = None,
    since: Optional[str] = None,
    previous_card_ids: Optional[Set[str]] = None
) -> Iterator[Dict[str, Any]]:
    """
    Yields Cognee-compatible documents for Trello cards and deletion tombstones.
    If board_ids is omitted, attempts to auto-discover boards from the authenticated member account.
    """
    cli = client or TrelloClient()

    if not board_ids:
        raw_env = os.getenv("TRELLO_BOARD_IDS", "")
        if raw_env:
            board_ids = [b.strip() for b in raw_env.split(",") if b.strip()]
        else:
            # Auto-discover member boards
            try:
                boards = cli.get_member_boards()
                board_ids = [b["id"] for b in boards]
            except Exception as e:
                raise ValueError(
                    f"Must provide 'board_ids' parameter, set 'TRELLO_BOARD_IDS', or authenticate with valid credentials: {e}"
                )

    if isinstance(board_ids, str):
        board_ids = [b.strip() for b in board_ids.split(",") if b.strip()]

    for bid in board_ids:
        board_pack = cli.fetch_board_data(bid, since=since, previous_card_ids=previous_card_ids)
        # Yield active card documents
        for c in board_pack["cards"]:
            yield {
                "id": c["id"],
                "type": "trello_card",
                "title": f"Trello Card: {c['name']} ({c['list_name']})",
                "text": c["text"],
                "metadata": c,
                "_deleted": False
            }
        # Yield deletion tombstones
        for t in board_pack["tombstones"]:
            yield {
                "id": t["id"],
                "type": "trello_card",
                "_deleted": True,
                "metadata": t
            }


@_optional_source
def trello_source(
    board_ids: Optional[Union[str, List[str]]] = None,
    since: Optional[str] = None,
    previous_card_ids: Optional[Set[str]] = None
):
    """Cognee / DLT source wrapper for Trello data."""
    @_optional_resource(name="trello_data", write_disposition="append")
    def _data():
        yield from trello_data(board_ids=board_ids, since=since, previous_card_ids=previous_card_ids)

    return _data()
