"""DLT source for Fern API documentation hubs (full-snapshot sync + forget-on-delete).

Discovers, parses, and ingests Fern API definitions and documentation (fern/ directory containing
fern.config.json, definition/ API YAML files, and markdown guides) as standard documents
for Cognee's ingestion pipeline.

Declares ``cognee_document_source = "fern"`` so each API endpoint and guide flows through
the standard cognify entity-extraction pipeline (for natural language and technical schemas)
rather than the tabular dlt schema path.

Uses a full-snapshot model (``write_disposition="replace"``). Deletions propagate
automatically: any endpoint or page removed from Fern definitions falls out of the snapshot,
allowing Cognee's ``orphan_cleanup`` to prune it from the knowledge graph.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("fern_connector")
except ImportError:
    logger = logging.getLogger("fern_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

FERN_TABLE_NAME = "fern_docs"
FERN_SOURCE_NAME = "fern"

_EXTRA_HINT = (
    'The Fern connector requires "dlt" and "pyyaml": '
    'pip install "dlt[sqlalchemy]" pyyaml'
)


def _load_fern_config(site_path: Path) -> dict[str, Any]:
    """Loads organization metadata from fern.config.json."""
    for candidate in (
        site_path / "fern.config.json",
        site_path / "fern" / "fern.config.json",
    ):
        if candidate.is_file():
            try:
                with open(candidate, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", candidate, exc)
    return {}


def _resolve_fern_dirs(base_path: Path) -> tuple[Path, Path]:
    """Finds root fern directory and definition/ directory."""
    if (base_path / "fern.config.json").is_file() or (base_path / "definition").is_dir():
        fern_root = base_path
    elif (base_path / "fern").is_dir():
        fern_root = base_path / "fern"
    else:
        fern_root = base_path

    definition_dir = fern_root / "definition" if (fern_root / "definition").is_dir() else fern_root
    return fern_root, definition_dir


def _render_endpoint(
    service_name: str,
    base_path: str,
    endpoint_id: str,
    endpoint_data: dict[str, Any],
) -> dict[str, Any]:
    """Renders a single Fern endpoint definition into a normalized document row."""
    method = endpoint_data.get("method", "GET").upper()
    path = endpoint_data.get("path", "")
    full_path = f"{base_path.rstrip('/')}/{path.lstrip('/')}".rstrip("/")
    if not full_path.startswith("/"):
        full_path = f"/{full_path}"

    display_name = endpoint_data.get("display-name") or endpoint_id.replace("-", " ").title()
    docs = endpoint_data.get("docs", "")

    lines = [
        f"# {service_name}: {display_name}",
        f"**Endpoint:** `{method} {full_path}`",
    ]
    if docs:
        lines.append(f"> {docs.strip()}")

    # Path Parameters
    path_params = endpoint_data.get("path-parameters") or {}
    if path_params:
        lines.append("\n### Path Parameters")
        for p_name, p_val in path_params.items():
            p_type = p_val if isinstance(p_val, str) else p_val.get("type", "string")
            p_doc = p_val.get("docs", "") if isinstance(p_val, dict) else ""
            doc_str = f" - {p_doc}" if p_doc else ""
            lines.append(f"- **{p_name}** (`{p_type}`){doc_str}")

    # Query Parameters
    query_params = endpoint_data.get("request", {}).get("query-parameters") or {}
    if query_params:
        lines.append("\n### Query Parameters")
        for q_name, q_val in query_params.items():
            q_type = q_val if isinstance(q_val, str) else q_val.get("type", "string")
            q_doc = q_val.get("docs", "") if isinstance(q_val, dict) else ""
            doc_str = f" - {q_doc}" if q_doc else ""
            lines.append(f"- **{q_name}** (`{q_type}`){doc_str}")

    # Request Body
    req_body = endpoint_data.get("request", {}).get("body")
    if req_body:
        lines.append("\n### Request Body")
        if isinstance(req_body, dict):
            b_type = req_body.get("type", "object")
            lines.append(f"Type: `{b_type}`")
        else:
            lines.append(f"Type: `{req_body}`")

    # Response Body
    response_info = endpoint_data.get("response", {})
    if response_info:
        lines.append("\n### Response")
        r_type = (
            response_info.get("type", "object")
            if isinstance(response_info, dict)
            else response_info
        )
        lines.append(f"Returns: `{r_type}`")

    # Errors
    errors = endpoint_data.get("errors") or []
    if errors:
        lines.append("\n### Errors")
        for err in errors:
            err_name = err if isinstance(err, str) else err.get("type", "Error")
            lines.append(f"- `{err_name}`")

    content = "\n".join(lines)
    return {
        "id": f"fern:endpoint:{service_name}:{endpoint_id}",
        "url": full_path,
        "title": f"{method} {full_path} - {display_name}",
        "content": content,
    }


def _render_guide(file_path: Path, root_path: Path) -> dict[str, Any]:
    """Renders a Fern markdown guide into a normalized document row."""
    text = file_path.read_text(encoding="utf-8")
    title = file_path.stem.replace("-", " ").title()

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1])
                if isinstance(fm, dict) and "title" in fm:
                    title = fm["title"]
            except Exception:
                pass
            text = parts[2].strip()

    rel_path = str(file_path.relative_to(root_path).with_suffix("")).replace("\\", "/")
    return {
        "id": f"fern:guide:{rel_path}",
        "url": f"/{rel_path}",
        "title": title,
        "content": f"# {title}\n\n{text}".strip(),
    }


def fern_source(
    fern_path: str | Path | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Fern API endpoints and documentation pages.

    Args:
        fern_path: Path to the Fern repository or fern/ directory.
                   Falls back to current directory or FERN_PATH env var.
        client: Optional pre-injected client for testing.

    Returns:
        A dlt source suitable for cognee.add(...) / cognee.remember(...).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_path = (
        Path(fern_path)
        if fern_path
        else Path(os.environ.get("FERN_PATH", "."))
    ).resolve()

    if not resolved_path.exists() or not resolved_path.is_dir():
        raise ValueError(
            f"Valid Fern directory required. '{resolved_path}' is not a directory."
        )

    fern_root, definition_dir = _resolve_fern_dirs(resolved_path)
    _load_fern_config(fern_root)

    @dlt.resource(name=FERN_TABLE_NAME, primary_key="id", write_disposition="replace")
    def fern_docs() -> Iterator[dict[str, Any]]:
        synced_count = 0

        # 1. Process API Definition YAML files
        for yml_file in definition_dir.rglob("*.yml"):
            if yml_file.name in ("generators.yml", "versions.yml"):
                continue
            try:
                data = yaml.safe_load(yml_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to parse YAML %s: %s", yml_file, exc)
                continue

            if not isinstance(data, dict):
                continue

            service = data.get("service")
            if isinstance(service, dict) and "endpoints" in service:
                service_name = yml_file.stem
                base_path = service.get("base-path", "")
                endpoints = service.get("endpoints", {})
                for ep_id, ep_info in endpoints.items():
                    if isinstance(ep_info, dict):
                        synced_count += 1
                        yield _render_endpoint(service_name, base_path, ep_id, ep_info)

        # 2. Process Markdown / MDX documentation guides
        for ext in ("*.mdx", "*.md"):
            for doc_file in fern_root.rglob(ext):
                if doc_file.name.startswith((".", "_")):
                    continue
                synced_count += 1
                yield _render_guide(doc_file, fern_root)

        logger.info("Fern: synced %d API resource(s) and guide(s).", synced_count)

    @dlt.source(name=FERN_SOURCE_NAME)
    def _fern():
        return fern_docs

    source = _fern()
    setattr(source, DOCUMENT_SOURCE_ATTR, FERN_SOURCE_NAME)
    return source
