"""Hermetic unit tests for the markdown_tasklist connector."""

import tempfile
from pathlib import Path

import pytest

from cognee_community_connector_markdown_tasklist import (
    MARKDOWN_TASKLIST_SOURCE_NAME,
    MARKDOWN_TASKLIST_TABLE_NAME,
    collect_markdown_tasklists,
    markdown_tasklist_source,
    parse_markdown_tasklist,
)

SAMPLE_MARKDOWN = """# Project Launch
Here is the general project context.

## Infrastructure Setup
- [x] Provision PostgreSQL database on GCP #infra
- [ ] Configure Redis cluster cache #infra priority:high
- [ ] Setup SSL certificates @devops due:2026-11-20

## Frontend Application
* [x] Design dashboard wireframes #design
* [ ] Integrate WebSocket feed
+ [ ] Add accessibility ARIA labels
"""


def test_parse_markdown_tasklist_basic():
    doc = parse_markdown_tasklist(SAMPLE_MARKDOWN, filename="launch.md")
    assert doc.filename == "launch.md"
    assert doc.total_tasks == 6
    assert doc.completed_tasks == 2
    assert doc.pending_tasks == 4
    assert doc.completion_rate == round(2 / 6 * 100.0, 1)
    assert len(doc.tasks) == 6


def test_parse_markdown_tasklist_headings():
    doc = parse_markdown_tasklist(SAMPLE_MARKDOWN, filename="launch.md")
    tasks = doc.tasks
    
    # First three tasks should have section "Project Launch > Infrastructure Setup"
    assert tasks[0].section == "Project Launch > Infrastructure Setup"
    assert tasks[0].text == "Provision PostgreSQL database on GCP #infra"
    assert tasks[0].completed is True
    
    # Next three tasks should have section "Project Launch > Frontend Application"
    assert tasks[3].section == "Project Launch > Frontend Application"
    assert tasks[3].text == "Design dashboard wireframes #design"
    assert tasks[3].completed is True
    assert tasks[4].completed is False


def test_parse_markdown_tasklist_tags_and_priorities():
    doc = parse_markdown_tasklist(SAMPLE_MARKDOWN, filename="launch.md")
    tasks = doc.tasks
    
    # Task 1: #infra priority:high
    t1 = tasks[1]
    assert t1.priority == "high"
    assert "#infra" in t1.tags
    assert "priority:high" in t1.tags

    # Task 2: @devops due:2026-11-20
    t2 = tasks[2]
    assert t2.due_date == "2026-11-20"
    assert "@devops" in t2.tags


def test_parse_markdown_tasklist_exclude_completed():
    doc = parse_markdown_tasklist(SAMPLE_MARKDOWN, filename="launch.md", include_completed=False)
    assert doc.total_tasks == 4
    assert doc.completed_tasks == 0
    assert doc.pending_tasks == 4
    for t in doc.tasks:
        assert t.completed is False


def test_collect_markdown_tasklists_from_tempdir():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        sub1 = base / "team_a"
        sub1.mkdir()
        (sub1 / "tasks.md").write_text("- [ ] Task A1\n- [x] Task A2", encoding="utf-8")
        
        sub2 = base / "team_b"
        sub2.mkdir()
        (sub2 / "todo.md").write_text("# Team B\n- [ ] Task B1", encoding="utf-8")
        
        # Non-task markdown file
        (base / "notes.md").write_text("# Meeting Notes\nJust notes, no checklists here.", encoding="utf-8")

        docs = collect_markdown_tasklists(base, recursive=True, only_with_tasks=True)
        assert len(docs) == 2
        
        filenames = {d.filename for d in docs}
        assert "tasks.md" in filenames
        assert "todo.md" in filenames
        assert "notes.md" not in filenames


def test_collect_markdown_tasklists_missing_dir():
    with pytest.raises(FileNotFoundError):
        collect_markdown_tasklists("/path/that/definitely/does/not/exist_123456")


def test_markdown_tasklist_source_dlt():
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt not available in current test environment")

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        (base / "demo.md").write_text("# Demo\n- [ ] Write integration test\n- [x] Verify dlt source", encoding="utf-8")

        src = markdown_tasklist_source(base)
        assert src.name == MARKDOWN_TASKLIST_SOURCE_NAME
        
        # Test iterating over resource
        resources = list(src.selected_resources.values())
        assert len(resources) == 1
        res = resources[0]
        assert res.name == MARKDOWN_TASKLIST_TABLE_NAME
        
        items = list(res)
        assert len(items) == 1
        item = items[0]
        assert item["name"] == "demo.md"
        assert item["total_tasks"] == 2
        assert item["completed_tasks"] == 1
        assert item["pending_tasks"] == 1
