"""Hermetic unit tests for the Hypothesis connector."""

import unittest
from unittest.mock import MagicMock, patch

from cognee_community_connector_hypothesis import (
    HypothesisAnnotation,
    HypothesisClient,
    aggregate_annotations_by_url,
    extract_exact_quote,
    extract_page_title,
    hypothesis_pages_resource,
    hypothesis_source,
    parse_raw_annotation,
)


class TestHypothesisConnector(unittest.TestCase):
    def setUp(self):
        self.sample_raw_annotation_1 = {
            "id": "ann-101",
            "uri": "https://arxiv.org/abs/1706.03762",
            "document": {"title": ["Attention Is All You Need"]},
            "text": "Core foundation of transformer architectures.",
            "tags": ["ml", "transformers", "nlp"],
            "user": "acct:researcher@hypothes.is",
            "created": "2026-10-01T12:00:00Z",
            "updated": "2026-10-01T12:05:00Z",
            "target": [
                {
                    "source": "https://arxiv.org/abs/1706.03762",
                    "selector": [
                        {
                            "type": "TextQuoteSelector",
                            "exact": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks",
                            "prefix": "Abstract. ",
                            "suffix": " in an encoder-decoder configuration.",
                        }
                    ],
                }
            ],
        }

        self.sample_raw_annotation_2 = {
            "id": "ann-102",
            "uri": "https://arxiv.org/abs/1706.03762",
            "document": {"title": ["Attention Is All You Need"]},
            "text": "Multi-head attention mechanism note.",
            "tags": ["attention"],
            "user": "acct:researcher@hypothes.is",
            "created": "2026-10-01T13:00:00Z",
            "updated": "2026-10-01T13:10:00Z",
            "target": [
                {
                    "selector": [
                        {
                            "type": "TextQuoteSelector",
                            "exact": "Multi-head attention allows the model to jointly attend to information from different representation subspaces",
                        }
                    ]
                }
            ],
        }

        self.sample_raw_annotation_pdf = {
            "id": "ann-201",
            "uri": "https://example.com/papers/quantum-algorithms.pdf",
            "document": {},  # No title provided in document metadata
            "text": "Quantum speedup analysis note.",
            "tags": ["quantum", "algorithms"],
            "user": "acct:researcher@hypothes.is",
            "created": "2026-10-02T09:00:00Z",
            "updated": "2026-10-02T09:15:00Z",
            "target": [
                {
                    "selector": [
                        {
                            "type": "TextQuoteSelector",
                            "exact": "Shor's algorithm provides exponential speedup over classical factoring.",
                        }
                    ]
                }
            ],
        }

    def test_extract_exact_quote(self):
        quote = extract_exact_quote(self.sample_raw_annotation_1)
        self.assertIn("dominant sequence transduction models", quote)

        empty_quote = extract_exact_quote({})
        self.assertEqual(empty_quote, "")

    def test_extract_page_title(self):
        title = extract_page_title(self.sample_raw_annotation_1, "https://arxiv.org/abs/1706.03762")
        self.assertEqual(title, "Attention Is All You Need")

        # Fallback to URL path formatting when title is absent
        fallback_title = extract_page_title(self.sample_raw_annotation_pdf, "https://example.com/papers/quantum-algorithms.pdf")
        self.assertEqual(fallback_title, "Quantum Algorithms.Pdf")

    def test_parse_raw_annotation(self):
        parsed = parse_raw_annotation(self.sample_raw_annotation_1)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.annotation_id, "ann-101")
        self.assertEqual(parsed.page_title, "Attention Is All You Need")
        self.assertIn("transformers", parsed.tags)
        self.assertIn("dominant sequence transduction", parsed.quote)

        # Invalid annotation without id
        invalid = parse_raw_annotation({})
        self.assertIsNone(invalid)

    def test_aggregate_annotations_by_url(self):
        ann1 = parse_raw_annotation(self.sample_raw_annotation_1)
        ann2 = parse_raw_annotation(self.sample_raw_annotation_2)
        ann3 = parse_raw_annotation(self.sample_raw_annotation_pdf)

        docs = aggregate_annotations_by_url([ann1, ann2, ann3])
        # Two distinct URLs
        self.assertEqual(len(docs), 2)

        # Find document for arxiv paper
        arxiv_doc = next(d for d in docs if "arxiv.org" in d["url"])
        self.assertEqual(arxiv_doc["annotation_count"], 2)
        self.assertEqual(arxiv_doc["title"], "Attention Is All You Need")
        self.assertIn("Core foundation of transformer architectures.", arxiv_doc["content"])
        self.assertIn("Multi-head attention mechanism note.", arxiv_doc["content"])
        self.assertIn("`#transformers`", arxiv_doc["content"])
        self.assertIn("data_id", arxiv_doc)

    def test_hypothesis_client_pagination(self):
        mock_http = MagicMock()

        # Page 1 response
        mock_resp_1 = MagicMock()
        mock_resp_1.status_code = 200
        mock_resp_1.json.return_value = {
            "rows": [self.sample_raw_annotation_1],
            "total": 2,
        }

        # Page 2 response
        mock_resp_2 = MagicMock()
        mock_resp_2.status_code = 200
        mock_resp_2.json.return_value = {
            "rows": [self.sample_raw_annotation_2],
            "total": 2,
        }

        # Page 3 empty (end of results)
        mock_resp_3 = MagicMock()
        mock_resp_3.status_code = 200
        mock_resp_3.json.return_value = {"rows": [], "total": 2}

        mock_http.get.side_effect = [mock_resp_1, mock_resp_2, mock_resp_3]

        client = HypothesisClient(api_token="test_token", client=mock_http)
        items = client.fetch_annotations(username="researcher", limit=1)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].annotation_id, "ann-101")
        self.assertEqual(items[1].annotation_id, "ann-102")

    def test_hypothesis_client_error_handling(self):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized token"
        mock_resp.raise_for_status.side_effect = Exception("HTTP 401 Unauthorized")
        mock_http.get.return_value = mock_resp

        client = HypothesisClient(api_token="invalid_token", client=mock_http)
        with self.assertRaises(Exception):
            client.fetch_annotations(username="researcher")

    def test_dlt_source_and_resource(self):
        with patch.object(HypothesisClient, "fetch_annotations") as mock_fetch:
            ann1 = parse_raw_annotation(self.sample_raw_annotation_1)
            mock_fetch.return_value = [ann1]

            source = hypothesis_source(api_token="dummy", username="testuser")
            self.assertIsNotNone(source)

            # Test resource iteration
            resource = hypothesis_pages_resource(api_token="dummy", username="testuser")
            results = list(resource)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["title"], "Attention Is All You Need")
            self.assertEqual(results[0]["annotation_count"], 1)


if __name__ == "__main__":
    unittest.main()
