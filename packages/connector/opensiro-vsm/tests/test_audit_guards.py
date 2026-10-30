"""B18: audit must fail closed before touching Cognee for invalid selections."""

import asyncio
import unittest
from collections import Counter
from unittest.mock import patch

from cognee_community_connector_opensiro_vsm.audit import audit_dataset, verify_node_type_counts
from cognee_community_connector_opensiro_vsm.source import Snapshot, SourceError

PIN = "a" * 40


class AuditSelectionTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot(
            commit=PIN,
            accepted={},
            excluded={},
            proposed={},
            catalog_ids=frozenset(),
            unquoted_question_count=0,
        )

    def test_empty_selection_rejected_before_any_sdk_access(self):
        with patch("cognee_community_connector_opensiro_vsm.audit.source_data") as source:
            with self.assertRaisesRegex(SourceError, "empty or duplicated"):
                asyncio.run(audit_dataset(self.snapshot, (), "missing", "tag", lexical=False))
            source.assert_not_called()

    def test_unknown_selection_rejected_before_any_sdk_access(self):
        with patch("cognee_community_connector_opensiro_vsm.audit.source_data") as source:
            with self.assertRaisesRegex(SourceError, "non-admitted"):
                asyncio.run(
                    audit_dataset(
                        self.snapshot, ("not-in-catalog",), "missing", "tag", lexical=True
                    )
                )
            source.assert_not_called()

    def test_duplicates_rejected_before_any_sdk_access(self):
        with patch("cognee_community_connector_opensiro_vsm.audit.source_data") as source:
            with self.assertRaisesRegex(SourceError, "empty or duplicated"):
                asyncio.run(
                    audit_dataset(self.snapshot, ("x", "x"), "missing", "tag", lexical=True)
                )
            source.assert_not_called()


class AuditCountTests(unittest.TestCase):
    def test_absent_zero_evidence_is_valid(self):
        observed = Counter({"VSMHarness": 3, "VSMAssessment": 3, "VSMFunction": 18})
        expected = {"VSMHarness": 3, "VSMAssessment": 3, "VSMFunction": 18, "VSMEvidence": 0}
        verify_node_type_counts(observed, expected)

    def test_unexpected_nonzero_evidence_is_rejected(self):
        observed = Counter(
            {"VSMHarness": 3, "VSMAssessment": 3, "VSMFunction": 18, "VSMEvidence": 1}
        )
        expected = {"VSMHarness": 3, "VSMAssessment": 3, "VSMFunction": 18, "VSMEvidence": 0}
        with self.assertRaisesRegex(SourceError, "mismatched"):
            verify_node_type_counts(observed, expected)

    def test_unknown_extra_node_kind_rejected(self):
        with self.assertRaisesRegex(SourceError, "unexpected"):
            verify_node_type_counts(Counter({"VSMHarness": 1, "Other": 1}), {"VSMHarness": 1})


if __name__ == "__main__":
    unittest.main()
