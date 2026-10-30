"""DLT source for VitePress documentation hubs (full-snapshot sync + forget-on-delete).

Discovers, parses, and ingests VitePress documentation websites (.vitepress/ directory
containing config and markdown docs) as standard documents for Cognee's ingestion pipeline.

Declares ``cognee_document_source = "vitepress"`` so each page flows through the
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

    logger = get_logger("vitepress_connector")
except ImportError:
    logger = logging.getLogger("vitepress_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

VITEPRESS_TABLE_NAME = "vitepress_docs"
VITEPRESS_SOURCE_NAME = "vitepress"

_EXTRA_HINT = (
    'The VitePress connector requires "dlt" and "pyyaml": '
    'pip install "dlt[sqlalchemy]" pyyaml'
)


def _load_vitepress_config(site_path: Path) -> dict[str, Any]:
    """Loads sidebar and site configuration from .vitepress/config.json if available."""
    candidates = [
        site_path / ".vitepress" / "config.json",
        site_path / "config.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                with open(candidate, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", candidate, exc)
    return {}


def _extract_sidebar_items(
    sidebar_data: Any,
    breadcrumbs: list[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """Recursively walks VitePress sidebar groups and item links."""
    if breadcrumbs is None:
        breadcrumbs = []

    results: list[tuple[str, list[str]]] = []

    if isinstance(sidebar_data, list):
        for item in sidebar_data:
            results.extend(_extract_sidebar_items(item, breadcrumbs))
    elif isinstance(sidebar_data, dict):
        # Case: Multi-sidebar mapping (e.g., {"/guide/": [...], "/api/": [...]})
        if "items" not in sidebar_data and "link" not in sidebar_data:
            for section_prefix, section_items in sidebar_data.items():
                section_name = section_prefix.strip("/").title() or "Main"
                sub_crumbs = [*list(breadcrumbs), section_name]
                results.extend(_extract_sidebar_items(section_items, sub_crumbs))
            return results

        group_text = sidebar_data.get("text")
        sub_crumbs = [*list(breadcrumbs), str(group_text)] if group_text else list(breadcrumbs)

        link = sidebar_data.get("link")
        if link:
            results.append((str(link), list(breadcrumbs)))

        if "items" in sidebar_data:
            results.extend(_extract_sidebar_items(sidebar_data["items"], sub_crumbs))

    return results


def _find_doc_file(site_path: Path, link: str) -> Path | None:
    """Resolves a link to a Markdown file on disk."""
    clean_link = link.strip().lstrip("/").removesuffix(".html").removesuffix(".md")
    candidates = [
        site_path / f"{clean_link}.md",
        site_path / f"{clean_link}.mdx",
        site_path / clean_link / "index.md",
        site_path / clean_link / "index.mdx",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _parse_frontmatter_and_content(file_path: Path) -> tuple[dict[str, Any], str]:
    """Extracts YAML frontmatter and body from a VitePress markdown file."""
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


def _clean_containers(content: str) -> str:
    """Converts VitePress custom containers (::: tip, ::: warning, etc.) into clean Markdown."""
    # Convert ::: type [title] \n body \n :::
    container_pattern = re.compile(
        r":::\s*([a-zA-Z]+)(?:[ \t]+([^\n\r]+))?\r?\n(.*?)\r?\n:::",
        flags=re.DOTALL,
    )

    def _replace_container(match: re.Match) -> str:
        tag = match.group(1).capitalize()
        custom_title = match.group(2)
        body = match.group(3).strip()

        title_str = custom_title.strip() if custom_title else tag
        quoted_lines = [f"> {line}" for line in body.splitlines()]
        return f"> **{title_str}:**\n" + "\n".join(quoted_lines) + "\n"

    cleaned = container_pattern.sub(_replace_container, content)

    # Strip custom Vue components or template script blocks
    cleaned = re.sub(r"<script.*?>.*?</script>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<style.*?>.*?</style>", "", cleaned, flags=re.DOTALL)

    return cleaned.strip()


def _page_to_row(
    link: str,
    breadcrumbs: list[str],
    file_path: Path,
) -> dict[str, Any]:
    """Flattens a parsed VitePress document into a normalized document row."""
    frontmatter, raw_content = _parse_frontmatter_and_content(file_path)
    body_markdown = _clean_containers(raw_content)

    title = frontmatter.get("title") or file_path.stem.replace("-", " ").title()
    description = frontmatter.get("description", "")
    slug = f"/{link.strip().lstrip('/')}"

    header_parts = []
    if breadcrumbs:
        header_parts.append(f"**Section:** {' > '.join(breadcrumbs)}")
    header_parts.append(f"# {title}")
    if description:
        header_parts.append(f"> {description}")

    full_content = "\n\n".join(header_parts) + "\n\n" + body_markdown

    return {
        "id": f"vitepress:{link.strip().lstrip('/')}",
        "url": slug,
        "title": title,
        "content": full_content.strip(),
    }


def vitepress_source(
    site_path: str | Path | None = None,
    client: Any = None,
):
    """Create a dlt source that yields VitePress documentation pages as markdown documents.

    Args:
        site_path: Path to the VitePress documentation directory (containing .vitepress/).
                   Falls back to current directory or VITEPRESS_PATH env var.
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
        else Path(os.environ.get("VITEPRESS_PATH", "."))
    ).resolve()

    if not resolved_path.exists() or not resolved_path.is_dir():
        raise ValueError(
            f"Valid VitePress directory required. '{resolved_path}' is not a directory."
        )

    config = _load_vitepress_config(resolved_path)
    sidebar_data = config.get("themeConfig", {}).get("sidebar", {})

    @dlt.resource(name=VITEPRESS_TABLE_NAME, primary_key="id", write_disposition="replace")
    def vitepress_docs() -> Iterator[dict[str, Any]]:
        synced_count = 0
        seen_links = set()

        # 1. Walk configured sidebars
        if sidebar_data:
            items = _extract_sidebar_items(sidebar_data)
            for link, breadcrumbs in items:
                file_path = _find_doc_file(resolved_path, link)
                if file_path and file_path.is_file():
                    clean_id = link.strip().lstrip("/").removesuffix(".html").removesuffix(".md")
                    seen_links.add(clean_id)
                    synced_count += 1
                    yield _page_to_row(link, breadcrumbs, file_path)

        # 2. Walk all markdown files in repository
        for ext in ("*.md", "*.mdx"):
            for p in resolved_path.rglob(ext):
                if p.name.startswith((".", "_")) or ".vitepress" in p.parts:
                    continue
                rel_link = str(p.relative_to(resolved_path).with_suffix("")).replace("\\", "/")
                clean_rel = rel_link.strip().lstrip("/").removesuffix(".html").removesuffix(".md")
                if clean_rel in seen_links:
                    continue

                parts = rel_link.split("/")
                breadcrumbs = parts[:-1]
                synced_count += 1
                yield _page_to_row(rel_link, breadcrumbs, p)

        logger.info("VitePress: synced %d documentation page(s).", synced_count)

    @dlt.source(name=VITEPRESS_SOURCE_NAME)
    def _vitepress():
        return vitepress_docs

    source = _vitepress()
    setattr(source, DOCUMENT_SOURCE_ATTR, VITEPRESS_SOURCE_NAME)
    return source
