import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cognee_community_tasks_keenable import search_web
from cognee_community_tasks_keenable.keenable_task import (
    APP_TITLE,
    KEYED_SEARCH_URL,
    PUBLIC_SEARCH_URL,
    KeenableSearchResult,
    _parse_result,
    search_and_add,
)


def _http_response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error",
            request=MagicMock(),
            response=response,
        )
    else:
        response.raise_for_status.return_value = None
    return response


def _client_cm(response):
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm, client


class TestParseResult:
    def test_reads_url_title_and_snippet(self):
        parsed = _parse_result(
            {"url": "https://example.com", "title": "Example", "snippet": "Hello"}
        )
        assert parsed == KeenableSearchResult(
            url="https://example.com", title="Example", snippet="Hello"
        )
        assert parsed.to_dict()["content"] == "Hello"

    def test_skips_entries_without_a_url(self):
        assert _parse_result({"title": "Missing"}) is None
        assert _parse_result("not-an-object") is None


class TestSearchWeb:
    def test_uses_the_public_endpoint_and_title_header_without_a_key(self, monkeypatch):
        monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
        response = _http_response(
            {
                "results": [
                    {"url": "https://a.example", "title": "A", "snippet": "body A"},
                    {"url": "https://b.example", "title": "B", "snippet": "body B"},
                ]
            }
        )
        cm, client = _client_cm(response)

        with patch("cognee_community_tasks_keenable.keenable_task.httpx.AsyncClient", return_value=cm):
            out = asyncio.run(search_web("hello world", num_results=2))

        assert len(out) == 2
        assert out[0]["url"] == "https://a.example"
        assert out[0]["title"] == "A"
        assert out[0]["content"] == "body A"
        client.post.assert_awaited_once()
        args, kwargs = client.post.call_args
        assert args[0] == PUBLIC_SEARCH_URL
        assert kwargs["json"] == {"query": "hello world", "max_results": 2}
        assert kwargs["headers"]["X-Keenable-Title"] == APP_TITLE
        assert "X-API-Key" not in kwargs["headers"]

    def test_uses_the_keyed_endpoint_when_an_api_key_is_set(self, monkeypatch):
        monkeypatch.setenv("KEENABLE_API_KEY", "test-key")
        response = _http_response({"results": [{"url": "https://a.example", "title": "A"}]})
        cm, client = _client_cm(response)

        with patch("cognee_community_tasks_keenable.keenable_task.httpx.AsyncClient", return_value=cm):
            asyncio.run(search_web("q"))

        args, kwargs = client.post.call_args
        assert args[0] == KEYED_SEARCH_URL
        assert kwargs["headers"]["X-API-Key"] == "test-key"
        assert kwargs["headers"]["X-Keenable-Title"] == APP_TITLE

    def test_raises_on_http_errors(self, monkeypatch):
        monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
        response = _http_response({"error": "nope"}, status_code=400)
        cm, _ = _client_cm(response)

        with patch("cognee_community_tasks_keenable.keenable_task.httpx.AsyncClient", return_value=cm):
            with pytest.raises(httpx.HTTPStatusError):
                asyncio.run(search_web("q"))


class TestSearchAndAdd:
    def test_ingests_combined_result_text(self, monkeypatch):
        monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
        results = [
            {
                "url": "https://a.example",
                "title": "A",
                "content": "body A",
                "snippet": "body A",
            }
        ]

        with (
            patch(
                "cognee_community_tasks_keenable.keenable_task.search_web",
                new=AsyncMock(return_value=results),
            ),
            patch(
                "cognee_community_tasks_keenable.keenable_task.cognee.add",
                new=AsyncMock(),
            ) as add,
            patch(
                "cognee_community_tasks_keenable.keenable_task.cognee.cognify",
                new=AsyncMock(return_value="ok"),
            ) as cognify,
        ):
            out = asyncio.run(search_and_add("hello", dataset_name="keenable_search"))

        assert out == "ok"
        add.assert_awaited_once()
        document = add.await_args.args[0]
        assert "https://a.example" in document
        assert "body A" in document
        assert add.await_args.kwargs["dataset_name"] == "keenable_search"
        cognify.assert_awaited_once()

    def test_raises_when_no_content_is_returned(self):
        with patch(
            "cognee_community_tasks_keenable.keenable_task.search_web",
            new=AsyncMock(return_value=[{"url": "https://a.example", "content": ""}]),
        ):
            with pytest.raises(RuntimeError, match="No Keenable results"):
                asyncio.run(search_and_add("hello"))
