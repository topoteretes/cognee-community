"""Tests for Polar.sh data connector."""
import unittest
from unittest.mock import MagicMock, patch
from cognee_community_connector_polar.polar import PolarClient, polar_source

class TestPolarConnector(unittest.TestCase):
    def test_missing_token_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                PolarClient(access_token=None)

    @patch.object(PolarClient, "_fetch_json")
    def test_get_paginated_customers(self, mock_fetch):
        mock_fetch.return_value = {
            "items": [
                {"id": "cust_123", "email": "alex@example.com", "name": "Alex", "created_at": "2026-10-31T00:00:00Z"}
            ],
            "pagination": {"max_page": 1}
        }

        client = PolarClient(access_token="test_token", organization_id="test_org")
        results = list(client.get_paginated("customers"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "cust_123")
        self.assertEqual(results[0]["email"], "alex@example.com")

    @patch.object(PolarClient, "_fetch_json")
    def test_source_resources(self, mock_fetch):
        mock_fetch.return_value = {
            "items": [
                {"id": "sub_456", "status": "active", "product_id": "prod_1"}
            ],
            "pagination": {"max_page": 1}
        }
        res = polar_source(access_token="test_token", organization_id="test_org")
        self.assertIn("subscriptions", res)
        self.assertEqual(len(res["subscriptions"]), 1)
        self.assertEqual(res["subscriptions"][0]["subscription_id"], "sub_456")

if __name__ == "__main__":
    unittest.main()
