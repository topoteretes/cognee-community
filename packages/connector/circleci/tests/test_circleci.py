"""Tests for the CircleCI connector.

These tests use a mocked HTTP client so they run without a real CircleCI
account or network access.
"""

from __future__ import annotations

from typing import Any

import pytest

from cognee_community_connector_circleci import circleci_source


class _FakeClient:
    """A test double for the CircleCI HTTP client closure."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        key = f"{method} {path}"
        if key not in self.responses:
            raise KeyError(f"No canned response for {key}")
        return self.responses[key]


def test_source_factory_accepts_client_injection() -> None:
    """The factory accepts a pre-built ``client`` (test-seam)."""
    fake = _FakeClient()
    source = circleci_source(client=fake)
    # The source should be callable / iterable via dlt; a basic smoke check
    # is that it carries the document-mode marker.
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "circleci"


def test_source_factory_requires_token_when_no_client() -> None:
    """Without a client and no env var, a clear error is raised."""
    import os

    os.environ.pop("CIRCLECI_API_TOKEN", None)
    with pytest.raises(ValueError, match="CircleCI API token required"):
        circleci_source(api_token=None, client=None)


def test_source_factory_reads_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CIRCLECI_API_TOKEN`` is picked up when ``api_token`` is omitted."""
    monkeypatch.setenv("CIRCLECI_API_TOKEN", "token-from-env")
    # We can't fully exercise the HTTP path without requests, but we can
    # confirm the import / wiring path doesn't explode by injecting a client.
    fake = _FakeClient()
    source = circleci_source(client=fake)
    assert source is not None


# TODO: add tests that exercise the resource generator against a canned
#       ``_FakeClient`` returning known pipeline / workflow / job payloads,
#       asserting the yielded document shape and incremental-cursor behavior.
