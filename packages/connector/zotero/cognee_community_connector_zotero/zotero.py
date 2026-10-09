"""Zotero connector – reads items from a Zotero user library via the Zotero Web API v3."""

import logging
import dlt
from dlt.sources.helpers import requests

logger = logging.getLogger(__name__)

ZOTERO_SOURCE_NAME = "zotero"
DOCUMENT_SOURCE_ATTR = "is_document_source"


def zotero_items(
    api_key: str | None = dlt.secrets.value,
    user_id: str | None = dlt.config.value,
    library_type: str = "user",
):
    """Returns a dlt source that yields Zotero library items.

    Args:
        api_key: Zotero API key (from https://www.zotero.org/settings/keys).
        user_id: Zotero user or group ID.
        library_type: "user" (default) or "group".
    """

    @dlt.resource(name="zotero_items", write_disposition="replace", primary_key="id")
    def _items_resource():
        if not api_key:
            raise ValueError("A Zotero API key is required")
        if not user_id:
            raise ValueError("A Zotero user_id (or group_id) is required")

        prefix = "users" if library_type == "user" else "groups"
        base_url = f"https://api.zotero.org/{prefix}/{user_id}/items"

        headers = {
            "Zotero-API-Key": api_key,
            "Zotero-API-Version": "3",
            "Accept": "application/json",
        }

        start = 0
        limit = 100

        while True:
            params = {"start": start, "limit": limit, "itemType": "-attachment || note"}
            resp = requests.get(base_url, headers=headers, params=params)
            resp.raise_for_status()
            items = resp.json()

            if not items:
                break

            for item in items:
                yield _item_to_row(item)

            # Zotero returns Total-Results header; check if we've fetched all
            total = int(resp.headers.get("Total-Results", 0))
            start += limit
            if start >= total:
                break

    @dlt.source(name=ZOTERO_SOURCE_NAME)
    def _zotero():
        return _items_resource()

    source = _zotero()
    setattr(source, DOCUMENT_SOURCE_ATTR, ZOTERO_SOURCE_NAME)
    return source


def _item_to_row(item: dict) -> dict:
    """Convert a Zotero API item object into a flat document row."""
    data = item.get("data", {})
    key = item.get("key", data.get("key", "unknown"))
    item_type = data.get("itemType", "")

    title = data.get("title", "Untitled")
    creators = data.get("creators", [])
    authors = "; ".join(
        f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
        for c in creators
    )
    date = data.get("date", "")
    abstract = data.get("abstractNote", "")
    url = data.get("url", "")
    tags = ", ".join(t.get("tag", "") for t in data.get("tags", []))
    publication = data.get("publicationTitle", "") or data.get("bookTitle", "")
    doi = data.get("DOI", "")

    content = (
        f"Title: {title}\n"
        f"Authors: {authors}\n"
        f"Date: {date}\n"
        f"Type: {item_type}\n"
        f"Publication: {publication}\n"
        f"DOI: {doi}\n"
        f"URL: {url}\n"
        f"Tags: {tags}\n\n"
        f"Abstract:\n{abstract}"
    )

    return {
        "id": f"zotero_{key}",
        "title": title,
        "content": content,
    }
