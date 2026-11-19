import os
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("missive_connector")

MISSIVE_TABLE_NAME = "missive_conversations"
MISSIVE_SOURCE_NAME = "missive"
MISSIVE_API_BASE = "https://api.missiveapp.com/v1"
MAX_RETRIES = 5
DEFAULT_LIMIT = 50


class MissiveAPIClient:
    def __init__(
        self,
        api_token: str,
        base_url: str = MISSIVE_API_BASE,
        client: httpx.Client | None = None,
    ):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "User-Agent": "cognee-missive-connector/0.1.0",
            },
            timeout=30.0,
        )

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.request(method, endpoint, **kwargs)
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", 2**attempt))
                    time.sleep(retry_after)
                    continue
                if response.status_code >= 500:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.RequestError:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError(
            f"Failed to execute request to {endpoint} after {MAX_RETRIES} attempts."
        )

    def get_conversations(
        self,
        limit: int = DEFAULT_LIMIT,
        until: int | None = None,
        mailbox: str | None = None,
        team: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if until is not None:
            params["until"] = until
        if mailbox:
            params["mailbox"] = mailbox
        if team:
            params["team"] = team
        if label:
            params["label"] = label
        return self._request("GET", "/conversations", params=params)

    def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/conversations/{conversation_id}/messages")
        return data.get("messages", [])

    def get_comments(self, conversation_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/conversations/{conversation_id}/comments")
        return data.get("comments", [])


def clean_html(html_content: str) -> str:
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for el in soup(["script", "style", "noscript", "svg"]):
        el.decompose()

    for br in soup.find_all("br"):
        br.replace_with("\n")
    for p in soup.find_all(["p", "div", "blockquote"]):
        p.insert_before("\n")
        p.insert_after("\n")
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        h.insert_before("\n# ")
        h.insert_after("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
        li.insert_after("\n")
    for container in soup.find_all(["ul", "ol"]):
        container.insert_after("\n")
    for a in soup.find_all("a", href=True):
        text = a.get_text().strip()
        href = a["href"]
        if text and href:
            a.replace_with(f"[{text}]({href})")

    raw_text = soup.get_text()
    lines: list[str] = []
    for line in raw_text.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        if trimmed.startswith(">") or re.match(
            r"^On .+ wrote:$", trimmed, re.IGNORECASE
        ):
            continue
        if trimmed.startswith("---") or trimmed.startswith("___"):
            continue
        lines.append(trimmed)
    return "\n".join(lines)


def format_timestamp(ts: int | float | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(ts, tz=UTC)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)


def render_conversation_content(
    conversation: dict[str, Any],
    messages: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    include_comments: bool = True,
    include_contact_details: bool = False,
) -> str:
    subject = conversation.get("subject") or "Untitled Conversation"
    entries: list[dict[str, Any]] = []

    for msg in messages:
        ts = msg.get("delivered_at") or msg.get("created_at") or 0
        from_field = msg.get("from_field") or {}
        sender_name = from_field.get("name") or "Unknown"
        if include_contact_details and from_field.get("address"):
            sender = f"{sender_name} <{from_field.get('address')}>"
        else:
            sender = sender_name

        body = clean_html(msg.get("body") or msg.get("preview") or "")
        msg_type = msg.get("type", "message").capitalize()
        entries.append(
            {
                "timestamp": ts,
                "type": "message",
                "header": f"[{format_timestamp(ts)}] {msg_type} from {sender}:",
                "content": body,
            }
        )

    if include_comments:
        for com in comments:
            ts = com.get("created_at") or 0
            author_field = com.get("author") or {}
            author_name = author_field.get("name") or "Team Member"
            if include_contact_details and author_field.get("email"):
                author = f"{author_name} <{author_field.get('email')}>"
            else:
                author = author_name

            body = clean_html(com.get("body") or "")
            entries.append(
                {
                    "timestamp": ts,
                    "type": "comment",
                    "header": f"[{format_timestamp(ts)}] Internal Comment from {author}:",
                    "content": body,
                }
            )

    entries.sort(key=lambda x: x["timestamp"])

    doc_parts: list[str] = [f"## Subject: {subject}\n"]
    for entry in entries:
        if not entry["content"].strip():
            continue
        doc_parts.append(f"### {entry['header']}\n{entry['content']}\n")

    return "\n".join(doc_parts).strip()


def _conversation_to_row(
    client: MissiveAPIClient,
    conversation: dict[str, Any],
    include_comments: bool = True,
    include_contact_details: bool = False,
) -> dict[str, Any]:
    conv_id = conversation.get("id", "")
    subject = conversation.get("subject") or "Untitled Conversation"
    messages = client.get_messages(conv_id)
    comments = client.get_comments(conv_id) if include_comments else []

    rendered_text = render_conversation_content(
        conversation=conversation,
        messages=messages,
        comments=comments,
        include_comments=include_comments,
        include_contact_details=include_contact_details,
    )

    return {
        "id": conv_id,
        "title": subject,
        "content": rendered_text,
        "url": f"https://mail.missiveapp.com/#/conversations/{conv_id}",
    }


def _iter_conversations(
    client: MissiveAPIClient,
    since: int | None = None,
    mailbox: str | None = None,
    team: str | None = None,
    label: str | None = None,
):
    until_cursor: int | None = None
    while True:
        data = client.get_conversations(
            limit=DEFAULT_LIMIT,
            until=until_cursor,
            mailbox=mailbox,
            team=team,
            label=label,
        )
        conversations = data.get("conversations", [])
        if not conversations:
            return

        for conv in conversations:
            if conv.get("trashed_at") or conv.get("junked_at"):
                continue
            last_activity = conv.get("last_activity_at")
            if since and last_activity and last_activity < since:
                return
            yield conv

        meta = data.get("meta", {})
        next_page = meta.get("next", {})
        until_cursor = next_page.get("until")
        if not until_cursor:
            return


def missive_source(
    api_token: str | None = None,
    since: int | None = None,
    mailbox: str | None = None,
    team: str | None = None,
    label: str | None = None,
    include_comments: bool = True,
    include_contact_details: bool = False,
    client: MissiveAPIClient | None = None,
):
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Missive connector requires dlt: pip install "dlt[sqlalchemy]"'
        ) from exc

    resolved_token = api_token or os.environ.get("MISSIVE_API_TOKEN")
    if client is None:
        if not resolved_token:
            raise ValueError(
                "Missive API token required: pass api_token= or set MISSIVE_API_TOKEN."
            )
        api_client = MissiveAPIClient(api_token=resolved_token)
    else:
        api_client = client

    @dlt.resource(
        name=MISSIVE_TABLE_NAME, primary_key="id", write_disposition="replace"
    )
    def missive_conversations():
        count = 0
        for conv in _iter_conversations(
            client=api_client,
            since=since,
            mailbox=mailbox,
            team=team,
            label=label,
        ):
            row = _conversation_to_row(
                client=api_client,
                conversation=conv,
                include_comments=include_comments,
                include_contact_details=include_contact_details,
            )
            if row["content"]:
                count += 1
                yield row
        logger.info("Missive: synced %d conversation(s).", count)

    @dlt.source(name=MISSIVE_SOURCE_NAME)
    def _missive():
        return missive_conversations

    source = _missive()
    setattr(source, DOCUMENT_SOURCE_ATTR, MISSIVE_SOURCE_NAME)
    return source
