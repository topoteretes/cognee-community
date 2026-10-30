"""DLT source for Docusaurus documentation hubs (full-snapshot sync + forget-on-delete).

Discovers, parses, and ingests Docusaurus documentation sites (sidebars.json/sidebars.js + docs/)
as standard documents for Cognee's ingestion pipeline.

Declares ``cognee_document_source = "docusaurus"`` so each page flows through the
standard cognify entity-extraction pipeline (natural language and technical documentation)
rather than the tabular dlt schema path.

Uses a full-snapshot model (``write_disposition="replace"``). Deletions propagate
automatically: any page removed from sidebars or deleted from disk falls out of the
snapshot, allowing Cognee's ``orphan_cleanup`` to prune it from the knowledge graph.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("docusaurus_connector")
except ImportError:
    logger = logging.getLogger("docusaurus_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

DOCUSAURUS_TABLE_NAME = "docusaurus_docs"
DOCUSAURUS_SOURCE_NAME = "docusaurus"

_EXTRA_HINT = (
    'The Docusaurus connector requires "dlt" and "pyyaml": '
    'pip install "dlt[sqlalchemy]" pyyaml'
)


def _load_sidebar_config(site_path: Path) -> dict[str, Any]:
    """Loads sidebar structure from sidebars.json if present."""
    for filename in ("sidebars.json", "sidebar.json"):
        cfg = site_path / filename
        if cfg.exists():
            try:
                with open(cfg, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", cfg, exc)
    return {}


def _extract_sidebar_items(
    items: Any,
    breadcrumbs: list[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """Recursively walks Docusaurus sidebar categories and items."""
    if breadcrumbs is None:
        breadcrumbs = []

    results: list[tuple[str, list[str]]] = []

    if isinstance(items, list):
        for item in items:
            results.extend(_extract_sidebar_items(item, breadcrumbs))
    elif isinstance(items, dict):
        item_type = items.get("type", "category")
        if item_type == "category":
            cat_label = items.get("label", "Category")
            sub_crumbs = [*list(breadcrumbs), cat_label]
            if "items" in items:
                results.extend(_extract_sidebar_items(items["items"], sub_crumbs))
        elif item_type == "doc":
            doc_id = items.get("id")
            if doc_id:
                results.append((doc_id, list(breadcrumbs)))
    elif isinstance(items, str):
        # Shorthand doc id string
        results.append((items, list(breadcrumbs)))

    return results


def _find_doc_file(docs_path: Path, doc_id: str) -> Path | None:
    """Resolves a doc id to an MD/MDX file inside the docs/ directory."""
    clean_id = doc_id.strip().lstrip("/")
    candidates = [
        docs_path / f"{clean_id}.mdx",
        docs_path / f"{clean_id}.md",
        docs_path / clean_id / "index.mdx",
        docs_path / clean_id / "index.md",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _parse_frontmatter_and_content(file_path: Path) -> tuple[dict[str, Any], str]:
    """Extracts YAML frontmatter and body from markdown/MDX file."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read file %s: %s", file_path, exc)
        return {}, ""

    frontmatter: dict[str, Any] = {}
    content = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            raw_fm = parts[1].strip()
            content = parts[2].strip()
            try:
                parsed = yaml.safe_load(raw_fm)
                if isinstance(parsed, dict):
                    frontmatter = parsed
            except Exception as exc:
                logger.warning("YAML parse error in %s: %s", file_path, exc)

    return frontmatter, content


def _clean_admonitions(content: str) -> str:
    """Converts Docusaurus admonitions (:::note, :::tip, etc.) into clean Markdown."""
    # Convert :::tag [optional title] \n body \n :::
    admonition_pattern = re.compile(
        r":::([a-zA-Z]+)(?:[ \t]+([^\n\r]+))?\r?\n(.*?)\r?\n:::",
        flags=re.DOTALL,
    )

    def _replace_admonition(match: re.Match) -> str:
        tag = match.group(1).capitalize()
        custom_title = match.group(2)
        body = match.group(3).strip()

        title_str = custom_title.strip() if custom_title else tag
        quoted_lines = [f"> {line}" for line in body.splitlines()]
        return f"> **{title_str}:**\n" + "\n".join(quoted_lines) + "\n"

    cleaned = admonition_pattern.sub(_replace_admonition, content)

    # Strip custom MDX import/exports
    cleaned = re.sub(r"^(import|export)\s+.*?;?\s*$", "", cleaned, flags=re.MULTILINE)

    # Strip container tags like <Tabs>, <TabItem>
    cleaned = re.sub(r"</?Tabs[^>]*>", "", cleaned)
    cleaned = re.sub(r"</?TabItem[^>]*>", "", cleaned)

    return cleaned.strip()


def _page_to_row(
    doc_id: str,
    breadcrumbs: list[str],
    file_path: Path,
) -> dict[str, Any]:
    """Flattens a parsed Docusaurus document into a normalized document row."""
    frontmatter, raw_content = _parse_frontmatter_and_content(file_path)
    body_markdown = _clean_admonitions(raw_content)

    title = (
        frontmatter.get("title")
        or frontmatter.get("sidebar_label")
        or doc_id.split("/")[-1].replace("-", " ").title()
    )
    description = frontmatter.get("description", "")
    slug = frontmatter.get("slug") or f"/{doc_id.lstrip('/')}"

    header_parts = []
    if breadcrumbs:
        header_parts.append(f"**Section:** {' > '.join(breadcrumbs)}")
    header_parts.append(f"# {title}")
    if description:
        header_parts.append(f"> {description}")

    full_content = "\n\n".join(header_parts) + "\n\n" + body_markdown

    return {
        "id": f"docusaurus:{doc_id}",
        "url": slug,
        "title": title,
        "content": full_content.strip(),
    }


def docusaurus_source(
    site_path: str | Path | None = None,
    docs_dir_name: str = "docs",
    include_docs: list[str] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Docusaurus documentation pages as markdown documents.

    Args:
        site_path: Path to the Docusaurus site directory (contains docs/ and sidebars.json).
                   Falls back to current directory or DOCUSAURUS_PATH env var.
        docs_dir_name: Subdirectory name for documents (default: 'docs').
        include_docs: Optional list of specific doc IDs to ingest.
        client: Optional pre-injected client for testing.

    Returns:
        A dlt source suitable for cognee.add(...) / cognee.remember(...).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_path = (
        Path(site_path)
        if site_path
        else Path(os.environ.get("DOCUSAURUS_PATH", "."))
    ).resolve()

    if not resolved_path.exists() or not resolved_path.is_dir():
        raise ValueError(
            f"Valid Docusaurus site directory required. '{resolved_path}' is not a directory."
        )

    docs_path = (
        resolved_path / docs_dir_name
        if (resolved_path / docs_dir_name).exists()
        else resolved_path
    )
    sidebar_config = _load_sidebar_config(resolved_path)

    @dlt.resource(name=DOCUSAURUS_TABLE_NAME, primary_key="id", write_disposition="replace")
    def docusaurus_docs() -> Iterator[dict[str, Any]]:
        synced_count = 0
        seen_ids = set()

        # 1. Walk configured sidebars
        for sidebar_name, items in sidebar_config.items():
            parsed_items = _extract_sidebar_items(items, breadcrumbs=[sidebar_name])
            for doc_id, breadcrumbs in parsed_items:
                if include_docs and doc_id not in include_docs:
                    continue

                file_path = _find_doc_file(docs_path, doc_id)
                if file_path and file_path.is_file():
                    seen_ids.add(doc_id)
                    synced_count += 1
                    yield _page_to_row(doc_id, breadcrumbs, file_path)

        # 2. Walk all docs in folder (handles autogenerated or unlinked docs)
        for ext in ("*.mdx", "*.md"):
            for p in docs_path.rglob(ext):
                if p.name.startswith((".", "_")):
                    continue
                rel_id = str(p.relative_to(docs_path).with_suffix("")).replace("\\", "/")
                if rel_id in seen_ids:
                    continue
                if include_docs and rel_id not in include_docs:
                    continue

                parts = rel_id.split("/")
                breadcrumbs = parts[:-1]
                synced_count += 1
                yield _page_to_row(rel_id, breadcrumbs, p)

        logger.info("Docusaurus: synced %d documentation page(s).", synced_count)

    @dlt.source(name=DOCUSAURUS_SOURCE_NAME)
    def _docusaurus():
        return docusaurus_docs

    source = _docusaurus()
    setattr(source, DOCUMENT_SOURCE_ATTR, DOCUSAURUS_SOURCE_NAME)
    return source
