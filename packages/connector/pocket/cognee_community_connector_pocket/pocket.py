"""Pocket reading list connector for cognee.

Connects to Pocket via the v3 REST API or parses standard HTML export archives
(``ril_export.html``) to turn saved reading lists into cognee long-term memory::

    import cognee
    from cognee_community_connector_pocket import pocket_source, pocket_export_source

    # Option 1: Live API sync with incremental updates
    await cognee.remember(
        pocket_source(
            consumer_key="YOUR_CONSUMER_KEY",
            access_token="YOUR_ACCESS_TOKEN",
            state="all",
        ),
        dataset_name="my_reading_list",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    # Option 2: Zero-credential offline HTML export file sync
    await cognee.remember(
        pocket_export_source("/path/to/ril_export.html"),
        dataset_name="my_reading_list",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Document-mode routing** — attaches ``DOCUMENT_SOURCE_ATTR = "pocket"`` so
  articles pass directly into cognee's text-document cognify pipeline.
* **Stable Primary Key** — ``pocket:{item_id}`` gives idempotent upserts.
* **Dual Ingestion Paths**:
  - Live API: fetches item metadata, authors, tags, and excerpts.
  - HTML Export: zero-config offline parsing of Pocket's canonical export.
* **Forget-on-delete & Tombstones** — articles deleted in Pocket (status == 2)
  are emitted with ``_deleted=True``; on ``merge`` this removes the rows from
  the destination so cognee's ``orphan_cleanup`` purges them from memory.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("pocket_connector")
except ImportError:
    import logging
    logger = logging.getLogger("pocket_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

POCKET_SOURCE_NAME = "pocket"
POCKET_ARTICLES_TABLE = "pocket_articles"
POCKET_API_URL = "https://getpocket.com/v3/get"


# ---------------------------------------------------------------------------
# API Helper
# ---------------------------------------------------------------------------
def _fetch_pocket_api(
    consumer_key: str,
    access_token: str,
    state: str = "all",
    since: int | None = None,
    detail_type: str = "complete",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch items from Pocket v3 API."""
    payload = {
        "consumer_key": consumer_key,
        "access_token": access_token,
        "state": state,
        "detailType": detail_type,
        "sort": "newest",
    }
    if since is not None:
        payload["since"] = since

    encoded_data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Accept": "application/json",
        "User-Agent": "cognee-community-connector-pocket/0.1.0",
    }

    req = urllib.request.Request(POCKET_API_URL, data=encoded_data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Formatters & Extractors
# ---------------------------------------------------------------------------
def _extract_authors(authors_data: Any) -> str:
    if isinstance(authors_data, dict):
        names = [a.get("name") for a in authors_data.values() if isinstance(a, dict) and a.get("name")]
        return ", ".join(names)
    elif isinstance(authors_data, list):
        names = [a.get("name") for a in authors_data if isinstance(a, dict) and a.get("name")]
        return ", ".join(names)
    return ""


def _extract_tags(tags_data: Any) -> str:
    if isinstance(tags_data, dict):
        return ", ".join(tags_data.keys())
    elif isinstance(tags_data, list):
        return ", ".join(str(t) for t in tags_data)
    return ""


def _format_timestamp(ts: Any) -> str:
    if not ts or str(ts) == "0":
        return "N/A"
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError, OverflowError):
        return str(ts)


def iter_pocket_api_articles(
    consumer_key: str | None = None,
    access_token: str | None = None,
    state: str = "all",
    since: int | None = None,
    client: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield article documents from the Pocket v3 API."""
    if client:
        data = client()
    else:
        if not consumer_key or not access_token:
            raise ValueError("Pocket API requires both consumer_key and access_token.")
        data = _fetch_pocket_api(consumer_key, access_token, state=state, since=since)

    items = data.get("list", {})
    if isinstance(items, list) and not items:
        return
    if not isinstance(items, dict):
        return

    for item_id, item in items.items():
        if not isinstance(item, dict):
            continue

        raw_id = str(item.get("item_id") or item_id)
        status = str(item.get("status", "0"))

        # Status 2 indicates deleted in Pocket
        if status == "2" or item.get("_deleted"):
            yield {
                "id": f"pocket:{raw_id}",
                "item_id": raw_id,
                "_deleted": True,
            }
            continue

        title = (
            item.get("resolved_title")
            or item.get("given_title")
            or item.get("title")
            or "Untitled Article"
        ).strip()
        url = (
            item.get("resolved_url")
            or item.get("given_url")
            or item.get("url")
            or ""
        ).strip()
        excerpt = (item.get("excerpt") or "").strip()
        authors_str = _extract_authors(item.get("authors"))
        tags_str = _extract_tags(item.get("tags"))
        time_added = _format_timestamp(item.get("time_added"))
        time_read = _format_timestamp(item.get("time_read"))

        status_label = "Archived" if status == "1" else "Unread"

        parts = [
            f"Article: {title}",
            f"URL: {url}",
            f"Status: {status_label}",
            f"Added: {time_added}",
        ]
        if authors_str:
            parts.append(f"Authors: {authors_str}")
        if tags_str:
            parts.append(f"Tags: {tags_str}")
        if time_read != "N/A":
            parts.append(f"Read: {time_read}")
        if excerpt:
            parts.append(f"Summary:\n{excerpt}")

        yield {
            "id": f"pocket:{raw_id}",
            "item_id": raw_id,
            "title": f"Article: {title}",
            "content": "\n\n".join(parts),
            "url": url,
            "status": status_label,
            "authors": authors_str,
            "tags": tags_str,
            "time_added": time_added,
            "word_count": int(item.get("word_count") or 0),
            "_deleted": False,
        }


def iter_pocket_export_articles(export_path: str | os.PathLike) -> Iterator[dict[str, Any]]:
    """Parse Pocket canonical HTML export file (ril_export.html) into documents."""
    path = Path(export_path)
    if not path.is_file():
        raise FileNotFoundError(f"Pocket export file not found: {path}")

    content = path.read_text(encoding="utf-8", errors="replace")

    # Match standard Pocket export anchors: <a href="..." time_added="..." tags="...">Title</a>
    pattern = re.compile(
        r'<a\s+href="(?P<url>[^"]*)"\s+time_added="(?P<time_added>\d+)"(?:\s+tags="(?P<tags>[^"]*)")?[^>]*>(?P<title>[^<]*)</a>',
        re.IGNORECASE,
    )

    for idx, match in enumerate(pattern.finditer(content)):
        groups = match.groupdict()
        url = groups.get("url", "").strip()
        title = groups.get("title", "").strip() or "Untitled Bookmark"
        time_added_ts = groups.get("time_added")
        time_added = _format_timestamp(time_added_ts)
        tags_str = groups.get("tags") or ""

        # Deterministic unique ID using time_added and index
        item_id = f"export_{time_added_ts}_{idx}"

        parts = [
            f"Article: {title}",
            f"URL: {url}",
            f"Added: {time_added}",
        ]
        if tags_str:
            parts.append(f"Tags: {tags_str}")

        yield {
            "id": f"pocket:{item_id}",
            "item_id": item_id,
            "title": f"Article: {title}",
            "content": "\n\n".join(parts),
            "url": url,
            "tags": tags_str,
            "time_added": time_added,
            "_deleted": False,
        }


# ---------------------------------------------------------------------------
# Public dlt Resource & Source Factories
# ---------------------------------------------------------------------------
def pocket_source(
    consumer_key: str | None = None,
    access_token: str | None = None,
    state: str = "all",
    since: int | None = None,
    client: Any = None,
):
    """Return a dlt resource for syncing Pocket articles via the REST API."""
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Pocket connector requires dlt: pip install "cognee[dlt]".'
        ) from exc

    @dlt.resource(
        name=POCKET_ARTICLES_TABLE,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def pocket_articles():
        yield from iter_pocket_api_articles(
            consumer_key=consumer_key,
            access_token=access_token,
            state=state,
            since=since,
            client=client,
        )

    setattr(pocket_articles, DOCUMENT_SOURCE_ATTR, POCKET_SOURCE_NAME)
    return pocket_articles


def pocket_export_source(export_path: str | os.PathLike):
    """Return a dlt resource for offline ingestion of a Pocket HTML export file."""
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Pocket connector requires dlt: pip install "cognee[dlt]".'
        ) from exc

    path_str = str(export_path)

    @dlt.resource(
        name=POCKET_ARTICLES_TABLE,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def pocket_articles():
        yield from iter_pocket_export_articles(path_str)

    setattr(pocket_articles, DOCUMENT_SOURCE_ATTR, POCKET_SOURCE_NAME)
    return pocket_articles
