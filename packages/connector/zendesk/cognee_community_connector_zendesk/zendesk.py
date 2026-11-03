"""Zendesk Support Tickets Data-Source Connector for Cognee.

Ingests support tickets, comment conversation threads, tags, and requester context
from Zendesk Support REST API v2 into the Cognee graph and vector memory.
Resolves issue: cognee#6465.
"""

import os
import time
import base64
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Any, Optional, Iterator, Union
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


class ZendeskClient:
    """Client for the Zendesk Support REST API v2."""

    def __init__(
        self,
        subdomain: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        oauth_token: Optional[str] = None,
        base_url: Optional[str] = None,
        user_agent: Optional[str] = None
    ):
        self.subdomain = subdomain or os.getenv("ZENDESK_SUBDOMAIN")
        self.email = email or os.getenv("ZENDESK_EMAIL")
        self.api_token = api_token or os.getenv("ZENDESK_API_TOKEN")
        self.oauth_token = oauth_token or os.getenv("ZENDESK_OAUTH_TOKEN")
        self.user_agent = user_agent or "ZendeskCogneeConnector/1.0 (https://github.com/topoteretes/cognee-community)"

        if base_url:
            self.base_url = base_url.rstrip("/")
        elif self.subdomain:
            self.base_url = f"https://{self.subdomain.strip()}.zendesk.com/api/v2"
        else:
            self.base_url = "https://d3v-support.zendesk.com/api/v2"

    def _build_auth_header(self) -> Dict[str, str]:
        if self.oauth_token:
            return {"Authorization": f"Bearer {self.oauth_token}"}
        elif self.email and self.api_token:
            auth_str = f"{self.email}/token:{self.api_token}"
            encoded = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
            return {"Authorization": f"Basic {encoded}"}
        return {}

    def _http_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> Any:
        params = params or {}
        query_str = urllib.parse.urlencode(params)
        if endpoint.startswith("http"):
            full_url = f"{endpoint}?{query_str}" if query_str else endpoint
        else:
            full_url = f"{self.base_url}/{endpoint.lstrip('/')}?{query_str}" if query_str else f"{self.base_url}/{endpoint.lstrip('/')}"

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            **self._build_auth_header()
        }
        req = urllib.request.Request(full_url, headers=headers)

        last_exc = None
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                last_exc = e
                if e.code == 401:
                    raise PermissionError(f"Zendesk authentication failed: invalid credentials for {self.base_url}")
                if e.code == 404:
                    raise ValueError(f"Zendesk resource not found: {endpoint}")
                if e.code == 429:
                    retry_after = int(e.headers.get("Retry-After", 2))
                    time.sleep(retry_after)
                    continue
                if e.code in (500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                last_exc = e
                time.sleep(1.0 * (attempt + 1))
                continue

        raise last_exc or RuntimeError(f"Failed to fetch {full_url}")

    def get_users_map(self) -> Dict[int, Dict[str, Any]]:
        """Fetches and maps users by their ID to resolve requester/assignee names."""
        try:
            res = self._http_get("users.json", {"role[]": ["agent", "admin", "end-user"], "per_page": 100})
            users = res.get("users", [])
            return {u["id"]: u for u in users if "id" in u}
        except Exception:
            return {}

    def get_tickets(
        self,
        start_time_epoch: Optional[int] = None,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetches tickets using standard or incremental export endpoint.
        If start_time_epoch is specified, uses incremental export for real-time cursor sync.
        """
        all_tickets: List[Dict[str, Any]] = []

        if start_time_epoch is not None:
            endpoint = "incremental/tickets.json"
            params = {"start_time": start_time_epoch}
            res = self._http_get(endpoint, params)
            tickets = res.get("tickets", [])
            all_tickets.extend(tickets)
        else:
            endpoint = "tickets.json"
            params = {"per_page": 100, "sort_by": "updated_at", "sort_order": "desc"}
            res = self._http_get(endpoint, params)
            tickets = res.get("tickets", [])
            all_tickets.extend(tickets)

        if limit and len(all_tickets) > limit:
            all_tickets = all_tickets[:limit]

        return all_tickets

    def get_ticket_comments(self, ticket_id: int) -> List[Dict[str, Any]]:
        """Fetches all conversation comments and audit entries for a ticket."""
        try:
            res = self._http_get(f"tickets/{ticket_id}/comments.json")
            return res.get("comments", [])
        except Exception:
            return []

    def fetch_tickets_data(
        self,
        start_time_epoch: Optional[int] = None,
        limit: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Ingests tickets, resolves user metadata, extracts comment threads,
        and constructs rich Markdown documents ready for Cognee semantic search.
        """
        users_map = self.get_users_map()
        raw_tickets = self.get_tickets(start_time_epoch=start_time_epoch, limit=limit)

        ticket_documents = []
        max_updated_epoch = start_time_epoch or 0

        for t in raw_tickets:
            tid = t.get("id")
            subject = t.get("subject", "Untitled Ticket")
            desc = t.get("description", "").strip()
            status = t.get("status", "unknown").upper()
            priority = (t.get("priority") or "normal").upper()
            created_at = t.get("created_at", "")
            updated_at = t.get("updated_at", "")
            tags = t.get("tags", [])
            ticket_type = t.get("type", "incident")

            # Calculate cursor epoch
            if updated_at:
                try:
                    dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    epoch = int(dt.timestamp())
                    if epoch > max_updated_epoch:
                        max_updated_epoch = epoch
                except Exception:
                    pass

            # Resolve Requester and Assignee
            req_id = t.get("requester_id")
            requester_obj = users_map.get(req_id, {})
            requester_name = requester_obj.get("name") or str(req_id or "Unknown Requester")
            requester_email = requester_obj.get("email", "")

            assignee_id = t.get("assignee_id")
            assignee_obj = users_map.get(assignee_id, {})
            assignee_name = assignee_obj.get("name") or (str(assignee_id) if assignee_id else "Unassigned")

            # Fetch conversation comments
            comments = self.get_ticket_comments(tid) if tid else []
            formatted_comments = []
            for c in comments:
                author_id = c.get("author_id")
                author_obj = users_map.get(author_id, {})
                author_name = author_obj.get("name") or str(author_id or "User")
                is_public = c.get("public", True)
                body = c.get("body", "").strip()
                c_date = c.get("created_at", "")
                note_type = "Public Reply" if is_public else "Internal Note"
                formatted_comments.append(f"- **{author_name}** [{note_type} - {c_date}]:\n  {body}")

            # Synthesize clean Markdown document
            text_doc = (
                f"# Zendesk Ticket #{tid}: {subject}\n"
                f"- **Status**: {status}\n"
                f"- **Priority**: {priority}\n"
                f"- **Type**: {ticket_type}\n"
                f"- **Requester**: {requester_name} ({requester_email})\n"
                f"- **Assignee**: {assignee_name}\n"
                f"- **Tags**: {', '.join(tags) if tags else 'None'}\n"
                f"- **Created At**: {created_at}\n"
                f"- **Last Updated**: {updated_at}\n\n"
            )
            if desc:
                text_doc += f"## Problem Description\n{desc}\n\n"
            if formatted_comments:
                text_doc += "## Conversation History\n" + "\n\n".join(formatted_comments) + "\n"

            ticket_documents.append({
                "id": f"zendesk_ticket_{tid}",
                "ticket_id": tid,
                "subject": subject,
                "status": status,
                "priority": priority,
                "requester": requester_name,
                "requester_email": requester_email,
                "assignee": assignee_name,
                "tags": tags,
                "created_at": created_at,
                "updated_at": updated_at,
                "comments_count": len(comments),
                "text": text_doc
            })

        return {
            "cursor_epoch": max_updated_epoch,
            "tickets_count": len(ticket_documents),
            "tickets": ticket_documents
        }


def zendesk_data(
    subdomain: Optional[str] = None,
    client: Optional[ZendeskClient] = None,
    start_time_epoch: Optional[int] = None,
    limit: Optional[int] = None
) -> Iterator[Dict[str, Any]]:
    """Yields Cognee-compatible documents for Zendesk tickets and conversations."""
    cli = client or ZendeskClient(subdomain=subdomain)
    pack = cli.fetch_tickets_data(start_time_epoch=start_time_epoch, limit=limit)

    for t in pack["tickets"]:
        yield {
            "id": t["id"],
            "type": "zendesk_ticket",
            "title": f"Zendesk #{t['ticket_id']}: {t['subject']}",
            "text": t["text"],
            "metadata": t
        }


@_optional_source
def zendesk_source(
    subdomain: Optional[str] = None,
    start_time_epoch: Optional[int] = None,
    limit: Optional[int] = None
):
    """Cognee / DLT source wrapper for Zendesk tickets."""
    @_optional_resource(name="zendesk_data", write_disposition="append")
    def _data():
        yield from zendesk_data(subdomain=subdomain, start_time_epoch=start_time_epoch, limit=limit)

    return _data()
