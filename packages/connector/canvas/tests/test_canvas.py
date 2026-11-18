"""Unit tests for Canvas LMS connector."""

import httpx
import pytest

from cognee_community_connector_canvas.canvas import (
    CanvasAuthError,
    CanvasClient,
    _parse_next_link,
    _render_assignment_markdown,
    _render_module_markdown,
    _render_page_markdown,
    _strip_verifier_and_html,
    canvas_source,
)


def test_client_requires_non_empty_token():
    with pytest.raises(CanvasAuthError):
        CanvasClient(access_token="")
    with pytest.raises(CanvasAuthError):
        CanvasClient(access_token="   ")


def test_client_handles_unauthorized(fake_canvas):
    transport = httpx.MockTransport(fake_canvas.handle_request)
    client = CanvasClient(
        access_token="wrong_token",
        base_url="https://canvas.instructure.com",
        transport=transport,
    )
    with pytest.raises(CanvasAuthError, match="401"):
        client.get_courses()


def test_client_handles_rate_limiting_retry(fake_canvas):
    fake_canvas.rate_limit_hits = 1
    transport = httpx.MockTransport(fake_canvas.handle_request)
    client = CanvasClient(
        access_token="valid_canvas_token",
        base_url="https://canvas.instructure.com",
        transport=transport,
    )
    courses = client.get_courses()
    assert len(courses) == 1
    assert courses[0]["id"] == 101


def test_client_handles_transient_503_error(fake_canvas):
    fake_canvas.server_error_hits = 1
    transport = httpx.MockTransport(fake_canvas.handle_request)
    client = CanvasClient(
        access_token="valid_canvas_token",
        base_url="https://canvas.instructure.com",
        transport=transport,
    )
    courses = client.get_courses()
    assert len(courses) == 1


def test_client_respects_max_requests_budget(fake_canvas):
    transport = httpx.MockTransport(fake_canvas.handle_request)
    client = CanvasClient(
        access_token="valid_canvas_token",
        base_url="https://canvas.instructure.com",
        transport=transport,
        max_requests=1,
    )
    client.get_courses()
    with pytest.raises(Exception, match="Request budget exceeded"):
        client.get_pages(101)


def test_strip_verifier_removes_security_parameters():
    html_with_verifier = '<p>Download file: <a href="https://canvas.edu/files/1?verifier=SEC123&download=1">Link</a></p>'
    cleaned = _strip_verifier_and_html(html_with_verifier)
    assert "verifier=" not in cleaned
    assert "SEC123" not in cleaned
    assert "Download file: Link" in cleaned


def test_strip_html_removes_tags_and_scripts():
    raw_html = "<script>alert('xss')</script><h3>Header</h3><p>Paragraph with <b>bold</b> text.</p>"
    cleaned = _strip_verifier_and_html(raw_html)
    assert "alert" not in cleaned
    assert "Header Paragraph with bold text." == cleaned


def test_parse_next_link_rfc5988():
    link_hdr = (
        '<https://canvas.instructure.com/api/v1/courses?page=1&per_page=10>; rel="current", '
        '<https://canvas.instructure.com/api/v1/courses?page=2&per_page=10>; rel="next", '
        '<https://canvas.instructure.com/api/v1/courses?page=5&per_page=10>; rel="last"'
    )
    next_url = _parse_next_link(link_hdr)
    assert next_url == "https://canvas.instructure.com/api/v1/courses?page=2&per_page=10"

    assert _parse_next_link(None) is None
    assert _parse_next_link('<https://canvas.com/api>; rel="prev"') is None


def test_render_page_markdown():
    page = {
        "course_id": 101,
        "url": "ai-ethics",
        "title": "AI Ethics",
        "body": "<p>Readings on AI safety and alignment.</p>",
        "updated_at": "2026-10-15T12:00:00Z",
    }
    doc_id, title, content = _render_page_markdown(page, "CS101")
    assert doc_id == "canvas:page:101:ai-ethics"
    assert "CS101 - Page: AI Ethics" in title
    assert "Readings on AI safety and alignment." in content


def test_render_module_markdown():
    module = {
        "course_id": 101,
        "id": 201,
        "name": "Week 2: Knowledge Graphs",
        "position": 2,
        "items": [
            {"title": "Graph Traversal Lecture", "type": "Page"},
            {"title": "RDF & Cypher Exercise", "type": "Assignment"},
        ],
    }
    doc_id, title, content = _render_module_markdown(module, "CS101")
    assert doc_id == "canvas:module:101:201"
    assert "CS101 - Module: Week 2: Knowledge Graphs" in title
    assert "[Page] Graph Traversal Lecture" in content
    assert "[Assignment] RDF & Cypher Exercise" in content


def test_render_assignment_markdown():
    assignment = {
        "course_id": 101,
        "id": 301,
        "name": "Midterm Project",
        "description": "<p>Design and evaluate a vector retrieval memory graph pipeline.</p>",
        "due_at": "2026-11-15T23:59:59Z",
        "points_possible": 150,
    }
    doc_id, title, content = _render_assignment_markdown(assignment, "CS101")
    assert doc_id == "canvas:assignment:101:301"
    assert "Due: 2026-11-15T23:59:59Z" in content
    assert "Points: 150" in content
    assert "Design and evaluate a vector retrieval" in content


def test_source_yields_all_selected_resources(fake_canvas):
    transport = httpx.MockTransport(fake_canvas.handle_request)
    source = canvas_source(
        access_token="valid_canvas_token",
        base_url="https://canvas.instructure.com",
        courses=[101],
        resources=["pages", "modules", "assignments", "announcements"],
        transport=transport,
    )
    rows = list(source)
    assert len(rows) == 4
    doc_ids = {r["id"] for r in rows}
    assert "canvas:page:101:syllabus-overview" in doc_ids
    assert "canvas:module:101:201" in doc_ids
    assert "canvas:assignment:101:301" in doc_ids
    assert "canvas:announcement:101:401" in doc_ids


def test_source_incremental_skips_unchanged_content(canvas_client, fake_canvas):
    from cognee_community_connector_canvas.canvas import _iter_canvas_rows

    state = {}
    # 1. First sync yields both items
    rows1 = list(_iter_canvas_rows(canvas_client, ["pages", "assignments"], [101], state=state))
    assert len(rows1) == 2

    # 2. Second sync with no changes yields 0 rows (content unchanged)
    rows2 = list(_iter_canvas_rows(canvas_client, ["pages", "assignments"], [101], state=state))
    assert len(rows2) == 0

    # 3. Modify assignment description -> only assignment is re-emitted
    fake_canvas.assignments[101][0]["description"] = "<p>Updated description for assignment.</p>"
    rows3 = list(_iter_canvas_rows(canvas_client, ["pages", "assignments"], [101], state=state))
    assert len(rows3) == 1
    assert rows3[0]["id"] == "canvas:assignment:101:301"

    # 4. Remove assignment -> emits tombstone with _deleted=True
    fake_canvas.assignments[101] = []
    rows4 = list(_iter_canvas_rows(canvas_client, ["pages", "assignments"], [101], state=state))
    assert len(rows4) == 1
    assert rows4[0]["id"] == "canvas:assignment:101:301"
    assert rows4[0]["_deleted"] is True
