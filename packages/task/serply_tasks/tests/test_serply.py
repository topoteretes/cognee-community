import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from cognee_community_tasks_serply import search_web
from cognee_community_tasks_serply.serply_task import _build_headers, search_and_add

CALL_PATH = "cognee_community_tasks_serply.serply_task._call"
COGNEE_PATH = "cognee_community_tasks_serply.serply_task.cognee"


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("SERPLY_API_KEY", "test-api-key")


def _result(link="https://example.com", title="Example", description="snippet", **extra):
    """Build one entry of Serply's ``results`` array."""
    return {
        "title": title,
        "description": description,
        "position": 1,
        "result_type": "organic",
        "metadata": {"display_url": "example.com", **extra},
        "link": link,
    }


def _http_error(status):
    return aiohttp.ClientResponseError(MagicMock(), (), status=status, message="error")


def _fake_call(results, pages=None):
    """Return search ``results`` for GET /search and ``pages[url]`` for POST /request."""

    async def fake(session, method, path, params=None, payload=None):
        if path == "/search":
            return {"results": results}
        page = (pages or {})[payload["url"]]
        if isinstance(page, Exception):
            raise page
        return page

    return AsyncMock(side_effect=fake)


def _mock_cognee():
    mock_cognee = MagicMock()
    mock_cognee.add = AsyncMock()
    mock_cognee.cognify = AsyncMock(return_value="graph_result")
    return mock_cognee


class TestSearchWeb:
    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("SERPLY_API_KEY", raising=False)
        with pytest.raises(ValueError, match="SERPLY_API_KEY"):
            asyncio.run(search_web("anything"))

    def test_headers_carry_key_and_user_agent(self):
        assert _build_headers("explicit-key") == {
            "X-Api-Key": "explicit-key",
            "User-Agent": "cognee-community",
        }

    def test_rejects_out_of_range_num_results(self):
        with pytest.raises(ValueError, match="num_results"):
            asyncio.run(search_web("q", num_results=11))

    def test_returns_parsed_results(self):
        call = _fake_call(
            [
                _result(link="https://a.example", title="A", description="about A"),
                _result(link="https://b.example", title="B", published_time="2 days ago"),
            ]
        )
        with patch(CALL_PATH, call):
            out = asyncio.run(search_web("hello world", num_results=2))

        assert out[0] == {
            "url": "https://a.example",
            "title": "A",
            "content": "about A",
            "description": "about A",
            "position": 1,
            "published": None,
            "error": None,
        }
        assert out[1]["published"] == "2 days ago"
        assert call.call_args.kwargs["params"] == {"q": "hello world", "num": 2}

    def test_drops_non_url_links_and_trims_to_num_results(self):
        results = [_result(link="CAESfQHrOzAV")] + [
            _result(link=f"https://{i}.example") for i in range(4)
        ]
        with patch(CALL_PATH, _fake_call(results)):
            out = asyncio.run(search_web("q", num_results=3))

        assert [r["url"] for r in out] == [
            "https://0.example",
            "https://1.example",
            "https://2.example",
        ]

    def test_passes_filters(self):
        call = _fake_call([])
        with patch(CALL_PATH, call):
            asyncio.run(search_web("q", search_type="news", country="GB", time_range="week"))
        assert call.call_args.kwargs["params"] == {
            "q": "q",
            "num": 10,
            "tbm": "nws",
            "gl": "gb",
            "tbs": "qdr:w",
        }


class TestIncludeContent:
    def test_fetches_markdown_and_reports_failed_pages(self):
        call = _fake_call(
            [_result(link="https://good.example"), _result(link="https://bad.example")],
            pages={"https://good.example": "# Good", "https://bad.example": _http_error(500)},
        )
        with patch(CALL_PATH, call):
            out = asyncio.run(search_web("q", include_content=True))

        assert out[0]["content"] == "# Good"
        assert out[0]["error"] is None
        assert out[1]["content"] == "snippet"
        assert "500" in out[1]["error"]
        fetch = call.call_args_list[1]
        assert fetch.args[1:3] == ("POST", "/request")
        assert fetch.kwargs["payload"]["response_type"] == "markdown"

    def test_account_errors_are_raised(self):
        call = _fake_call(
            [_result(link="https://example.com")],
            pages={"https://example.com": _http_error(402)},
        )
        with patch(CALL_PATH, call), pytest.raises(aiohttp.ClientResponseError):
            asyncio.run(search_web("q", include_content=True))


class TestSearchAndAdd:
    def test_raises_when_no_results_have_content(self):
        call = _fake_call(
            [_result(description=None)],
            pages={"https://example.com": ""},
        )
        with (
            patch(CALL_PATH, call),
            pytest.raises(RuntimeError, match="No Serply results"),
        ):
            asyncio.run(search_and_add(query="q"))

    def test_calls_cognee_add_and_cognify(self):
        call = _fake_call(
            [_result(link="https://a.example", title="A")],
            pages={"https://a.example": "# Page A"},
        )
        mock_cognee = _mock_cognee()
        with patch(CALL_PATH, call), patch(COGNEE_PATH, mock_cognee):
            result = asyncio.run(search_and_add(query="q", dataset_name="test_dataset"))

        mock_cognee.add.assert_awaited_once_with(
            "Source: https://a.example\nTitle: A\n# Page A", dataset_name="test_dataset"
        )
        mock_cognee.cognify.assert_awaited_once_with(datasets=["test_dataset"])
        assert result == "graph_result"
