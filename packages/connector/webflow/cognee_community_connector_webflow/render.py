"""Turn Webflow CMS items, collection schemas and pages into plain-text documents."""

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any

_SKIPPED_TAGS = {"script", "style", "template"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "ul", "ol", "blockquote",
    "pre", "figure", "figcaption", "table", "tr", "br", "hr",
}  # fmt: skip
_HEADINGS = {f"h{level}": "#" * level for level in range(1, 7)}

# Field types whose value is only an id (or a file) that says nothing in text.
_OPAQUE_TYPES = {"Reference", "MultiReference", "File"}
_IMAGE_TYPES = {"Image", "MultiImage"}
# Labels Webflow requires on every item; they head the document instead.
_HEADER_SLUGS = ("name", "slug")


class _TextExtractor(HTMLParser):
    """Keeps text, headings, list bullets and link targets; drops the markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._links: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        elif tag in _HEADINGS:
            self.parts.append(f"\n{_HEADINGS[tag]} ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._links.append(
                href if href.startswith(("http://", "https://", "mailto:")) else None
            )

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _HEADINGS or tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a" and self._links:
            href = self._links.pop()
            if href and not self._skip:
                self.parts.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """Render Webflow rich text (HTML) as readable plain text."""
    if not html:
        return ""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = [
        re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(extractor.parts).split("\n")
    ]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def schema_fingerprint(schema: dict) -> str:
    """Changes when a field is added, renamed, retyped or its options change.

    Item documents are rendered from the schema, so an item must be rendered
    again when it changes, even though the item's own ``lastUpdated`` did not.
    """
    fields = [
        [
            field.get("id"),
            field.get("slug"),
            field.get("displayName"),
            field.get("type"),
            (field.get("validations") or {}).get("options"),
        ]
        for field in schema.get("fields") or []
    ]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:8]


def field_text(field: dict | None, value: Any) -> str:
    """The text of one field value, or ``""`` when it has nothing worth saying."""
    field_type = (field or {}).get("type")
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if field_type in _OPAQUE_TYPES:
        return ""
    if field_type == "RichText":
        return html_to_text(str(value))
    if field_type == "Switch" or isinstance(value, bool):
        return "yes" if value else "no"
    if field_type == "Option":
        options = {
            option.get("id"): option.get("name")
            for option in ((field or {}).get("validations") or {}).get("options") or []
        }
        return str(options.get(value, value))
    if field_type in _IMAGE_TYPES:
        images = value if isinstance(value, list) else [value]
        alts = [image.get("alt") for image in images if isinstance(image, dict)]
        return ", ".join(alt for alt in alts if alt)
    if isinstance(value, list):
        return ", ".join(str(part) for part in value if part not in (None, ""))
    if isinstance(value, dict):
        return ""
    return str(value).strip()


def item_content(collection: dict, schema: dict, item: dict) -> str:
    """One CMS item as a document: a header, then ``Label: value`` per field."""
    data = item.get("fieldData") or {}
    fields = {field.get("slug"): field for field in schema.get("fields") or []}
    singular = collection.get("singularName") or "item"
    lines = [
        f'Webflow {singular} "{data.get("name") or item.get("id")}" '
        f"(collection: {collection['displayName']}, site: {collection['siteName']})"
    ]
    if data.get("slug"):
        lines.append(f"Slug: {data['slug']}")
    if item.get("isDraft"):
        lines.append("Status: draft")

    ordered = [slug for slug in fields if slug not in _HEADER_SLUGS]
    ordered += [slug for slug in data if slug not in fields and slug not in _HEADER_SLUGS]
    for slug in ordered:
        field = fields.get(slug)
        text = field_text(field, data.get(slug))
        if not text:
            continue
        label = (field or {}).get("displayName") or slug
        # Long values (rich text) read better under their label than after it.
        lines.append(f"{label}:\n{text}" if "\n" in text else f"{label}: {text}")
    return "\n".join(lines)


def card_content(collection: dict, schema: dict) -> str:
    """A collection's fields and what they mean, for the LLM to read next to its items."""
    lines = [f'Webflow collection "{collection["displayName"]}" (site: {collection["siteName"]})']
    if collection.get("slug"):
        lines.append(f"Slug: {collection['slug']}")
    if collection.get("singularName"):
        lines.append(f"Each item is a {collection['singularName']}.")
    fields = [
        field for field in schema.get("fields") or [] if field.get("slug") not in _HEADER_SLUGS
    ]
    if fields:
        lines.append("Fields:")
    for field in fields:
        detail = field.get("type") or "unknown"
        options = [o.get("name") for o in (field.get("validations") or {}).get("options") or []]
        if options:
            detail += f": {', '.join(str(option) for option in options if option)}"
        if field.get("isRequired"):
            detail += ", required"
        line = f"- {field.get('displayName') or field.get('slug')} ({detail})"
        if field.get("helpText"):
            line += f": {field['helpText']}"
        lines.append(line)
    return "\n".join(lines)


def page_content(site_name: str, page: dict, nodes: list[dict]) -> str:
    """A static page: its title, path and description, then the text on it."""
    title = page.get("title") or page.get("slug") or page.get("id")
    lines = [f'Webflow page "{title}" (site: {site_name})']
    path = page.get("publishedPath") or (f"/{page['slug']}" if page.get("slug") else "")
    if path:
        lines.append(f"Path: {path}")
    description = (page.get("seo") or {}).get("description")
    if description:
        lines.append(f"Description: {description}")
    body = [text for node in nodes if (text := _node_text(node))]
    if body:
        lines += ["", *body]
    return "\n".join(lines)


def _node_text(node: dict) -> str:
    kind = node.get("type")
    if kind == "text":
        text = node.get("text") or {}
        return (text.get("text") or html_to_text(text.get("html"))).strip()
    if kind == "image":
        return ((node.get("image") or {}).get("alt") or "").strip()
    if kind == "component-instance":
        texts = []
        for override in node.get("propertyOverrides") or []:
            text = override.get("text") or {}
            texts.append((text.get("text") or html_to_text(text.get("html"))).strip())
        return "\n".join(text for text in texts if text)
    return ""
