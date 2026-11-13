"""DLT source for Etherpad documents and chat history.

Fetches collaborative pads and chat messages from an Etherpad instance,
renders them to structured markdown documents, and yields them as a dlt
resource tagged with DOCUMENT_SOURCE_ATTR for cognee's entity extraction.
"""

from datetime import datetime, timezone
import os
import time
from typing import Any, Generator
import requests

try:
    from cognee.shared.logging_utils import get_logger
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
    logger = get_logger("etherpad_connector")
except ImportError:
    import logging
    logger = logging.getLogger("etherpad_connector")
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

ETHERPAD_TABLE_NAME = "etherpad_pads"
ETHERPAD_SOURCE_NAME = "etherpad"
DEFAULT_API_VERSION = "1.3.1"
MAX_RETRIES = 5


def _is_transient(status_code: int) -> bool:
    """True for transient network / server errors."""
    return status_code in (429, 500, 502, 503, 504)


class EtherpadClient:
    """Client for the Etherpad HTTP API."""

    def __init__(
        self,
        base_url: str = "http://localhost:9001",
        api_key: str | None = None,
        bearer_token: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.bearer_token = bearer_token
        self.api_version = api_version
        self.session = session or requests.Session()

    def request(self, function: str, **params) -> dict[str, Any]:
        """Call an Etherpad API endpoint with retries and response validation."""
        endpoint = f"{self.base_url}/api/{self.api_version}/{function}"
        headers = {}
        query_params = dict(params)

        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.api_key:
            query_params["apikey"] = self.api_key

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.get(endpoint, params=query_params, headers=headers, timeout=30)
                if _is_transient(resp.status_code) and attempt < MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                code = payload.get("code", 0)
                if code != 0:
                    msg = payload.get("message", "Unknown Etherpad API error")
                    raise RuntimeError(f"Etherpad API error (code {code}): {msg}")
                return payload.get("data", {})
            except (requests.RequestException, ValueError) as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
        return {}


def _format_timestamp(epoch_ms: int | None) -> str:
    """Format millisecond timestamp to readable UTC ISO string."""
    if not epoch_ms:
        return "Unknown"
    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError):
        return str(epoch_ms)


def _render_pad_markdown(
    pad_id: str,
    text: str,
    last_edited_ms: int | None,
    read_only_url: str,
    chat_history: list[dict[str, Any]] | None = None,
) -> str:
    """Render pad text and optional chat history into clean Markdown."""
    sections = [
        f"# Etherpad: {pad_id}\n",
        f"- **Pad ID:** `{pad_id}`",
        f"- **Read-Only Link:** [{read_only_url}]({read_only_url})",
        f"- **Last Edited:** {_format_timestamp(last_edited_ms)}\n",
        "## Content",
        text.strip() if text else "*(Pad is empty)*",
    ]

    if chat_history:
        sections.append("\n## Chat History")
        for msg in chat_history:
            user = msg.get("userName") or msg.get("userId") or "Anonymous"
            msg_time = _format_timestamp(msg.get("time"))
            msg_text = msg.get("text", "").strip()
            sections.append(f"- **{user}** ({msg_time}): {msg_text}")

    return "\n".join(sections)


def etherpad_source(
    base_url: str | None = None,
    api_key: str | None = None,
    bearer_token: str | None = None,
    pad_ids: list[str] | None = None,
    pad_prefix: str | None = None,
    include_chat: bool = True,
    client: Any = None,
):
    """Create a dlt source yielding Etherpad documents for Cognee memory.

    Args:
        base_url: Base URL of the Etherpad instance. Falls back to ETHERPAD_BASE_URL.
        api_key: Etherpad admin API key. Falls back to ETHERPAD_API_KEY.
        bearer_token: OAuth2/OIDC bearer token if SSO enabled. Falls back to ETHERPAD_BEARER_TOKEN.
        pad_ids: Optional explicit list of pad IDs to ingest. If None, lists all pads.
        pad_prefix: Optional prefix filter (e.g. "meeting-") applied to pad IDs.
        include_chat: Whether to fetch and append pad chat history.
        client: Optional pre-configured client for testing.

    Returns:
        A dlt source configured for document-mode ingestion.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError("Etherpad connector requires dlt: pip install dlt") from exc

    resolved_url = base_url or os.environ.get("ETHERPAD_BASE_URL", "http://localhost:9001")
    resolved_key = api_key or os.environ.get("ETHERPAD_API_KEY")
    resolved_bearer = bearer_token or os.environ.get("ETHERPAD_BEARER_TOKEN")

    if client is None:
        if not resolved_key and not resolved_bearer:
            raise ValueError(
                "Etherpad authentication required: pass api_key= or bearer_token= "
                "(or set ETHERPAD_API_KEY / ETHERPAD_BEARER_TOKEN)."
            )
        client = EtherpadClient(
            base_url=resolved_url,
            api_key=resolved_key,
            bearer_token=resolved_bearer,
        )

    @dlt.resource(name=ETHERPAD_TABLE_NAME, primary_key="id", write_disposition="replace")
    def fetch_pads() -> Generator[dict[str, Any], None, None]:
        target_pads: list[str] = []
        if pad_ids:
            target_pads = list(pad_ids)
        else:
            all_pads_data = client.request("listAllPads")
            target_pads = all_pads_data.get("padIDs", [])

        if pad_prefix:
            target_pads = [p for p in target_pads if p.startswith(pad_prefix)]

        count = 0
        for pid in target_pads:
            try:
                text_data = client.request("getText", padID=pid)
                text = text_data.get("text", "")

                last_edited_data = client.request("getLastEdited", padID=pid)
                last_edited = last_edited_data.get("lastEdited")

                # Fetch read-only ID so links do not expose edit rights
                ro_id = pid
                try:
                    ro_data = client.request("getReadOnlyID", padID=pid)
                    ro_id = ro_data.get("readOnlyID", pid)
                except Exception:
                    pass
                read_only_url = f"{client.base_url}/p/{ro_id}"

                chat_history = None
                if include_chat:
                    try:
                        chat_data = client.request("getChatHistory", padID=pid)
                        chat_history = chat_data.get("messages", [])
                    except Exception:
                        chat_history = None

                rendered = _render_pad_markdown(
                    pad_id=pid,
                    text=text,
                    last_edited_ms=last_edited,
                    read_only_url=read_only_url,
                    chat_history=chat_history,
                )

                count += 1
                yield {
                    "id": pid,
                    "url": read_only_url,
                    "title": f"Etherpad: {pid}",
                    "content": rendered,
                }
            except Exception as e:
                logger.warning("Etherpad: failed to fetch pad %s: %s", pid, e)
                continue

        logger.info("Etherpad: synced %d pad(s).", count)

    @dlt.source(name=ETHERPAD_SOURCE_NAME)
    def _source():
        return fetch_pads

    source_instance = _source()
    setattr(source_instance, DOCUMENT_SOURCE_ATTR, ETHERPAD_SOURCE_NAME)
    return source_instance
