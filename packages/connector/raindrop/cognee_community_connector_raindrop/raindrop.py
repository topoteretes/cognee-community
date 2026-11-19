import logging
import dlt
from dlt.sources.helpers import requests

logger = logging.getLogger(__name__)

RAINDROP_SOURCE_NAME = "raindrop"
DOCUMENT_SOURCE_ATTR = "is_document_source"

def raindrop_bookmarks(api_token: str | None = dlt.secrets.value):
    """Returns the Raindrop dlt source."""

    @dlt.resource(name="raindrop_items", write_disposition="replace", primary_key="id")
    def _raindrop_resource():
        if not api_token:
            raise ValueError("API token is required for the Raindrop connector")

        headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json"
        }

        url = "https://api.raindrop.io/rest/v1/raindrops/0"
        page = 0

        state = dlt.current.source_state()
        seen_ids = set()
        previous_state_ids = state.get("known_ids", set())

        while True:
            params = {"perpage": 50, "page": page}
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                item_id = str(item.get("_id"))
                seen_ids.add(item_id)
                yield _raindrop_to_row(item)

            page += 1

        # Removed state-based deletions since it's replace
        state["known_ids"] = list(seen_ids)

    @dlt.source(name=RAINDROP_SOURCE_NAME)
    def _raindrop():
        return _raindrop_resource()

    source = _raindrop()
    setattr(source, DOCUMENT_SOURCE_ATTR, RAINDROP_SOURCE_NAME)
    return source

def _raindrop_to_row(item: dict) -> dict:
    title = item.get("title", "Untitled")
    url = item.get("link", "")
    excerpt = item.get("excerpt", "")
    tags = item.get("tags", [])

    content = f"Title: {title}\\nURL: {url}\\nExcerpt: {excerpt}\\nTags: {', '.join(tags)}"

    return {
        "id": f"raindrop_{item.get('_id')}",
        "title": title,
        "content": content
    }
