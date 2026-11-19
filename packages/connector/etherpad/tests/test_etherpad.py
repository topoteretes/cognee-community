"""Hermetic unit tests for the Etherpad dlt connector.

Runs in CI without requiring an active Etherpad instance or credentials.
Supports both pytest and standard unittest runner.
"""

from unittest.mock import MagicMock
import unittest

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from cognee_community_connector_etherpad.etherpad import (
    EtherpadClient,
    _format_timestamp,
    _render_pad_markdown,
    etherpad_source,
)


class TestEtherpadConnector(unittest.TestCase):
    def test_missing_credentials_raises(self):
        with self.assertRaises(ValueError):
            etherpad_source(api_key=None, bearer_token=None, base_url="http://test")

    def test_format_timestamp(self):
        self.assertEqual(_format_timestamp(None), "Unknown")
        formatted = _format_timestamp(1700000000000)
        self.assertIn("2023-11-14", formatted)

    def test_render_pad_markdown(self):
        text = "Weekly engineering sync discussion."
        chat = [
            {"userName": "Alice", "time": 1700000000000, "text": "Ready to begin."},
            {"userName": "Bob", "time": 1700000060000, "text": "All services green."},
        ]
        markdown = _render_pad_markdown(
            pad_id="sync-2026",
            text=text,
            last_edited_ms=1700000060000,
            read_only_url="http://localhost:9001/p/r.ro123",
            chat_history=chat,
        )

        self.assertIn("# Etherpad: sync-2026", markdown)
        self.assertIn("r.ro123", markdown)
        self.assertIn("Weekly engineering sync discussion.", markdown)
        self.assertIn("## Chat History", markdown)
        self.assertIn("Alice", markdown)
        self.assertIn("All services green.", markdown)

    def test_client_api_error_raises(self):
        session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 1, "message": "padID does not exist", "data": None}
        session.get.return_value = mock_resp

        client = EtherpadClient(base_url="http://localhost:9001", api_key="secret", session=session)
        with self.assertRaises(RuntimeError):
            client.request("getText", padID="non_existent")

    def test_etherpad_source_attributes(self):
        client = MagicMock()
        source = etherpad_source(client=client)
        self.assertEqual(getattr(source, DOCUMENT_SOURCE_ATTR), "etherpad")

    def test_fetch_pads_yields_documents(self):
        client = MagicMock()
        client.base_url = "http://localhost:9001"
        client.request.side_effect = lambda fn, **kwargs: {
            "listAllPads": {"padIDs": ["notes-1", "notes-2"]},
            "getText": {"text": f"Content for {kwargs.get('padID')}"},
            "getLastEdited": {"lastEdited": 1700000000000},
            "getReadOnlyID": {"readOnlyID": f"ro-{kwargs.get('padID')}"},
            "getChatHistory": {"messages": []},
        }.get(fn, {})

        source = etherpad_source(client=client, include_chat=False)
        resource = list(source.selected_resources.values())[0]
        records = list(resource())

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["id"], "notes-1")
        self.assertEqual(records[0]["title"], "Etherpad: notes-1")
        self.assertIn("ro-notes-1", records[0]["url"])
        self.assertIn("Content for notes-1", records[0]["content"])


if __name__ == "__main__":
    unittest.main()
