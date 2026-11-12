"""CPU-only sanity tests for typed source and lexical section projection."""

import unittest

from cognee_community_connector_opensiro_vsm.native import chunks, typed_model
from cognee_community_connector_opensiro_vsm.source import parse_assessment

SHA = "a" * 40
DOC = """---
harness_id: sample
project_name: Sample
repository: https://github.com/example/sample
review_ref: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
profile_version: v1
assessment_procedure_version: v1
status: included
autonomy_s1: A
autonomy_s2: —
autonomy_s3: ?
autonomy_s3_star: A(P)
autonomy_s4: C
autonomy_s5: P
---

## S1 — Operations
Evidence S1.
## S2 — Coordination
Evidence S2.
## S3 — Management
Evidence S3.
## S3* — Audit
Evidence S3 star.
## S4 — Intelligence
Evidence S4.
## S5 — Identity
Evidence S5.
"""


class NativeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rec, _ = parse_assessment("assessments/sample.md", DOC.encode(), SHA)

    def test_exact_typed_grades(self):
        entity = typed_model(self.rec, SHA)
        assessments = entity.assessments
        self.assertEqual(len(assessments), 1)
        self.assertEqual(
            [(r.function, r.autonomy) for r in assessments[0].function_states],
            list(self.rec.states),
        )
        self.assertEqual(assessments[0].reviewed_upstream_ref, self.rec.review_ref)
        self.assertEqual(assessments[0].content_sha256, self.rec.sha256)

    def test_section_chunks_exact(self):
        items = chunks(self.rec)
        self.assertEqual(len(items), 6)
        self.assertEqual({x.document_name for x in items}, {"sample.md"})
        self.assertTrue(all(x.document_id == self.rec.external_id for x in items))
        self.assertTrue(all("Project: Sample" in x.text for x in items))


if __name__ == "__main__":
    unittest.main()
