"""Shared test fakes: a session that replays recorded CircleCI API responses."""

import json
from pathlib import Path

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from cognee_community_connector_circleci.circleci import DEFAULT_BASE_URL

FIXTURES = Path(__file__).parent / "fixtures"


def _response(status: int, body: dict | None = None, headers: dict | None = None):
    """Build a real ``requests.Response`` so raise_for_status/json behave as in production."""
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body or {}).encode()
    response.headers = CaseInsensitiveDict(headers or {})
    return response


class FakeSession:
    """Stand-in for ``requests.Session`` that answers GETs from ``tests/fixtures/``.

    ``index`` names an index.json under fixtures/ that maps each request path to a
    recorded response file and status. Responses queued for a path with
    :meth:`queue` are served first, in order: that is how tests inject 429s, 5xx
    errors, network errors or extra pages. A path that is neither queued nor
    recorded raises KeyError, so a test fails loudly if the connector asks for
    something unexpected.
    """

    def __init__(self, index: str = "index.json"):
        self.routes = json.loads((FIXTURES / index).read_text())
        self.queued: dict[str, list] = {}
        self.calls: list[tuple[str, dict]] = []

    def queue(self, path: str, *responses) -> None:
        """Queue ``(status, body[, headers])`` tuples or exceptions for ``path``."""
        self.queued.setdefault(path, []).extend(responses)

    def get(self, url: str, params: dict | None = None, timeout: float | None = None):
        assert url.startswith(DEFAULT_BASE_URL), url
        path = url[len(DEFAULT_BASE_URL) :]
        self.calls.append((path, dict(params or {})))

        if self.queued.get(path):
            queued = self.queued[path].pop(0)
            if isinstance(queued, Exception):
                raise queued
            return _response(*queued)

        route = self.routes[path]
        return _response(route["status"], json.loads((FIXTURES / route["file"]).read_text()))


@pytest.fixture
def fake_session():
    return FakeSession()


@pytest.fixture
def session_for():
    """Factory for a FakeSession over another index, e.g. ``"slow-running/index.json"``."""
    return FakeSession


@pytest.fixture
def load_fixture():
    """Read a recorded response, e.g. ``load_fixture("main/pipeline.json")``."""
    return lambda rel: json.loads((FIXTURES / rel).read_text())


@pytest.fixture
def no_sleep(monkeypatch):
    """Record retry delays instead of sleeping."""
    delays: list[float] = []
    monkeypatch.setattr("cognee_community_connector_circleci.circleci.time.sleep", delays.append)
    return delays
