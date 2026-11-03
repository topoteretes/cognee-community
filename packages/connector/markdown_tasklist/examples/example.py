"""Example showing how to use the markdown_tasklist connector with cognee."""

import asyncio
from pathlib import Path

from cognee_community_connector_markdown_tasklist import (
    collect_markdown_tasklists,
    markdown_tasklist_source,
    parse_markdown_tasklist,
)


def run_standalone_demo():
    sample_content = """# Q4 Engineering Roadmap
## Backend Infra
- [x] Migrate database clusters #infra
- [ ] Implement rate-limiting middleware #security priority:high
- [ ] Set up continuous performance profiling @alice due:2026-11-15

## Developer Experience
- [x] Write API documentation
- [ ] Add hermetic unit test coverage for connectors
"""
    doc = parse_markdown_tasklist(sample_content, filename="roadmap.md")
    print(f"Parsed {doc.total_tasks} tasks from {doc.filename}:")
    print(f"Completed: {doc.completed_tasks}/{doc.total_tasks} ({doc.completion_rate}%)")
    print("\nRendered Markdown for Cognee:")
    print(doc.rendered_markdown)


async def run_cognee_ingestion_demo():
    try:
        import cognee
    except ImportError:
        print("cognee is not installed in this environment.")
        return

    notes_dir = Path.home() / "Documents" / "Notes"
    if not notes_dir.exists():
        print(f"Demo directory {notes_dir} does not exist.")
        return

    # Ingest task lists into cognee memory
    await cognee.remember(
        markdown_tasklist_source(notes_dir, recursive=True),
        dataset_name="project_tasks",
    )
    print("Ingested markdown task lists into cognee memory!")


if __name__ == "__main__":
    run_standalone_demo()
