"""DLT source for Mintlify documentation hubs (full-snapshot sync + forget-on-delete).

Discovers, parses, and ingests Mintlify documentation hubs (mint.json + MDX/MD pages)
as standard documents for Cognee's ingestion pipeline.

Declares ``cognee_document_source = "mintlify"`` so each page flows through the
standard cognify entity-extraction pipeline (the right treatment for prose and technical docs)
rather than the tabular dlt schema path.

Uses a full-snapshot model (``write_disposition="replace"``). Deletions propagate
automatically: any page removed from mint.json or deleted from disk falls out of the
snapshot, allowing Cognee's ``orphan_cleanup`` to prune it from the knowledge graph and
vector stores. Unchanged pages preserve a stable content-hash data_id, preventing redundant
re-cognification.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("mintlify_connector")
except ImportError:
    import logging

    logger = logging.getLogger("mintlify_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

MINTLIFY_TABLE_NAME = "mintlify_docs"
MINTLIFY_SOURCE_NAME = "mintlify"

_EXTRA_HINT = (
    'The Mintlify connector requires "dlt" and "pyyaml": '
    'pip install "dlt[sqlalchemy]" pyyaml'
)


def _load_mint_config(docs_path: Path) -> dict[str, Any]:
    """Loads mint.json configuration from the target documentation directory."""
    config_file = docs_path / "mint.json"
    if not config_file.exists():
        return {}

    try:
        with open(config_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Could not read mint.json at %s: %s", config_file, exc)
        return {}


def _extract_navigation_pages(
    nav: Any,
    breadcrumbs: list[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """Recursively walks Mintlify's navigation tree and returns (page_slug, breadcrumbs)."""
    if breadcrumbs is None:
        breadcrumbs = []

    pages: list[tuple[str, list[str]]] = []

    if isinstance(nav, list):
        for item in nav:
            pages.extend(_extract_navigation_pages(item, breadcrumbs))
    elif isinstance(nav, dict):
        group_name = nav.get("group")
        sub_breadcrumbs = list(breadcrumbs)
        if group_name:
            sub_breadcrumbs.append(str(group_name))

        if "pages" in nav:
            pages.extend(_extract_navigation_pages(nav["pages"], sub_breadcrumbs))
    elif isinstance(nav, str):
        # A single page slug
        pages.append((nav, list(breadcrumbs)))

    return pages


def _find_page_file(docs_path: Path, page_slug: str) -> Path | None:
    """Resolves page slug to a physical file (.mdx, .md, or folder index)."""
    clean_slug = page_slug.strip().lstrip("/")
    candidates = [
        docs_path / f"{clean_slug}.mdx",
        docs_path / f"{clean_slug}.md",
        docs_path / clean_slug / "index.mdx",
        docs_path / clean_slug / "index.md",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _parse_frontmatter_and_content(file_path: Path) -> tuple[dict[str, Any], str]:
    """Extracts YAML frontmatter and raw body from an MDX/MD file."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to read file %s: %s", file_path, exc)
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
            except Exception as e:
                logger.warning("YAML parse error in %s: %s", file_path, e)

    return frontmatter, content


def _clean_mdx(content: str) -> str:
    """Converts custom Mintlify MDX tags into clean semantic Markdown."""
    # 1. Strip import/export statements
    cleaned = re.sub(r"^(import|export)\s+.*?;?\s*$", "", content, flags=re.MULTILINE)

    # 2. Convert Callout tags (<Tip>, <Warning>, <Note>, <Info>, <Check>)
    callout_pattern = re.compile(
        r"<(Tip|Warning|Note|Info|Check)>(.*?)</\1>",
        flags=re.DOTALL | re.IGNORECASE,
    )

    def _replace_callout(match: re.Match) -> str:
        tag = match.group(1).capitalize()
        body = match.group(2).strip()
        quoted_lines = [f"> {line}" for line in body.splitlines()]
        return f"> **{tag}:**\n" + "\n".join(quoted_lines) + "\n"

    cleaned = callout_pattern.sub(_replace_callout, cleaned)

    # 3. Convert <Card title="..." icon="...">...</Card>
    card_pattern = re.compile(
        r'<Card\s+[^>]*title=["\'](.*?)["\'][^>]*>(.*?)</Card>',
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = card_pattern.sub(lambda m: f"### {m.group(1)}\n{m.group(2).strip()}\n", cleaned)

    # 4. Remove CardGroup and wrapper containers
    cleaned = re.sub(r"</?CardGroup>", "", cleaned)
    cleaned = re.sub(r"</?Frame[^>]*>", "", cleaned)
    cleaned = re.sub(r"</?Tabs>", "", cleaned)
    cleaned = re.sub(r"</?Tab[^>]*>", "", cleaned)
    cleaned = re.sub(r"</?AccordionGroup>", "", cleaned)

    # 5. Convert <Accordion title="...">...</Accordion>
    accordion_pattern = re.compile(
        r'<Accordion\s+[^>]*title=["\'](.*?)["\'][^>]*>(.*?)</Accordion>',
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = accordion_pattern.sub(lambda m: f"**{m.group(1)}**\n{m.group(2).strip()}\n", cleaned)

    # 6. Convert <ParamField path="..." type="...">...</ParamField>
    param_pattern = re.compile(
        r'<ParamField\s+[^>]*path=["\'](.*?)["\'][^>]*type=["\'](.*?)["\'][^>]*>(.*?)</ParamField>',
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = param_pattern.sub(
        lambda m: f"- **{m.group(1)}** (`{m.group(2)}`): {m.group(3).strip()}\n",
        cleaned,
    )

    # 7. Convert <ResponseField name="..." type="...">...</ResponseField>
    resp_pattern = re.compile(
        r'<ResponseField\s+[^>]*name=["\'](.*?)["\'][^>]*type=["\'](.*?)["\'][^>]*>(.*?)</ResponseField>',
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = resp_pattern.sub(
        lambda m: f"- **{m.group(1)}** (`{m.group(2)}`): {m.group(3).strip()}\n",
        cleaned,
    )

    # 8. Strip self-closing or remaining custom tags
    cleaned = re.sub(r"<[A-Z][a-zA-Z0-9]*\s*[^>]*/>", "", cleaned)

    return cleaned.strip()


def _page_to_row(
    page_slug: str,
    breadcrumbs: list[str],
    file_path: Path,
    site_title: str | None = None,
) -> dict[str, Any]:
    """Flattens a parsed Mintlify page into a normalized document row."""
    frontmatter, raw_content = _parse_frontmatter_and_content(file_path)
    body_markdown = _clean_mdx(raw_content)

    title = frontmatter.get("title") or page_slug.replace("-", " ").title()
    description = frontmatter.get("description", "")
    api_endpoint = frontmatter.get("api", "")

    header_parts = []
    if site_title:
        header_parts.append(f"# {site_title}")
    if breadcrumbs:
        header_parts.append(f"**Section:** {' > '.join(breadcrumbs)}")
    header_parts.append(f"## {title}")
    if description:
        header_parts.append(f"> {description}")
    if api_endpoint:
        header_parts.append(f"`API: {api_endpoint}`")

    full_content = "\n\n".join(header_parts) + "\n\n" + body_markdown

    return {
        "id": f"mintlify:{page_slug}",
        "url": f"/{page_slug.lstrip('/')}",
        "title": title,
        "content": full_content.strip(),
    }


def mintlify_source(
    docs_path: str | Path | None = None,
    include_pages: list[str] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Mintlify documentation pages as markdown documents.

    Args:
        docs_path: Path to the Mintlify documentation directory containing mint.json.
                   Falls back to current directory or MINTLIFY_DOCS_PATH env var.
        include_pages: Optional list of specific page slugs to ingest.
        client: Optional pre-injected client or filesystem handle (for testing).

    Returns:
        A dlt source suitable for cognee.add(...) / cognee.remember(...).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_path = (
        Path(docs_path)
        if docs_path
        else Path(os.environ.get("MINTLIFY_DOCS_PATH", "."))
    ).resolve()

    if not resolved_path.exists() or not resolved_path.is_dir():
        raise ValueError(
            f"Valid Mintlify docs directory required. '{resolved_path}' is not a directory."
        )

    config = _load_mint_config(resolved_path)
    site_name = config.get("name")

    @dlt.resource(name=MINTLIFY_TABLE_NAME, primary_key="id", write_disposition="replace")
    def mintlify_docs() -> Iterator[dict[str, Any]]:
        # Full-snapshot sync: each run yields all valid documentation pages currently present.
        # Deleted or moved pages fall out of the snapshot, allowing Cognee's orphan_cleanup
        # to prune them from the knowledge graph.
        synced_count = 0

        # Case 1: Walk structured mint.json navigation
        nav = config.get("navigation")
        configured_pages = _extract_navigation_pages(nav) if nav else []

        seen_slugs = set()
        for slug, breadcrumbs in configured_pages:
            if include_pages and slug not in include_pages:
                continue

            file_path = _find_page_file(resolved_path, slug)
            if file_path and file_path.is_file():
                seen_slugs.add(slug)
                synced_count += 1
                yield _page_to_row(slug, breadcrumbs, file_path, site_name)

        # Case 2: Discover any unconfigured .mdx / .md files in the repository
        if not nav or not configured_pages:
            for ext in ("*.mdx", "*.md"):
                for p in resolved_path.rglob(ext):
                    if p.name.startswith((".", "_")):
                        continue
                    rel_slug = str(p.relative_to(resolved_path).with_suffix("")).replace("\\", "/")
                    if rel_slug in seen_slugs:
                        continue
                    if include_pages and rel_slug not in include_pages:
                        continue

                    parts = rel_slug.split("/")
                    breadcrumbs = parts[:-1]
                    synced_count += 1
                    yield _page_to_row(rel_slug, breadcrumbs, p, site_name)

        logger.info("Mintlify: synced %d documentation page(s).", synced_count)

    @dlt.source(name=MINTLIFY_SOURCE_NAME)
    def _mintlify():
        return mintlify_docs

    source = _mintlify()
    setattr(source, DOCUMENT_SOURCE_ATTR, MINTLIFY_SOURCE_NAME)
    return source
