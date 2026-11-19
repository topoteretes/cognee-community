"""DLT source for Jupyter Notebooks (.ipynb) (full-snapshot sync + forget-on-delete).

Statically parses Jupyter notebook files without kernel execution (OWASP LLM02 zero-code-execution).
Extracts markdown narrative cells, code cells, and text outputs, and formats them into
normalized documents for Cognee's GraphRAG ingestion pipeline.

Declares ``cognee_document_source = "jupyter"`` with ``write_disposition="replace"``,
so deleted or renamed notebooks automatically prune out of Cognee's graph and vector stores
via orphan cleanup.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# dlt resource / table names
JUPYTER_TABLE_NAME = "jupyter_notebooks"
JUPYTER_SOURCE_NAME = "jupyter"

# Attribute marker required by Cognee document ingestion
DOCUMENT_SOURCE_ATTR = "cognee_document_source"

# Secret patterns to scrub
_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[a-zA-Z0-9_\-\.]{15,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*['\"])[^'\"]{10,}(['\"])", re.IGNORECASE),
    re.compile(r"(?i)(password\s*[:=]\s*['\"])[^'\"]+(['\"])", re.IGNORECASE),
    re.compile(r"(?i)(secret\s*[:=]\s*['\"])[^'\"]{8,}(['\"])", re.IGNORECASE),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\b(ghp_[a-zA-Z0-9]{20,})\b"),
    re.compile(r"\b(sk-[a-zA-Z0-9]{20,})\b"),
]

# ANSI escape sequence stripper for notebook error tracebacks
_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def redact_sensitive_data(text: str) -> str:
    """Scrub tokens, API keys, and credentials from text content."""
    if not text:
        return ""
    result = text
    for pattern in _SECRET_PATTERNS:
        replacement = r"\1[REDACTED]\2" if r"\2" in pattern.pattern else "[REDACTED]"
        result = pattern.sub(replacement, result)
    return result


def _clean_source(source: Any) -> str:
    """Normalize cell source into a single string."""
    if isinstance(source, list):
        return "".join(source)
    if isinstance(source, str):
        return source
    return ""


def _strip_ansi(text: str) -> str:
    """Remove terminal color codes from traceback lines."""
    return _ANSI_ESCAPE.sub("", text)


def _render_outputs(outputs: list[dict], max_chars: int = 2000) -> str:
    """Extract plain text, stdout, and error traceback from cell outputs."""
    parts: list[str] = []
    current_length = 0

    for out in outputs:
        if not isinstance(out, dict):
            continue

        output_type = out.get("output_type")
        rendered = ""

        if output_type == "stream":
            text = _clean_source(out.get("text", ""))
            if text.strip():
                rendered = f"[stdout]\n{text.strip()}"
        elif output_type in ("execute_result", "display_data"):
            data = out.get("data", {})
            if isinstance(data, dict):
                plain = data.get("text/plain")
                if plain:
                    text = _clean_source(plain)
                    if text.strip():
                        rendered = f"[output]\n{text.strip()}"
        elif output_type == "error":
            ename = out.get("ename", "Error")
            evalue = out.get("evalue", "")
            traceback = out.get("traceback", [])
            tb_clean = "\n".join(_strip_ansi(line) for line in traceback) if traceback else evalue
            rendered = f"[{ename}]\n{tb_clean.strip()}"

        if rendered:
            if current_length + len(rendered) > max_chars:
                remaining = max_chars - current_length
                if remaining > 50:
                    parts.append(rendered[:remaining] + "\n... [output truncated]")
                break
            parts.append(rendered)
            current_length += len(rendered)

    return "\n\n".join(parts)


def parse_notebook_content(
    notebook_json: dict | str,
    include_outputs: bool = True,
    max_output_chars: int = 2000,
    redact_secrets: bool = True,
    fallback_title: str = "Untitled Notebook",
) -> tuple[str, str]:
    """Parse a Jupyter notebook dict or JSON string into title and markdown prose.

    Returns:
        tuple of (title, markdown_content)
    """
    if isinstance(notebook_json, str):
        data = json.loads(notebook_json)
    elif isinstance(notebook_json, dict):
        data = notebook_json
    else:
        raise ValueError("notebook_json must be a JSON string or dict")

    cells = data.get("cells")
    if cells is None or not isinstance(cells, list) or (not cells and "worksheets" in data):
        # Support nbformat v3 worksheets fallback
        worksheets = data.get("worksheets", [])
        if isinstance(worksheets, list) and worksheets and isinstance(worksheets[0], dict):
            cells = worksheets[0].get("cells", [])
        else:
            cells = cells if isinstance(cells, list) else []

    title = fallback_title
    sections: list[str] = []

    for cell in cells:
        if not isinstance(cell, dict):
            continue

        cell_type = cell.get("cell_type", "")
        source_text = _clean_source(cell.get("source", "")).strip()

        if not source_text and not cell.get("outputs"):
            continue

        if cell_type == "markdown":
            # Check if first markdown cell contains a H1 title
            if title == fallback_title and source_text.startswith("# "):
                title_line = source_text.splitlines()[0]
                extracted_title = title_line.lstrip("#").strip()
                if extracted_title:
                    title = extracted_title

            sections.append(source_text)

        elif cell_type == "code":
            lang = "python"
            # Attempt to read kernel language from notebook metadata
            metadata = data.get("metadata", {})
            if isinstance(metadata, dict):
                kernel_spec = metadata.get("language_info", {})
                if isinstance(kernel_spec, dict) and kernel_spec.get("name"):
                    lang = kernel_spec["name"]

            code_block = f"```{lang}\n{source_text}\n```"
            if include_outputs and cell.get("outputs"):
                rendered_out = _render_outputs(cell.get("outputs", []), max_chars=max_output_chars)
                if rendered_out:
                    code_block += f"\n\n```output\n{rendered_out}\n```"

            sections.append(code_block)

        elif cell_type == "raw":
            sections.append(f"```text\n{source_text}\n```")

    full_content = "\n\n".join(sections)
    if redact_secrets:
        full_content = redact_sensitive_data(full_content)
        title = redact_sensitive_data(title)

    return title, full_content


def _find_notebook_files(
    notebook_paths: Iterable[str | Path] | None = None,
    workspace_dir: str | Path | None = None,
) -> list[Path]:
    """Discover all valid .ipynb files from paths or directory."""
    discovered: list[Path] = []

    if notebook_paths:
        for p in notebook_paths:
            path = Path(p)
            if path.is_file() and path.suffix.lower() == ".ipynb":
                discovered.append(path)
            elif path.is_dir():
                discovered.extend(path.rglob("*.ipynb"))

    if workspace_dir:
        root = Path(workspace_dir)
        if root.is_dir():
            discovered.extend(root.rglob("*.ipynb"))

    # Deduplicate while preserving order
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in discovered:
        resolved = path.resolve()
        if resolved not in seen and resolved.exists():
            seen.add(resolved)
            unique.append(path)

    return unique


def jupyter_source(
    notebook_paths: list[str | Path] | None = None,
    workspace_dir: str | Path | None = None,
    include_outputs: bool = True,
    max_output_chars: int = 2000,
    redact_secrets: bool = True,
):
    """Create a dlt source that yields Jupyter notebooks as markdown documents.

    Args:
        notebook_paths: Explicit list of notebook file or directory paths.
        workspace_dir: Root workspace path to scan recursively for .ipynb files.
        include_outputs: Whether to extract cell execution outputs (text/plain, stdout, errors).
        max_output_chars: Maximum characters of outputs per cell to prevent memory spikes.
        redact_secrets: Whether to scrub Bearer tokens, API keys, and passwords.

    Returns:
        A dlt source suitable for cognee.add(...) / cognee.remember(...).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Jupyter connector requires dlt. Install with: pip install 'dlt[sqlalchemy]>=1.9.0'"
        ) from exc

    @dlt.resource(name=JUPYTER_TABLE_NAME, primary_key="id", write_disposition="replace")
    def jupyter_notebooks():
        files = _find_notebook_files(notebook_paths, workspace_dir)
        for file_path in files:
            try:
                with open(file_path, encoding="utf-8") as fp:
                    content_text = fp.read()
            except (UnicodeDecodeError, OSError):
                # Retry with latin-1 or skip corrupt read
                try:
                    with open(file_path, encoding="latin-1") as fp:
                        content_text = fp.read()
                except Exception:
                    continue

            try:
                title, rendered_content = parse_notebook_content(
                    content_text,
                    include_outputs=include_outputs,
                    max_output_chars=max_output_chars,
                    redact_secrets=redact_secrets,
                    fallback_title=file_path.stem.replace("_", " ").title(),
                )
            except Exception:
                # Malformed JSON or schema error - skip invalid notebook
                continue

            if not rendered_content.strip():
                continue

            file_id = hashlib.sha256(str(file_path.resolve()).encode("utf-8")).hexdigest()
            yield {
                "id": file_id,
                "url": file_path.resolve().as_uri(),
                "title": title,
                "content": rendered_content,
            }

    @dlt.source(name=JUPYTER_SOURCE_NAME)
    def _jupyter():
        return jupyter_notebooks

    source = _jupyter()
    setattr(source, DOCUMENT_SOURCE_ATTR, JUPYTER_SOURCE_NAME)
    return source
