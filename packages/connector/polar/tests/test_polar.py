"""Comprehensive tests for Polar.sh data connector."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from cognee_community_connector_polar.polar import PolarClient, polar_source


class TestPolarConnector(unittest.TestCase):
    def test_missing_token_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                PolarClient(access_token=None)

    def test_sandbox_url(self):
        client = PolarClient(access_token="tok_123", sandbox=True)
        self.assertIn("sandbox-api.polar.sh", client.base_url)

        prod_client = PolarClient(access_token="tok_123", sandbox=False)
        self.assertIn("api.polar.sh", prod_client.base_url)
        self.assertNotIn("sandbox", prod_client.base_url)

    @patch.object(PolarClient, "_fetch_json")
    def test_get_paginated_customers(self, mock_fetch):
        mock_fetch.return_value = {
            "items": [
                {
                    "id": "cust_123",
                    "email": "alex@example.com",
                    "name": "Alex",
                    "created_at": "2026-10-31T00:00:00Z",
                    "billing_address": {"line1": "Secret Street 1"},  # should not be exposed
                }
            ],
            "pagination": {"max_page": 1},
        }

        client = PolarClient(access_token="test_token", organization_id="test_org")
        results = list(client.get_paginated("customers"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "cust_123")
        self.assertEqual(results[0]["email"], "alex@example.com")

    @patch.object(PolarClient, "_fetch_json")
    def test_source_resources(self, mock_fetch):
        def fake_fetch(url: str):
            if "customer-benefit-grants" in url:
                return {
                    "items": [
                        {
                            "id": "grant_999",
                            "customer_id": "cust_123",
                            "benefit_id": "ben_pro",
                            "is_granted": True,
                        }
                    ],
                    "pagination": {"max_page": 1},
                }
            elif "subscriptions" in url:
                return {
                    "items": [
                        {
                            "id": "sub_456",
                            "status": "active",
                            "product_id": "prod_1",
                            "cancel_at_period_end": False,
                        }
                    ],
                    "pagination": {"max_page": 1},
                }
            elif "products" in url:
                return {
                    "items": [
                        {
                            "id": "prod_1",
                            "name": "Pro Membership",
                            "description": "Unlimited access",
                        }
                    ],
                    "pagination": {"max_page": 1},
                }
            return {
                "items": [
                    {
                        "id": "cust_123",
                        "email": "alex@example.com",
                        "name": "Alex",
                    }
                ],
                "pagination": {"max_page": 1},
            }

        mock_fetch.side_effect = fake_fetch
        res = polar_source(access_token="test_token", organization_id="test_org")

        self.assertIn("subscriptions", res)
        self.assertIn("benefit_grants", res)
        self.assertIn("products", res)
        self.assertIn("customers", res)

        self.assertEqual(res["subscriptions"][0]["subscription_id"], "sub_456")
        self.assertEqual(res["benefit_grants"][0]["benefit_grant_id"], "grant_999")
        self.assertTrue(res["benefit_grants"][0]["is_granted"])


if __name__ == "__main__":
    unittest.main()
