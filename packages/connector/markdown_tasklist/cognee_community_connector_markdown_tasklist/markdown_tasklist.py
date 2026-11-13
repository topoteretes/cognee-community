"""DLT source for local Markdown task-list files (- [ ] and - [x]).

Parses tasks, checkboxes, tags, priorities, and parent header context from
local Markdown files into cognee memory as structured, searchable knowledge documents.

Resolves topoteretes/cognee#6405
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

MARKDOWN_TASKLIST_SOURCE_NAME = "markdown_tasklist"
MARKDOWN_TASKLIST_TABLE_NAME = "markdown_tasklist_documents"

_EXTRA_HINT = (
    'The markdown_tasklist connector requires cognee and dlt: '
    'pip install "cognee-community-connector-markdown-tasklist"'
)

# Regex to match Markdown checkboxes:
# Supports -, *, + bullet points with [ ] or [x] or [X]
_TASK_CHECKBOX_RE = re.compile(
    r"^([ \t]*)(?:[-*+]|\d+\.)[ \t]+\[([ xX])\][ \t]+(.*)$"
)

# Regex for Markdown headers
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")

# Regex for tags (#tag), mentions (@user), due dates (due:YYYY-MM-DD), and priority tags
_TAG_RE = re.compile(r"(?:^|\s)(#[a-zA-Z0-9_-]+|@[a-zA-Z0-9_-]+|due:\d{4}-\d{2}-\d{2}|priority:(?:high|medium|low))", re.IGNORECASE)


@dataclass
class MarkdownTask:
    """Represents a single extracted checklist item."""

    text: str
    completed: bool
    line_number: int
    indent_level: int
    section: str = "General"
    tags: list[str] = field(default_factory=list)
    priority: str | None = None
    due_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarkdownTasklistDocument:
    """Represents the parsed task list summary for a single markdown file."""

    file_path: str
    relative_path: str
    filename: str
    total_tasks: int
    completed_tasks: int
    pending_tasks: int
    completion_rate: float
    tasks: list[MarkdownTask]
    rendered_markdown: str
    content_hash: str


def parse_markdown_tasklist(
    content: str,
    file_path: str = "",
    relative_path: str = "",
    filename: str = "",
    include_completed: bool = True,
) -> MarkdownTasklistDocument:
    """Parses markdown text and extracts all checklist tasks with section context."""
    if not filename:
        filename = Path(file_path).name if file_path else (relative_path or "untitled.md")
    
    current_headings: dict[int, str] = {}
    active_section = "General"
    tasks: list[MarkdownTask] = []
    
    lines = content.splitlines()
    for idx, line in enumerate(lines, start=1):
        # Check for headers to maintain hierarchical context
        heading_match = _HEADING_RE.match(line.strip())
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2).strip()
            # Update heading stack
            current_headings[level] = heading_text
            # Remove any deeper headings
            keys_to_remove = [k for k in current_headings if k > level]
            for k in keys_to_remove:
                del current_headings[k]
            # Construct active section path (e.g., "Project > Sprint 1")
            active_section = " > ".join(current_headings[k] for k in sorted(current_headings.keys()))
            continue

        # Check for checklist items
        task_match = _TASK_CHECKBOX_RE.match(line)
        if task_match:
            indent_str = task_match.group(1)
            indent_level = len(indent_str.expandtabs(4)) // 2
            is_checked = task_match.group(2).lower() == "x"
            raw_text = task_match.group(3).strip()

            if not include_completed and is_checked:
                continue

            # Extract tags, mentions, priorities, and due dates
            found_tags = [t.strip() for t in _TAG_RE.findall(raw_text)]
            
            priority = None
            due_date = None
            clean_text = raw_text
            
            for tag in found_tags:
                lower_tag = tag.lower()
                if lower_tag.startswith("priority:"):
                    priority = lower_tag.split(":", 1)[1]
                elif lower_tag.startswith("due:"):
                    due_date = lower_tag.split(":", 1)[1]

            task = MarkdownTask(
                text=clean_text,
                completed=is_checked,
                line_number=idx,
                indent_level=indent_level,
                section=active_section,
                tags=found_tags,
                priority=priority,
                due_date=due_date,
            )
            tasks.append(task)

    total = len(tasks)
    completed = sum(1 for t in tasks if t.completed)
    pending = total - completed
    rate = round((completed / total * 100.0), 1) if total > 0 else 0.0

    # Build rendered markdown document optimized for Cognee semantic retrieval
    doc_lines = [
        f"# Tasks: {filename}",
        f"**File**: `{relative_path or file_path or filename}`",
        f"**Summary**: {completed}/{total} completed ({rate}%) | {pending} pending",
        "",
    ]

    current_sec = None
    for t in tasks:
        if t.section != current_sec:
            current_sec = t.section
            doc_lines.append(f"### {current_sec}")
        
        status_box = "[x]" if t.completed else "[ ]"
        tag_str = f" `{' '.join(t.tags)}`" if t.tags else ""
        meta_str = f" *(line {t.line_number})*"
        indent = "  " * t.indent_level
        doc_lines.append(f"{indent}- {status_box} {t.text}{tag_str}{meta_str}")

    rendered_md = "\n".join(doc_lines)
    content_hash = hashlib.sha256(rendered_md.encode("utf-8")).hexdigest()

    return MarkdownTasklistDocument(
        file_path=file_path,
        relative_path=relative_path or filename,
        filename=filename,
        total_tasks=total,
        completed_tasks=completed,
        pending_tasks=pending,
        completion_rate=rate,
        tasks=tasks,
        rendered_markdown=rendered_md,
        content_hash=content_hash,
    )


def collect_markdown_tasklists(
    directory_path: str | Path,
    recursive: bool = True,
    include_completed: bool = True,
    file_pattern: str = "*.md",
    only_with_tasks: bool = True,
) -> list[MarkdownTasklistDocument]:
    """Scans local path for markdown files and parses checklist tasks."""
    base_dir = Path(directory_path).expanduser().resolve()
    if not base_dir.exists():
        raise FileNotFoundError(f"Markdown task directory does not exist: {base_dir}")

    files: list[Path] = []
    if base_dir.is_file():
        files = [base_dir]
        base_dir = base_dir.parent
    elif recursive:
        files = sorted(base_dir.rglob(file_pattern))
    else:
        files = sorted(base_dir.glob(file_pattern))

    documents: list[MarkdownTasklistDocument] = []
    for file_path in files:
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            rel_path = str(file_path.relative_to(base_dir))
            doc = parse_markdown_tasklist(
                content=content,
                file_path=str(file_path),
                relative_path=rel_path,
                include_completed=include_completed,
            )
            if only_with_tasks and doc.total_tasks == 0:
                continue
            documents.append(doc)
        except Exception:
            continue

    return documents


def markdown_tasklist_source(
    directory_path: str | Path,
    recursive: bool = True,
    include_completed: bool = True,
    file_pattern: str = "*.md",
    only_with_tasks: bool = True,
) -> Any:
    """DLT source factory for importing local Markdown task lists into cognee memory."""
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    base_path = Path(directory_path).expanduser().resolve()

    @dlt.source(name=MARKDOWN_TASKLIST_SOURCE_NAME)
    def _source() -> Any:
        @dlt.resource(
            name=MARKDOWN_TASKLIST_TABLE_NAME,
            write_disposition="replace",
            primary_key="id",
        )
        def _documents() -> Iterable[dict[str, Any]]:
            docs = collect_markdown_tasklists(
                directory_path=base_path,
                recursive=recursive,
                include_completed=include_completed,
                file_pattern=file_pattern,
                only_with_tasks=only_with_tasks,
            )
            for doc in docs:
                payload = {
                    "id": doc.content_hash,
                    "name": doc.filename,
                    "file_path": doc.file_path,
                    "relative_path": doc.relative_path,
                    "total_tasks": doc.total_tasks,
                    "completed_tasks": doc.completed_tasks,
                    "pending_tasks": doc.pending_tasks,
                    "completion_rate": doc.completion_rate,
                    "tasks": [t.to_dict() for t in doc.tasks],
                    "text": doc.rendered_markdown,
                }
                setattr(payload, DOCUMENT_SOURCE_ATTR, MARKDOWN_TASKLIST_SOURCE_NAME)
                yield payload

        return _documents()

    return _source()
