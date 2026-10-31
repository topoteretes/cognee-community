"""DLT source for Zoho Cliq team chat channels, messages, and threads.

Fetches messages from Zoho Cliq channels, folds thread replies into parent discussions,
renders them into structured Markdown documents, and yields them as a dlt resource
tagged with DOCUMENT_SOURCE_ATTR for cognee's cognitive entity extraction.
"""

from datetime import datetime, timezone
import os
import time
from typing import Any, Generator
import requests

try:
    from cognee.shared.logging_utils import get_logger
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
    logger = get_logger("zoho_cliq_connector")
except ImportError:
    import logging
    logger = logging.getLogger("zoho_cliq_connector")
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

ZOHO_CLIQ_TABLE_NAME = "zoho_cliq_messages"
ZOHO_CLIQ_SOURCE_NAME = "zoho_cliq"
DEFAULT_DOMAIN = "cliq.zoho.com"
MAX_RETRIES = 5


def _is_transient(status_code: int) -> bool:
    """True for transient network / server errors."""
    return status_code in (429, 500, 502, 503, 504)


class ZohoCliqClient:
    """Client for the Zoho Cliq REST API."""

    def __init__(
        self,
        auth_token: str | None = None,
        domain: str = DEFAULT_DOMAIN,
        session: requests.Session | None = None,
    ):
        self.auth_token = auth_token
        self.domain = domain.rstrip("/")
        if not self.domain.startswith("http"):
            self.base_url = f"https://{self.domain}/api/v2"
        else:
            self.base_url = f"{self.domain}/api/v2"
        self.session = session or requests.Session()

    def request(self, method: str, endpoint: str, **kwargs) -> Any:
        """Call Zoho Cliq API with token authorization and retry backoff."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = dict(kwargs.pop("headers", {}))
        if self.auth_token:
            # Zoho supports Authorization: Zoho-oauthtoken <token>
            headers["Authorization"] = f"Zoho-oauthtoken {self.auth_token}"

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.request(method, url, headers=headers, timeout=30, **kwargs)
                if _is_transient(resp.status_code) and attempt < MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return {}
            except (requests.RequestException, ValueError) as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
        return {}


def _format_time(raw_time: Any) -> str:
    """Format millisecond timestamp into readable UTC string."""
    if not raw_time:
        return "Unknown"
    try:
        val = float(raw_time)
        if val > 1e11:
            val /= 1000.0
        return datetime.fromtimestamp(val, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError, OSError):
        return str(raw_time)


def _render_message_markdown(
    channel_name: str,
    message: dict[str, Any],
    replies: list[dict[str, Any]] | None = None,
) -> str:
    """Render message and its thread replies into clean Markdown."""
    msg_id = message.get("id", "unknown")
    sender = message.get("sender", {}).get("name") if isinstance(message.get("sender"), dict) else (message.get("sender") or "Unknown")
    msg_time = _format_time(message.get("time"))
    
    # Text content can be in content.text or text
    content_obj = message.get("content")
    if isinstance(content_obj, dict):
        text = content_obj.get("text", "")
    else:
        text = str(message.get("text") or content_obj or "")

    sections = [
        f"# Zoho Cliq: #{channel_name} - Message by {sender}\n",
        f"- **Channel:** `#{channel_name}`",
        f"- **Message ID:** `{msg_id}`",
        f"- **Sender:** {sender}",
        f"- **Posted At:** {msg_time}\n",
        "## Message Content",
        text.strip() if text else "*(No text content)*",
    ]

    if replies:
        sections.append("\n## Thread Replies")
        for reply in replies:
            reply_sender = reply.get("sender", {}).get("name") if isinstance(reply.get("sender"), dict) else (reply.get("sender") or "Unknown")
            reply_time = _format_time(reply.get("time"))
            reply_content = reply.get("content", {})
            reply_text = reply_content.get("text", "") if isinstance(reply_content, dict) else str(reply.get("text") or reply_content or "")
            sections.append(f"- **{reply_sender}** ({reply_time}): {reply_text.strip()}")

    return "\n".join(sections)


def zoho_cliq_source(
    auth_token: str | None = None,
    domain: str | None = None,
    channel_names: list[str] | None = None,
    include_threads: bool = True,
    client: Any = None,
):
    """Create a dlt source yielding Zoho Cliq channel conversations for Cognee memory.

    Args:
        auth_token: Zoho OAuth access token. Falls back to ZOHO_CLIQ_AUTH_TOKEN.
        domain: Zoho Cliq regional domain (e.g. cliq.zoho.com, cliq.zoho.in).
        channel_names: Optional filter for specific channel names or chat IDs.
        include_threads: Whether to fetch and fold thread replies into parent messages.
        client: Optional pre-configured client for testing.

    Returns:
        A dlt source ready for cognee ingestion.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError("Zoho Cliq connector requires dlt: pip install dlt") from exc

    resolved_token = auth_token or os.environ.get("ZOHO_CLIQ_AUTH_TOKEN")
    resolved_domain = domain or os.environ.get("ZOHO_CLIQ_DOMAIN", DEFAULT_DOMAIN)

    if client is None:
        if not resolved_token:
            raise ValueError(
                "Zoho Cliq authentication required: pass auth_token= or set ZOHO_CLIQ_AUTH_TOKEN."
            )
        client = ZohoCliqClient(auth_token=resolved_token, domain=resolved_domain)

    @dlt.resource(name=ZOHO_CLIQ_TABLE_NAME, primary_key="id", write_disposition="replace")
    def fetch_messages() -> Generator[dict[str, Any], None, None]:
        channels: list[dict[str, Any]] = []
        try:
            chan_resp = client.request("GET", "/channels")
            if isinstance(chan_resp, list):
                channels = chan_resp
            elif isinstance(chan_resp, dict):
                channels = chan_resp.get("data") or chan_resp.get("channels") or []
        except Exception as e:
            logger.warning("Zoho Cliq: error fetching channels: %s", e)
            return

        if channel_names:
            channels = [c for c in channels if c.get("name") in channel_names or c.get("channel_id") in channel_names]

        count = 0
        for chan in channels:
            cid = chan.get("channel_id") or chan.get("id")
            cname = chan.get("name") or str(cid)
            if not cid:
                continue

            try:
                # Fetch recent messages in channel
                msg_resp = client.request("GET", f"/chats/{cid}/messages", params={"limit": 50})
                messages = []
                if isinstance(msg_resp, list):
                    messages = msg_resp
                elif isinstance(msg_resp, dict):
                    messages = msg_resp.get("data") or msg_resp.get("messages") or []

                for m in messages:
                    # Skip deleted or non-text status notices
                    if m.get("type") == "deleted" or (isinstance(m.get("content"), dict) and m["content"].get("deleted")):
                        continue

                    mid = m.get("id")
                    if not mid:
                        continue

                    replies = None
                    if include_threads and m.get("thread_information"):
                        try:
                            thread_chat_id = m["thread_information"].get("chat_id")
                            if thread_chat_id:
                                reply_resp = client.request("GET", f"/chats/{thread_chat_id}/messages")
                                if isinstance(reply_resp, list):
                                    replies = reply_resp
                                elif isinstance(reply_resp, dict):
                                    replies = reply_resp.get("data") or reply_resp.get("messages") or []
                        except Exception:
                            replies = None

                    rendered = _render_message_markdown(
                        channel_name=cname,
                        message=m,
                        replies=replies,
                    )

                    count += 1
                    yield {
                        "id": f"{cid}_{mid}",
                        "url": f"https://{client.domain}/#chat/{cid}",
                        "title": f"Zoho Cliq: #{cname} message {mid}",
                        "content": rendered,
                    }
            except Exception as e:
                logger.warning("Zoho Cliq: failed to fetch messages for channel %s: %s", cname, e)
                continue

        logger.info("Zoho Cliq: synced %d message(s).", count)

    @dlt.source(name=ZOHO_CLIQ_SOURCE_NAME)
    def _source():
        return fetch_messages

    source_instance = _source()
    setattr(source_instance, DOCUMENT_SOURCE_ATTR, ZOHO_CLIQ_SOURCE_NAME)
    return source_instance
