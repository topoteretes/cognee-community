"""Hermetic unit tests for the Zoho Cliq dlt connector.

Runs in CI without requiring live Zoho credentials or endpoints.
Supports both pytest and standard unittest runner.
"""

from unittest.mock import MagicMock
import unittest

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from cognee_community_connector_zoho_cliq.zoho_cliq import (
    ZohoCliqClient,
    _format_time,
    _render_message_markdown,
    zoho_cliq_source,
)


class TestZohoCliqConnector(unittest.TestCase):
    def test_missing_credentials_raises(self):
        with self.assertRaises(ValueError):
            zoho_cliq_source(auth_token=None)

    def test_format_time(self):
        self.assertEqual(_format_time(None), "Unknown")
        formatted = _format_time(1700000000000)
        self.assertIn("2023-11-14", formatted)

    def test_render_message_markdown(self):
        msg = {
            "id": "msg_001",
            "sender": {"name": "Alice"},
            "time": 1700000000000,
            "content": {"text": "Release candidate v2.0 is deployed to staging."},
        }
        replies = [
            {
                "id": "rep_001",
                "sender": {"name": "Bob"},
                "time": 1700000060000,
                "content": {"text": "All smoke tests passing."},
            }
        ]

        markdown = _render_message_markdown("engineering", msg, replies)
        self.assertIn("# Zoho Cliq: #engineering - Message by Alice", markdown)
        self.assertIn("msg_001", markdown)
        self.assertIn("Release candidate v2.0", markdown)
        self.assertIn("## Thread Replies", markdown)
        self.assertIn("Bob", markdown)
        self.assertIn("All smoke tests passing.", markdown)

    def test_client_authorization_header(self):
        session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"channels": []}
        mock_resp.content = b'{"channels": []}'
        session.request.return_value = mock_resp

        client = ZohoCliqClient(auth_token="my_zoho_token", domain="cliq.zoho.com", session=session)
        client.request("GET", "/channels")

        session.request.assert_called_once()
        args, kwargs = session.request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Zoho-oauthtoken my_zoho_token")

    def test_zoho_cliq_source_attributes(self):
        client = MagicMock()
        source = zoho_cliq_source(client=client)
        self.assertEqual(getattr(source, DOCUMENT_SOURCE_ATTR), "zoho_cliq")

    def test_fetch_messages_yields_documents(self):
        client = MagicMock()
        client.domain = "cliq.zoho.com"

        def mock_request(method, endpoint, **kwargs):
            if endpoint == "/channels":
                return [{"channel_id": "c100", "name": "announcements"}]
            if endpoint == "/chats/c100/messages":
                return [
                    {
                        "id": "m1",
                        "sender": "Alice",
                        "time": 1700000000000,
                        "content": {"text": "Company offsite next Friday!"},
                    }
                ]
            return {}

        client.request.side_effect = mock_request

        source = zoho_cliq_source(client=client, include_threads=False)
        resource = list(source.selected_resources.values())[0]
        records = list(resource())

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "c100_m1")
        self.assertEqual(records[0]["title"], "Zoho Cliq: #announcements message m1")
        self.assertIn("announcements", records[0]["content"])
        self.assertIn("Company offsite next Friday!", records[0]["content"])


if __name__ == "__main__":
    unittest.main()
