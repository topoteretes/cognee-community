import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognee_community_tasks_scrapegraph import scrape_urls
from cognee_community_tasks_scrapegraph.scrapegraph_task import scrape_and_add


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("SGAI_API_KEY", "test-api-key")


class TestScrapeUrls:
    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("SGAI_API_KEY", raising=False)

        with pytest.raises(ValueError, match="SGAI_API_KEY"):
            asyncio.run(scrape_urls(["https://example.com"], api_key=None))

    def test_returns_results_for_each_url(self):
        mock_client = MagicMock()

        mock_response = MagicMock()
        mock_response.status = "success"
        mock_response.data.json_data = {
            "title": "Example",
            "content": "Text",
        }

        mock_client.extract.return_value = mock_response

        with patch(
            "cognee_community_tasks_scrapegraph.scrapegraph_task.ScrapeGraphAI",
            return_value=mock_client,
        ):
            results = asyncio.run(
                scrape_urls(
                    urls=["https://example.com", "https://example.org"],
                    user_prompt="Extract title and content",
                )
            )

        assert len(results) == 2
        assert results[0]["url"] == "https://example.com"
        assert results[1]["url"] == "https://example.org"
        assert results[0]["content"] == {
            "title": "Example",
            "content": "Text",
        }

    def test_handles_per_url_error_gracefully(self):
        mock_client = MagicMock()
        mock_client.extract.side_effect = Exception("Network error")

        with patch(
            "cognee_community_tasks_scrapegraph.scrapegraph_task.ScrapeGraphAI",
            return_value=mock_client,
        ):
            results = asyncio.run(
                scrape_urls(
                    urls=["https://bad-url.invalid"],
                )
            )

        assert len(results) == 1
        assert results[0]["url"] == "https://bad-url.invalid"
        assert results[0]["content"] == ""
        assert "error" in results[0]

    def test_explicit_api_key_is_used(self):
        mock_client = MagicMock()

        mock_response = MagicMock()
        mock_response.status = "success"
        mock_response.data.json_data = "ok"

        mock_client.extract.return_value = mock_response

        with patch(
            "cognee_community_tasks_scrapegraph.scrapegraph_task.ScrapeGraphAI",
            return_value=mock_client,
        ) as mock_cls:
            asyncio.run(
                scrape_urls(
                    ["https://example.com"],
                    api_key="explicit-key",
                )
            )

        mock_cls.assert_called_once_with(api_key="explicit-key")


class TestScrapeAndAdd:
    def test_raises_when_all_urls_fail(self):
        mock_client = MagicMock()
        mock_client.extract.side_effect = Exception("Network error")

        with (
            patch(
                "cognee_community_tasks_scrapegraph.scrapegraph_task.ScrapeGraphAI",
                return_value=mock_client,
            ),
            pytest.raises(RuntimeError, match="No URLs were scraped successfully"),
        ):
            asyncio.run(
                scrape_and_add(
                    urls=["https://bad-url.invalid"],
                )
            )

    def test_calls_cognee_add_and_cognify(self):
        mock_client = MagicMock()

        mock_response = MagicMock()
        mock_response.status = "success"
        mock_response.data.json_data = "scraped content"

        mock_client.extract.return_value = mock_response

        mock_cognee = MagicMock()
        mock_cognee.add = AsyncMock()
        mock_cognee.cognify = AsyncMock(return_value="graph_result")

        with (
            patch(
                "cognee_community_tasks_scrapegraph.scrapegraph_task.ScrapeGraphAI",
                return_value=mock_client,
            ),
            patch(
                "cognee_community_tasks_scrapegraph.scrapegraph_task.cognee",
                mock_cognee,
            ),
        ):
            result = asyncio.run(
                scrape_and_add(
                    urls=["https://example.com"],
                    dataset_name="test_dataset",
                )
            )

        mock_cognee.add.assert_called_once()

        call_kwargs = mock_cognee.add.call_args
        assert "test_dataset" in str(call_kwargs)

        mock_cognee.cognify.assert_called_once()
        assert result == "graph_result"
