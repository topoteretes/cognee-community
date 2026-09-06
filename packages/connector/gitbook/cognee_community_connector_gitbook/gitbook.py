from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

GITBOOK_SOURCE_NAME = "gitbook"
GITBOOK_TABLE_NAME = "gitbook_pages"

def _text_from_nodes(nodes):
    """Extract plain text from GitBook text nodes."""
    parts = []

    for node in nodes:
        if node.get("object") == "text":
            for leaf in node.get("leaves", []):
                parts.append(leaf.get("text", ""))

    return "".join(parts)


def _flatten_document(nodes):
    """Convert GitBook document nodes into Markdown."""

    def render_nodes(nodes, indent=""):
        blocks = []

        for node in nodes:
            node_type = node.get("type")
            children = node.get("nodes", [])

            if node_type == "paragraph":
                text = _text_from_nodes(children)

                if text:
                    blocks.append(f"{indent}{text}")

            elif node_type and node_type.startswith("heading-"):
                level = int(node_type.split("-")[1])
                text = _text_from_nodes(children)

                if text:
                    blocks.append(f"{indent}{'#' * level} {text}")

            elif node_type in {"list-unordered", "list-ordered"}:
                lines = []
                number = 1

                for item in children:
                    for child in item.get("nodes", []):
                        if child.get("type") == "paragraph":
                            text = _text_from_nodes(
                                child.get("nodes", [])
                            )

                            if text:
                                marker = (
                                    f"{number}. "
                                    if node_type == "list-ordered"
                                    else "- "
                                )

                                lines.append(
                                    f"{indent}{marker}{text}"
                                )

                                if node_type == "list-ordered":
                                    number += 1

                        elif child.get("type") in {
                            "list-unordered",
                            "list-ordered",
                        }:
                            nested_blocks = render_nodes(
                                [child],
                                indent=indent + "  ",
                            )
                            lines.extend(nested_blocks)

                if lines:
                    blocks.append("\n".join(lines))

        return blocks

    return "\n\n".join(render_nodes(nodes))


def _page_to_row(page):
    """Convert a GitBook page response into a Cognee document row."""
    document = page.get("document", {})
    nodes = document.get("nodes", [])

    return {
        "id": page["id"],
        "title": page.get("title", ""),
        "content": _flatten_document(nodes),
        "url": page.get("urls", {}).get("app"),
        "path": page.get("path", ""),
        "created_at": page.get("createdAt"),
        "updated_at": page.get("updatedAt"),
    }


def _deleted_row(page_id):
    """Build a row that instructs dlt to hard-delete a GitBook page."""
    return {
        "id": page_id,
        "_deleted": True,
    }


def gitbook_source(api_token, space_id, request_get=None):
    import dlt
    import requests

    request_get = request_get or requests.get

    @dlt.resource(
        name=GITBOOK_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def gitbook_pages():
        resource_state = dlt.current.resource_state()
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "*/*",
        }

        base_url = "https://api.gitbook.com/v1"

        response = request_get(
            f"{base_url}/spaces/{space_id}/content",
            headers=headers,
        )
        response.raise_for_status()

        revision = response.json()
        revision_id = revision["id"]
        last_revision_id = resource_state.get("last_revision_id")

        if last_revision_id == revision_id:
            return

        response = request_get(
            f"{base_url}/spaces/{space_id}/revisions/{revision_id}/pages",
            headers=headers,
        )
        response.raise_for_status()

        pages = response.json()["pages"]
        current_ids = {page["id"] for page in pages}
        known_ids = set(resource_state.get("known_ids", []))

        for page in pages:
            page_id = page["id"]

            response = request_get(
                f"{base_url}/spaces/{space_id}/revisions/{revision_id}/page/{page_id}",
                headers=headers,
            )
            response.raise_for_status()

            yield _page_to_row(response.json())

        for deleted_id in known_ids - current_ids:
            yield _deleted_row(deleted_id)

        resource_state["last_revision_id"] = revision_id
        resource_state["known_ids"] = list(current_ids)

    source = gitbook_pages()
    setattr(source, DOCUMENT_SOURCE_ATTR, GITBOOK_SOURCE_NAME)
    return source
