"""DLT source for Mastra.ai agent conversation threads and memory.

Fetches persisted threads, messages, and tool interactions from a Mastra application,
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
    logger = get_logger("mastra_connector")
except ImportError:
    import logging
    logger = logging.getLogger("mastra_connector")
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

MASTRA_TABLE_NAME = "mastra_threads"
MASTRA_SOURCE_NAME = "mastra"
MAX_RETRIES = 5


def _is_transient(status_code: int) -> bool:
    """True for transient network / server errors."""
    return status_code in (429, 500, 502, 503, 504)


class MastraClient:
    """Client for Mastra.ai Memory and Agent REST APIs."""

    def __init__(
        self,
        api_url: str = "http://localhost:4111",
        api_key: str | None = None,
        session: requests.Session | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.session = session or requests.Session()

    def request(self, method: str, path: str, **kwargs) -> Any:
        """Call Mastra API with backoff on transient errors."""
        url = f"{self.api_url}/{path.lstrip('/')}"
        headers = dict(kwargs.pop("headers", {}))
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

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
    """Format timestamp to UTC string."""
    if not raw_time:
        return "Unknown"
    if isinstance(raw_time, (int, float)):
        # Milliseconds or seconds
        if raw_time > 1e11:
            raw_time /= 1000.0
        try:
            return datetime.fromtimestamp(raw_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (ValueError, OSError):
            return str(raw_time)
    return str(raw_time)


def _render_thread_markdown(
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    include_tools: bool = True,
) -> str:
    """Render conversation thread and its messages to structured markdown."""
    thread_id = thread.get("id", "unknown")
    title = thread.get("title") or thread.get("name") or f"Thread {thread_id}"
    resource_id = thread.get("resourceId") or thread.get("resource_id") or "default"
    created_at = _format_time(thread.get("createdAt") or thread.get("created_at"))
    updated_at = _format_time(thread.get("updatedAt") or thread.get("updated_at"))

    sections = [
        f"# Mastra Conversation: {title}\n",
        f"- **Thread ID:** `{thread_id}`",
        f"- **Resource ID:** `{resource_id}`",
        f"- **Created At:** {created_at}",
        f"- **Updated At:** {updated_at}\n",
        "## Conversation Transcript",
    ]

    for msg in messages:
        role = (msg.get("role") or "UNKNOWN").upper()
        msg_time = _format_time(msg.get("createdAt") or msg.get("created_at"))
        content = msg.get("content", "").strip()

        sections.append(f"\n### {role} ({msg_time})")
        if content:
            sections.append(content)

        if include_tools:
            tool_calls = msg.get("toolCalls") or msg.get("tool_calls") or []
            for tool in tool_calls:
                fn_name = tool.get("function", {}).get("name") if isinstance(tool.get("function"), dict) else tool.get("name", "tool")
                fn_args = tool.get("function", {}).get("arguments") if isinstance(tool.get("function"), dict) else tool.get("args", "")
                sections.append(f"> **Tool Call:** `{fn_name}({fn_args})`")

            tool_result = msg.get("toolResult") or msg.get("tool_result")
            if tool_result:
                sections.append(f"> **Tool Result:** `{tool_result}`")

    return "\n".join(sections)


def mastra_source(
    api_url: str | None = None,
    api_key: str | None = None,
    resource_id: str | None = None,
    thread_ids: list[str] | None = None,
    include_tools: bool = True,
    client: Any = None,
):
    """Create a dlt source yielding Mastra conversation documents for Cognee memory.

    Args:
        api_url: Base URL of the Mastra deployment. Falls back to MASTRA_API_URL.
        api_key: Optional Bearer token or API key. Falls back to MASTRA_API_KEY.
        resource_id: Filter threads by specific Mastra agent resource ID.
        thread_ids: Optional explicit list of thread IDs to ingest.
        include_tools: Whether to render tool calls and results in transcript.
        client: Optional pre-configured client for testing.

    Returns:
        A dlt source ready for cognee ingestion.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError("Mastra connector requires dlt: pip install dlt") from exc

    resolved_url = api_url or os.environ.get("MASTRA_API_URL", "http://localhost:4111")
    resolved_key = api_key or os.environ.get("MASTRA_API_KEY")

    if client is None:
        client = MastraClient(api_url=resolved_url, api_key=resolved_key)

    @dlt.resource(name=MASTRA_TABLE_NAME, primary_key="id", write_disposition="replace")
    def fetch_threads() -> Generator[dict[str, Any], None, None]:
        threads_to_sync: list[dict[str, Any]] = []

        if thread_ids:
            for tid in thread_ids:
                try:
                    t_data = client.request("GET", f"/api/memory/threads/{tid}")
                    if t_data:
                        threads_to_sync.append(t_data if isinstance(t_data, dict) else {"id": tid})
                except Exception as e:
                    logger.warning("Mastra: error fetching thread %s: %s", tid, e)
        else:
            params = {}
            if resource_id:
                params["resourceId"] = resource_id
            try:
                resp = client.request("GET", "/api/memory/threads", params=params)
                if isinstance(resp, list):
                    threads_to_sync = resp
                elif isinstance(resp, dict):
                    threads_to_sync = resp.get("threads") or resp.get("data") or []
            except Exception as e:
                logger.warning("Mastra: error listing threads: %s", e)
                return

        count = 0
        for thread in threads_to_sync:
            tid = thread.get("id")
            if not tid:
                continue

            try:
                messages_resp = client.request("GET", f"/api/memory/threads/{tid}/messages")
                messages = []
                if isinstance(messages_resp, list):
                    messages = messages_resp
                elif isinstance(messages_resp, dict):
                    messages = messages_resp.get("messages") or messages_resp.get("data") or []

                rendered = _render_thread_markdown(
                    thread=thread,
                    messages=messages,
                    include_tools=include_tools,
                )

                count += 1
                yield {
                    "id": str(tid),
                    "url": f"{client.api_url}/agents/threads/{tid}",
                    "title": thread.get("title") or f"Mastra Thread {tid}",
                    "content": rendered,
                }
            except Exception as e:
                logger.warning("Mastra: failed to fetch messages for thread %s: %s", tid, e)
                continue

        logger.info("Mastra: synced %d thread(s).", count)

    @dlt.source(name=MASTRA_SOURCE_NAME)
    def _source():
        return fetch_threads

    source_instance = _source()
    setattr(source_instance, DOCUMENT_SOURCE_ATTR, MASTRA_SOURCE_NAME)
    return source_instance
