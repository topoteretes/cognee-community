"""Hermetic unit tests for the Mastra.ai dlt connector.

Runs in CI without requiring an active Mastra server.
Supports both pytest and standard unittest runner.
"""

from unittest.mock import MagicMock
import unittest

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from cognee_community_connector_mastra.mastra import (
    MastraClient,
    _format_time,
    _render_thread_markdown,
    mastra_source,
)


class TestMastraConnector(unittest.TestCase):
    def test_format_time(self):
        self.assertEqual(_format_time(None), "Unknown")
        formatted = _format_time(1700000000000)
        self.assertIn("2023-11-14", formatted)

    def test_render_thread_markdown(self):
        thread = {
            "id": "th_123",
            "title": "Onboarding Help",
            "resourceId": "onboarding_agent",
            "createdAt": 1700000000000,
            "updatedAt": 1700000060000,
        }
        messages = [
            {
                "role": "user",
                "content": "How do I create a new API token?",
                "createdAt": 1700000000000,
            },
            {
                "role": "assistant",
                "content": "Navigate to settings and click Generate Token.",
                "createdAt": 1700000030000,
                "toolCalls": [
                    {
                        "function": {"name": "query_docs", "arguments": '{"topic": "tokens"}'}
                    }
                ],
            },
        ]

        markdown = _render_thread_markdown(thread, messages, include_tools=True)
        self.assertIn("# Mastra Conversation: Onboarding Help", markdown)
        self.assertIn("th_123", markdown)
        self.assertIn("USER", markdown)
        self.assertIn("ASSISTANT", markdown)
        self.assertIn("query_docs", markdown)

    def test_client_authorization_header(self):
        session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"id": "t1"}]
        mock_resp.content = b'[{"id": "t1"}]'
        session.request.return_value = mock_resp

        client = MastraClient(api_url="http://localhost:4111", api_key="secret_token", session=session)
        client.request("GET", "/api/memory/threads")

        session.request.assert_called_once()
        args, kwargs = session.request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret_token")

    def test_mastra_source_attributes(self):
        client = MagicMock()
        source = mastra_source(client=client)
        self.assertEqual(getattr(source, DOCUMENT_SOURCE_ATTR), "mastra")

    def test_fetch_threads_yields_documents(self):
        client = MagicMock()
        client.api_url = "http://localhost:4111"

        def mock_request(method, path, **kwargs):
            if path == "/api/memory/threads":
                return [{"id": "t1", "title": "Customer Inquiry"}]
            if path == "/api/memory/threads/t1/messages":
                return [
                    {"role": "user", "content": "Hello", "createdAt": 1700000000000},
                    {"role": "assistant", "content": "Hi there!", "createdAt": 1700000010000},
                ]
            return {}

        client.request.side_effect = mock_request

        source = mastra_source(client=client)
        resource = list(source.selected_resources.values())[0]
        records = list(resource())

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "t1")
        self.assertEqual(records[0]["title"], "Customer Inquiry")
        self.assertIn("agents/threads/t1", records[0]["url"])
        self.assertIn("Hi there!", records[0]["content"])


if __name__ == "__main__":
    unittest.main()
