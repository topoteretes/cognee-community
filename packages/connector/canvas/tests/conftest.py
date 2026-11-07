"""Fixtures and mock Canvas LMS server using httpx.MockTransport."""

from typing import Any

import httpx
import pytest

from cognee_community_connector_canvas.canvas import CanvasClient


class FakeCanvasServer:
    """Mock in-memory Canvas LMS API server."""

    def __init__(self) -> None:
        self.valid_token = "valid_canvas_token"
        self.rate_limit_hits = 0
        self.server_error_hits = 0

        self.courses: list[dict[str, Any]] = [
            {"id": 101, "name": "CS101: Intro to AI & Memory", "course_code": "CS101"}
        ]
        self.pages: dict[int, list[dict[str, Any]]] = {
            101: [
                {
                    "id": "p1",
                    "url": "syllabus-overview",
                    "title": "Syllabus & Overview",
                    "body": (
                        "<p>Welcome to CS101. Download materials at "
                        '<a href="https://canvas.instructure.com/files/99?verifier=SECRET_V">Slides</a></p>'
                    ),
                    "updated_at": "2026-10-15T10:00:00Z",
                    "html_url": "https://canvas.instructure.com/courses/101/pages/syllabus-overview",
                }
            ]
        }
        self.modules: dict[int, list[dict[str, Any]]] = {
            101: [
                {
                    "id": 201,
                    "name": "Week 1: Foundations",
                    "position": 1,
                    "items": [
                        {"id": 1, "title": "Lecture 1 Reading", "type": "Page"},
                        {"id": 2, "title": "Lab 1 Setup", "type": "Assignment"},
                    ],
                }
            ]
        }
        self.assignments: dict[int, list[dict[str, Any]]] = {
            101: [
                {
                    "id": 301,
                    "name": "Project Proposal",
                    "description": "<p>Submit a 2-page project proposal on memory graphs.</p>",
                    "due_at": "2026-11-01T23:59:59Z",
                    "points_possible": 100,
                    "updated_at": "2026-10-16T12:00:00Z",
                    "html_url": "https://canvas.instructure.com/courses/101/assignments/301",
                }
            ]
        }
        self.announcements: dict[int, list[dict[str, Any]]] = {
            101: [
                {
                    "id": 401,
                    "title": "Midterm Exam Logistics",
                    "message": "<p>The midterm will cover weeks 1 through 4.</p>",
                    "user_name": "Prof. Smith",
                    "posted_at": "2026-10-20T09:00:00Z",
                    "html_url": "https://canvas.instructure.com/courses/101/discussion_topics/401",
                }
            ]
        }
        self.discussions: dict[int, list[dict[str, Any]]] = {
            101: [
                {
                    "id": 501,
                    "title": "Week 1 Discussion Forum",
                    "message": "<p>Post any questions about the assignment here.</p>",
                    "user_name": "Student A",
                    "last_reply_at": "2026-10-22T14:00:00Z",
                    "html_url": "https://canvas.instructure.com/courses/101/discussion_topics/501",
                }
            ]
        }

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self.valid_token}":
            return httpx.Response(401, json={"message": "Invalid access token"})

        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return httpx.Response(
                429,
                headers={"Retry-After": "0.1"},
                json={"message": "Rate limit exceeded (cost bucket)"},
            )

        if self.server_error_hits > 0:
            self.server_error_hits -= 1
            return httpx.Response(503, json={"message": "Temporary service unavailable"})

        path = request.url.path

        if path.endswith("/api/v1/courses"):
            return httpx.Response(200, json=self.courses)

        if "/pages" in path:
            return httpx.Response(200, json=self.pages.get(101, []))

        if "/modules" in path:
            return httpx.Response(200, json=self.modules.get(101, []))

        if "/assignments" in path:
            return httpx.Response(200, json=self.assignments.get(101, []))

        if "/discussion_topics" in path:
            if request.url.params.get("only_announcements") == "true":
                return httpx.Response(200, json=self.announcements.get(101, []))
            return httpx.Response(200, json=self.discussions.get(101, []))

        return httpx.Response(404, json={"message": f"Endpoint not found: {path}"})


@pytest.fixture
def fake_canvas():
    return FakeCanvasServer()


@pytest.fixture
def canvas_client(fake_canvas):
    transport = httpx.MockTransport(fake_canvas.handle_request)
    return CanvasClient(
        access_token="valid_canvas_token",
        base_url="https://canvas.instructure.com",
        transport=transport,
    )
