"""Jupyter Notebook (.ipynb) data-source connector for cognee.

Extracts narrative markdown and code cells with execution outputs
into structured cognee documents.
"""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("jupyter_connector")

JUPYTER_TABLE_NAME = "jupyter_notebook_cells"
JUPYTER_SOURCE_NAME = "jupyter"


def _extract_source_text(source: Any) -> str:
    if isinstance(source, list):
        return "".join(source)
    if isinstance(source, str):
        return source
    return ""


def _extract_output_text(outputs: list[dict[str, Any]]) -> str:
    text_chunks: list[str] = []
    for out in outputs:
        out_type = out.get("output_type")
        if out_type == "stream":
            text = _extract_source_text(out.get("text", ""))
            if text.strip():
                text_chunks.append(text.strip())
        elif out_type in ("execute_result", "display_data"):
            data = out.get("data", {})
            if "text/plain" in data:
                text = _extract_source_text(data["text/plain"])
                if text.strip():
                    text_chunks.append(text.strip())
        elif out_type == "error":
            evalue = out.get("evalue", "")
            ename = out.get("ename", "")
            text_chunks.append(f"{ename}: {evalue}".strip())
    return "\n".join(text_chunks)


def _find_notebook_files(path: str | Path) -> list[Path]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Notebook path not found: {path}")
    if p.is_file():
        if p.suffix.lower() == ".ipynb":
            return [p]
        return []
    return sorted(p.rglob("*.ipynb"))


def iter_notebook_cells(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield parsed cells from one or more Jupyter notebooks."""
    files = _find_notebook_files(path)

    for nb_file in files:
        nb_name = nb_file.stem
        try:
            with open(nb_file, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("Jupyter: failed reading %s: %s", nb_file, exc)
            continue

        cells = data.get("cells") or []
        # v3 fallback
        if not cells and "worksheets" in data:
            worksheets = data.get("worksheets", [])
            if worksheets and isinstance(worksheets, list):
                cells = worksheets[0].get("cells", [])

        language = (
            data.get("metadata", {})
            .get("language_info", {})
            .get("name", "python")
        )

        for idx, cell in enumerate(cells):
            cell_type = cell.get("cell_type", "raw")
            source_text = _extract_source_text(cell.get("source", "")).strip()
            if not source_text:
                continue

            exec_count = cell.get("execution_count")
            outputs_text = ""
            if cell_type == "code":
                outputs = cell.get("outputs", [])
                outputs_text = _extract_output_text(outputs)

            cell_id = f"nb:{nb_name}:{idx}:{hashlib.sha256(source_text.encode('utf-8')).hexdigest()[:10]}"

            lines = [f"# Notebook: {nb_name} (Cell {idx + 1} - {cell_type})"]
            if cell_type == "markdown":
                lines.append(source_text)
            elif cell_type == "code":
                lines.append(f"```{language}\n{source_text}\n```")
                if outputs_text:
                    lines.append(f"**Output:**\n```\n{outputs_text}\n```")
            else:
                lines.append(source_text)

            content = "\n\n".join(lines)

            yield {
                "id": cell_id,
                "notebook_name": nb_name,
                "file_path": str(nb_file),
                "cell_index": idx,
                "cell_type": cell_type,
                "execution_count": exec_count,
                "title": f"{nb_name} [{cell_type} cell {idx + 1}]",
                "content": content,
            }


def jupyter_notebook_source(path: str | Path):
    """Create a dlt source yielding Jupyter notebook cells as documents."""
    import dlt

    @dlt.resource(name=JUPYTER_TABLE_NAME, primary_key="id", write_disposition="replace")
    def jupyter_cells():
        count = 0
        for item in iter_notebook_cells(path):
            count += 1
            yield item
        logger.info("Jupyter: synced %d notebook cell(s).", count)

    @dlt.source(name=JUPYTER_SOURCE_NAME)
    def _jupyter():
        return jupyter_cells

    source = _jupyter()
    setattr(source, DOCUMENT_SOURCE_ATTR, JUPYTER_SOURCE_NAME)
    return source
