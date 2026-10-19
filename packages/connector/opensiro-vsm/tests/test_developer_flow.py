"""Offline test of search-hit provenance and read-only source inspection."""

import unittest
from types import SimpleNamespace

from examples.search_and_inspect import result_hits, source_detail


class DeveloperFlowTests(unittest.TestCase):
    def setUp(self):
        record = SimpleNamespace(
            harness_id="sample",
            project_name="Sample Harness",
            assessment_url="https://github.com/opensiro/vsm-harness-index/blob/"
            + "a" * 40
            + "/assessments/sa/sample.md",
            repository="https://github.com/example/sample",
            review_ref="b" * 40,
            sha256="c" * 64,
            raw_bytes=b"# Sample full canonical assessment\n",
            primary_evidence=("first original source",),
            grades=lambda: {"S1": "A"},
        )
        self.snapshot = SimpleNamespace(accepted={"sample": record})

    def test_native_hit_maps_to_pinned_original_source(self):
        response = [
            {
                "search_result": [
                    {
                        "type": "DocumentChunk",
                        "document_id": "opensiro/vsm-harness-index:sample",
                        "text": "Project: Sample Harness\n## S1 - Operations\npublic action history",
                    }
                ],
            }
        ]
        hits = result_hits(response, self.snapshot)
        self.assertEqual(hits[0]["harness_id"], "sample")
        self.assertEqual(hits[0]["rank"], 1)
        detail = source_detail(self.snapshot, "sample", hits, True)
        self.assertEqual(
            detail["pinned_upstream_tree"], "https://github.com/example/sample/tree/" + "b" * 40
        )
        self.assertIn("Sample full canonical assessment", detail["full_canonical_markdown"])

    def test_fail_closed_for_unknown_or_malformed_hit(self):
        for item in (
            {
                "type": "DocumentChunk",
                "document_id": "opensiro/vsm-harness-index:absent",
                "text": "x",
            },
            {"type": "DocumentChunk", "text": "Project: Sample"},
            {"type": "Other", "document_id": "opensiro/vsm-harness-index:sample", "text": "x"},
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                result_hits([{"search_result": [item]}], self.snapshot)

    def test_unreturned_source_cannot_be_credited_to_search(self):
        with self.assertRaises(ValueError):
            source_detail(self.snapshot, "sample", [], False)

    def test_unsupported_response_fails_closed(self):
        with self.assertRaises(ValueError):
            result_hits([{"unexpected": []}], self.snapshot)


if __name__ == "__main__":
    unittest.main()
