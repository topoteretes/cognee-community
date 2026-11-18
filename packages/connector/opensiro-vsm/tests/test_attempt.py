"""Tests for single-host durable stage claims (B15 DLT-precommit kill coverage)."""

import json
import tempfile
import unittest
from pathlib import Path

from cognee_community_connector_opensiro_vsm.attempt import claim_attempt
from cognee_community_connector_opensiro_vsm.source import SourceError

COMMIT = "a" * 40


class AttemptTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name) / "claims"

    def claim(self, dataset="new", commit=COMMIT, tag="src"):
        return claim_attempt(self.root, dataset, tag, commit, ("goose", "langflow"))

    def test_mark_before_backend_can_store_any_data(self):
        path = self.claim()
        data = json.loads(path.read_text())
        self.assertEqual(data["state"], "CLAIMED_NEVER_REUSABLE")
        self.assertEqual(data["dataset"], "new")
        self.assertEqual(data["selected_count"], 2)
        self.assertEqual(len(data["selected_sha256"]), 64)

    def test_retry_same_dataset_denied_even_when_no_cognee_docs_exist(self):
        self.claim()
        with self.assertRaisesRegex(SourceError, "previously claimed"):
            self.claim()
        self.assertEqual(len(list(self.root.glob("*.json"))), 1)

    def test_dataset_claim_is_unique_across_tags_and_source_commits(self):
        self.claim()
        with self.assertRaises(SourceError):
            self.claim(tag="other", commit="b" * 40)
        self.assertEqual(len(list(self.root.glob("*.json"))), 1)

    def test_fresh_generation_dataset_allowed(self):
        a = self.claim(dataset="v1")
        b = self.claim(dataset="v2")
        self.assertNotEqual(a, b)
        self.assertEqual(len(list(self.root.glob("*.json"))), 2)

    def test_path_traversal_cannot_escape_attempt_root(self):
        p = self.claim(dataset="../unsafe/../../dataset")
        self.assertEqual(p.parent, self.root)
        self.assertTrue(p.is_file())

    def test_bad_claim_rejected_without_writing(self):
        for bad in ("", "a" * 161):
            with self.assertRaises(SourceError):
                self.claim(dataset=bad)
        with self.assertRaises(SourceError):
            self.claim(commit="HEAD")
        self.assertFalse(self.root.exists())

    def test_partial_corrupted_claim_blocks_reuse(self):
        p = self.claim()
        p.write_text("{")
        with self.assertRaises(SourceError):
            self.claim()

    def test_distinct_paths_have_pinned_content(self):
        a = self.claim(dataset="a")
        b = self.claim(dataset="b")
        self.assertEqual(json.loads(a.read_text())["dataset"], "a")
        self.assertEqual(json.loads(b.read_text())["dataset"], "b")


if __name__ == "__main__":
    unittest.main()
