"""Hermetic unit tests for the Cognee Packagist connector."""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Ensure connector package is importable
PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from cognee_community_connector_packagist import (
    PackagistClient,
    packagist_source,
    format_package_document,
    format_tombstone,
)


class TestPackagistConnector(unittest.TestCase):

    def setUp(self):
        self.mock_package_info = {
            "package": {
                "name": "monolog/monolog",
                "description": "Sends your logs to files, sockets, inboxes, databases and various web services",
                "repository": "https://github.com/Seldaek/monolog",
                "maintainers": [{"name": "Seldaek"}, {"name": "alcohol"}],
                "downloads": {"total": 500000000, "monthly": 20000000},
                "abandoned": False,
                "versions": {
                    "3.8.0": {
                        "version": "3.8.0",
                        "time": "2024-11-01T12:00:00+00:00",
                        "require": {
                            "php": ">=8.1.0",
                            "psr/log": "^3.0"
                        },
                        "suggest": {
                            "graylog2/gelf-php": "Allow sending log messages to a GrayLog2 server"
                        }
                    }
                }
            }
        }

        self.mock_abandoned_package = {
            "package": {
                "name": "swiftmailer/swiftmailer",
                "description": "Free feature-rich PHP mailer",
                "repository": "https://github.com/swiftmailer/swiftmailer",
                "maintainers": [{"name": "fabpot"}],
                "downloads": {"total": 150000000, "monthly": 500000},
                "abandoned": "symfony/mailer",
                "versions": {
                    "v6.3.0": {
                        "version": "v6.3.0",
                        "time": "2021-10-18T15:00:00+00:00",
                        "require": {"php": ">=7.0.0"}
                    }
                }
            }
        }

        self.mock_advisories = [
            {
                "advisoryId": "PKSA-dmw8-jd8k-q3c6",
                "packageName": "monolog/monolog",
                "title": "Header Injection in NativeMailerHandler",
                "cve": "CVE-2022-24816",
                "link": "https://github.com/Seldaek/monolog/security/advisories/GHSA-7667-55hp-m7mg",
                "affectedVersions": "<2.3.6|>=3.0.0,<3.0.1"
            }
        ]

    def test_format_package_document_normal(self):
        doc = format_package_document(self.mock_package_info, advisories=[])
        self.assertEqual(doc["name"], "monolog/monolog")
        self.assertEqual(doc["vendor"], "monolog")
        self.assertEqual(doc["package_name"], "monolog")
        self.assertFalse(doc["abandoned"])
        self.assertIsNone(doc["replacement_package"])
        self.assertEqual(doc["latest_version"], "3.8.0")
        self.assertEqual(doc["total_downloads"], 500000000)
        self.assertEqual(doc["advisory_count"], 0)
        self.assertFalse(doc["_deleted"])
        self.assertIn("Composer Package: monolog/monolog", doc["text"])
        self.assertIn("**Total Downloads:** 500,000,000", doc["text"])

    def test_format_package_document_abandoned_with_replacement(self):
        doc = format_package_document(self.mock_abandoned_package)
        self.assertEqual(doc["name"], "swiftmailer/swiftmailer")
        self.assertTrue(doc["abandoned"])
        self.assertEqual(doc["replacement_package"], "symfony/mailer")
        self.assertIn("**PACKAGE ABANDONED:** Maintainer recommends using `symfony/mailer`", doc["text"])

    def test_format_package_document_with_advisories(self):
        doc = format_package_document(self.mock_package_info, advisories=self.mock_advisories)
        self.assertEqual(doc["advisory_count"], 1)
        self.assertIn("Security Advisories (1)", doc["text"])
        self.assertIn("PKSA-dmw8-jd8k-q3c6", doc["text"])
        self.assertIn("CVE-2022-24816", doc["text"])
        self.assertIn("Affected Versions: `<2.3.6|>=3.0.0,<3.0.1`", doc["text"])

    def test_format_tombstone(self):
        tombstone = format_tombstone("vendor/deleted-pkg")
        self.assertEqual(tombstone["name"], "vendor/deleted-pkg")
        self.assertTrue(tombstone["_deleted"])
        self.assertEqual(tombstone["text"], "")
        self.assertIn("id", tombstone)

    def test_packagist_source_validation(self):
        with self.assertRaises(ValueError):
            packagist_source()

    def test_packagist_source_iteration(self):
        mock_client = MagicMock()
        mock_client.get_security_advisories.return_value = {
            "monolog/monolog": self.mock_advisories
        }
        mock_client.get_package_info.return_value = self.mock_package_info

        items = list(packagist_source(packages=["monolog/monolog"], client=mock_client))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "monolog/monolog")
        self.assertEqual(items[0]["advisory_count"], 1)

    def test_packagist_source_exclude_abandoned_yields_tombstone(self):
        mock_client = MagicMock()
        mock_client.get_security_advisories.return_value = {}
        mock_client.get_package_info.return_value = self.mock_abandoned_package

        items = list(packagist_source(
            packages=["swiftmailer/swiftmailer"],
            client=mock_client,
            exclude_abandoned=True
        ))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "swiftmailer/swiftmailer")
        self.assertTrue(items[0]["_deleted"])

    def test_client_headers_and_defaults(self):
        client = PackagistClient(token="test-token", user_agent="CustomAgent/1.0")
        self.assertEqual(client.token, "test-token")
        self.assertEqual(client.user_agent, "CustomAgent/1.0")
        self.assertEqual(client.base_url, "https://packagist.org")


if __name__ == "__main__":
    unittest.main()
