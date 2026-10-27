import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from firecrawl.v2.types import (
    Document,
    DocumentMetadata,
    SearchData,
    SearchRequest,
    SearchResultWeb,
)
from firecrawl.v2.utils.error_handler import UnauthorizedError

from cognee_community_tasks_firecrawl import scrape_urls, search_web
from cognee_community_tasks_firecrawl.firecrawl_task import (
    FirecrawlDocument,
    _parse_document,
    scrape_and_add,
    search_and_add,
)

CLIENT_PATH = "cognee_community_tasks_firecrawl.firecrawl_task.AsyncFirecrawl"
COGNEE_PATH = "cognee_community_tasks_firecrawl.firecrawl_task.cognee"


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-api-key")


def _document(markdown="# Page", title="Page", status_code=200, **metadata):
    """Build a real firecrawl-py v2 Document, as returned by scrape and search."""
    return Document(
        markdown=markdown,
        metadata=DocumentMetadata(title=title, status_code=status_code, **metadata),
    )


def _fake_client(scrape=None, search=None):
    inst = MagicMock()
    inst.scrape = scrape or AsyncMock(return_value=_document())
    inst.search = search or AsyncMock(return_value=SearchData(web=[]))
    return inst


def _mock_cognee():
    mock_cognee = MagicMock()
    mock_cognee.add = AsyncMock()
    mock_cognee.cognify = AsyncMock(return_value="graph_result")
    return mock_cognee


class TestClient:
    def test_scrape_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        with pytest.raises(ValueError, match="FIRECRAWL_API_KEY"):
            asyncio.run(scrape_urls(["https://example.com"]))

    def test_search_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        with pytest.raises(ValueError, match="FIRECRAWL_API_KEY"):
            asyncio.run(search_web("anything"))

    def test_explicit_api_key_is_used(self):
        inst = _fake_client()
        with patch(CLIENT_PATH, return_value=inst) as mock_cls:
            asyncio.run(scrape_urls(["https://example.com"], api_key="explicit-key"))
        mock_cls.assert_called_once_with(api_key="explicit-key")


class TestScrapeUrls:
    def test_maps_documents_in_input_order(self):
        async def fake_scrape(url, **kwargs):
            if url.endswith("a"):
                # Finish the first URL last to check that input order is kept.
                await asyncio.sleep(0.01)
            return _document(markdown=f"body of {url}", title=url[-1].upper())

        inst = _fake_client(scrape=AsyncMock(side_effect=fake_scrape))
        with patch(CLIENT_PATH, return_value=inst):
            out = asyncio.run(scrape_urls(["https://x.example/a", "https://x.example/b"]))

        assert [r["url"] for r in out] == ["https://x.example/a", "https://x.example/b"]
        assert out[0]["title"] == "A"
        assert out[0]["content"] == "body of https://x.example/a"
        assert out[0]["status_code"] == 200
        assert out[0]["error"] is None

    def test_passes_scrape_options(self):
        inst = _fake_client()
        with patch(CLIENT_PATH, return_value=inst):
            asyncio.run(
                scrape_urls(["https://example.com"], only_main_content=False, timeout_ms=5000)
            )
        inst.scrape.assert_awaited_once_with(
            "https://example.com",
            formats=["markdown"],
            only_main_content=False,
            timeout=5000,
        )

    def test_failed_url_does_not_abort_batch(self):
        async def fake_scrape(url, **kwargs):
            if "bad" in url:
                raise RuntimeError("boom")
            return _document()

        inst = _fake_client(scrape=AsyncMock(side_effect=fake_scrape))
        with patch(CLIENT_PATH, return_value=inst):
            out = asyncio.run(scrape_urls(["https://bad.example", "https://good.example"]))

        assert out[0] == {
            "url": "https://bad.example",
            "title": None,
            "content": "",
            "description": None,
            "status_code": None,
            "error": "boom",
        }
        assert out[1]["content"] == "# Page"

    def test_account_errors_are_raised(self):
        inst = _fake_client(scrape=AsyncMock(side_effect=UnauthorizedError("Invalid token")))
        with patch(CLIENT_PATH, return_value=inst), pytest.raises(UnauthorizedError):
            asyncio.run(scrape_urls(["https://example.com"]))

    def test_rejects_invalid_concurrency(self):
        with pytest.raises(ValueError, match="concurrency"):
            asyncio.run(scrape_urls(["https://example.com"], concurrency=0))

    def test_honours_concurrency(self):
        active = 0
        peak = 0

        async def fake_scrape(url, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _document()

        inst = _fake_client(scrape=AsyncMock(side_effect=fake_scrape))
        urls = [f"https://x.example/{i}" for i in range(6)]
        with patch(CLIENT_PATH, return_value=inst):
            asyncio.run(scrape_urls(urls, concurrency=2))

        assert peak == 2


class TestParseDocument:
    def test_parses_dict_input(self):
        doc = _parse_document(
            {
                "markdown": "text",
                "metadata": {
                    "title": "X",
                    "sourceURL": "https://x.example",
                    "statusCode": 404,
                    "error": "Not Found",
                },
            }
        )
        assert isinstance(doc, FirecrawlDocument)
        assert doc.url == "https://x.example"
        assert doc.title == "X"
        assert doc.status_code == 404
        assert doc.error == "Not Found"

    def test_parses_search_result_without_markdown(self):
        doc = _parse_document(
            SearchResultWeb(url="https://y.example", title="Y", description="snippet")
        )
        assert doc.url == "https://y.example"
        assert doc.description == "snippet"
        assert doc.content == ""


class TestScrapeAndAdd:
    def test_raises_when_nothing_has_content(self):
        inst = _fake_client(scrape=AsyncMock(return_value=_document(markdown=None)))
        with (
            patch(CLIENT_PATH, return_value=inst),
            patch(COGNEE_PATH, _mock_cognee()),
            pytest.raises(RuntimeError, match="No scraped pages returned any content"),
        ):
            asyncio.run(scrape_and_add(["https://example.com"]))

    def test_calls_cognee_add_and_cognify(self):
        inst = _fake_client(scrape=AsyncMock(return_value=_document(markdown="body", title="T")))
        mock_cognee = _mock_cognee()
        with patch(CLIENT_PATH, return_value=inst), patch(COGNEE_PATH, mock_cognee):
            result = asyncio.run(
                scrape_and_add(["https://example.com"], dataset_name="test_dataset")
            )

        mock_cognee.add.assert_awaited_once_with(
            "Source: https://example.com\nTitle: T\nbody", dataset_name="test_dataset"
        )
        mock_cognee.cognify.assert_awaited_once_with(datasets=["test_dataset"])
        assert result == "graph_result"

    def test_ingests_good_pages_and_skips_failures_and_error_pages(self):
        async def fake_scrape(url, **kwargs):
            if "bad" in url:
                raise RuntimeError("boom")
            if "missing" in url:
                return _document(markdown="# 404 Not Found", status_code=404, error="Not Found")
            return _document(markdown="good body", title="Good")

        inst = _fake_client(scrape=AsyncMock(side_effect=fake_scrape))
        mock_cognee = _mock_cognee()
        urls = ["https://bad.example", "https://missing.example", "https://good.example"]
        with patch(CLIENT_PATH, return_value=inst), patch(COGNEE_PATH, mock_cognee):
            asyncio.run(scrape_and_add(urls, dataset_name="mixed"))

        mock_cognee.add.assert_awaited_once_with(
            "Source: https://good.example\nTitle: Good\ngood body", dataset_name="mixed"
        )


class TestSearch:
    def test_search_web_request_is_valid_for_the_sdk(self):
        inst = _fake_client()
        with patch(CLIENT_PATH, return_value=inst):
            asyncio.run(search_web("hello world", limit=3))

        args, kwargs = inst.search.call_args
        assert args == ("hello world",)
        assert kwargs == {
            "limit": 3,
            "scrape_options": {"formats": ["markdown"], "only_main_content": True},
        }
        request = SearchRequest(query=args[0], **kwargs)
        assert request.scrape_options.formats == ["markdown"]

    def test_search_web_maps_results(self):
        web = [
            _document(markdown="page", title="One", url="https://one.example", description="d1"),
            SearchResultWeb(url="https://two.example", title="Two", description="d2"),
        ]
        inst = _fake_client(search=AsyncMock(return_value=SearchData(web=web)))
        with patch(CLIENT_PATH, return_value=inst):
            out = asyncio.run(search_web("q"))

        assert out[0]["url"] == "https://one.example"
        assert out[0]["content"] == "page"
        assert out[0]["description"] == "d1"
        assert out[1] == {
            "url": "https://two.example",
            "title": "Two",
            "content": "",
            "description": "d2",
            "status_code": None,
            "error": None,
        }

    def test_search_web_without_scrape(self):
        inst = _fake_client()
        with patch(CLIENT_PATH, return_value=inst):
            asyncio.run(search_web("q", scrape=False))
        assert "scrape_options" not in inst.search.call_args.kwargs

    def test_search_and_add_skips_results_without_markdown(self):
        web = [
            _document(markdown="page one", title="One", url="https://one.example"),
            SearchResultWeb(url="https://two.example", title="Two"),
        ]
        inst = _fake_client(search=AsyncMock(return_value=SearchData(web=web)))
        mock_cognee = _mock_cognee()
        with patch(CLIENT_PATH, return_value=inst), patch(COGNEE_PATH, mock_cognee):
            result = asyncio.run(search_and_add("q", dataset_name="search_dataset"))

        mock_cognee.add.assert_awaited_once_with(
            "Source: https://one.example\nTitle: One\npage one", dataset_name="search_dataset"
        )
        mock_cognee.cognify.assert_awaited_once_with(datasets=["search_dataset"])
        assert result == "graph_result"

    def test_search_and_add_raises_when_nothing_has_content(self):
        web = [SearchResultWeb(url="https://two.example")]
        inst = _fake_client(search=AsyncMock(return_value=SearchData(web=web)))
        with (
            patch(CLIENT_PATH, return_value=inst),
            patch(COGNEE_PATH, _mock_cognee()),
            pytest.raises(RuntimeError, match="No Firecrawl search results"),
        ):
            asyncio.run(search_and_add("q"))
