# Local Markdown Task-List Connector for Cognee

A community data-source connector for importing local Markdown task lists (`- [ ]` and `- [x]`) into [Cognee](https://github.com/topoteretes/cognee) AI memory.

Resolves [topoteretes/cognee#6405](https://github.com/topoteretes/cognee/issues/6405).

## Overview

Many developers and engineering teams manage personal and project tasks directly in local `.md` files (such as `TODO.md`, `ROADMAP.md`, or sprint notes) rather than third-party SaaS task trackers.

This connector parses local markdown checklists, preserves hierarchical section headers (`# Epic > ## Feature`), extracts tags (`#infra`, `@alice`, `priority:high`, `due:YYYY-MM-DD`), and ingests them into Cognee's semantic memory with full metadata.

## Features

- **Standard Markdown Checklist Support**: Accurately parses `- [ ]`, `- [x]`, `* [ ]`, `* [x]`, and `+ [ ]` bullet syntax.
- **Hierarchical Section Context**: Automatically maps each task to its enclosing Markdown headings (`# Launch > ## Backend`), preventing ambiguous orphaned checklist items.
- **Tag & Metadata Extraction**: Extracts inline hashtags (`#tag`), mentions (`@username`), priority levels (`priority:high`), and due dates (`due:YYYY-MM-DD`).
- **Progress Metrics**: Calculates total, completed, pending tasks, and completion percentage for each document.
- **Incremental & Snapshot Ready**: Produces deterministic content hashes (`id`) and supports `write_disposition="replace"` for seamless orphan cleanup.
- **Recursive Directory Discovery**: Automatically scans project trees for markdown files with customizable glob patterns.

## Installation

```bash
pip install "cognee-community-connector-markdown-tasklist"
```

## Usage

### Ingesting into Cognee Memory

```python
import asyncio
from pathlib import Path
import cognee
from cognee_community_connector_markdown_tasklist import markdown_tasklist_source

async def main():
    notes_path = Path.home() / "Workspace" / "ProjectNotes"
    
    # Ingest task lists into Cognee memory
    await cognee.remember(
        markdown_tasklist_source(notes_path, recursive=True),
        dataset_name="project_tasks",
    )
    
    # Search your project memory
    results = await cognee.search("What high priority backend tasks are still pending?")
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

### Standalone Text Parsing

```python
from cognee_community_connector_markdown_tasklist import parse_markdown_tasklist

markdown_content = """# Sprint 12
- [x] Set up CI/CD pipeline #devops
- [ ] Implement database migrations priority:high
"""

doc = parse_markdown_tasklist(markdown_content, filename="sprint12.md")
print(f"Total tasks: {doc.total_tasks}, Completed: {doc.completed_tasks} ({doc.completion_rate}%)")
for task in doc.tasks:
    print(f"[{'X' if task.completed else ' '}] {task.text} (Line {task.line_number})")
```

## Running Tests

```bash
PYTHONPATH=packages/connector/markdown_tasklist pytest packages/connector/markdown_tasklist/tests -v
```
