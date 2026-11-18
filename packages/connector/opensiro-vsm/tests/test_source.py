"""No pytest. Run: .venv/bin/python -m unittest discover -s opensiro_source_prototype/tests -v

Pure parsing and Git snapshot fixtures. Current production-sized corpus smoke test
is separately exposed by CLI validate; no need to copy all source into fake repos.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from cognee_community_connector_opensiro_vsm.source import (
    SourceError,
    diff_snapshots,
    inspect_one_historical_record,
    parse_assessment,
    read_snapshot,
)

REVIEW = "1" * 40
FIELDS = ("s1", "s2", "s3", "s3_star", "s4", "s5")
EXAMPLE = """---
harness_id: sample
project_name: Sample Harness
repository: https://github.com/example/sample
review_ref: {review}
profile_version: 0.2.4
assessment_procedure_version: 0.3.7
status: included
autonomy_s1: A
autonomy_s2: —
autonomy_s3: —
autonomy_s3_star: ?
autonomy_s4: A(P)
autonomy_s5: C(P)
---

# Sample

## Primary evidence
- [README](https://github.com/example/sample)
- [test.py](https://github.com/example/sample/blob/{review}/test.py)

## S1 — Operations

Working.
"""
SHA = "a" * 40


def markup(s3="—", status="included", extra="", s3star="?"):
    return (
        EXAMPLE.format(review=REVIEW)
        .replace("autonomy_s3: —", "autonomy_s3: " + s3)
        .replace("autonomy_s3_star: ?", "autonomy_s3_star: " + s3star)
        .replace("status: included", "status: " + status)
        .replace("## S1 — Operations", extra + "\n## S1 — Operations")
    ).encode()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class ParseTests(unittest.TestCase):
    def test_literal_state_and_version(self):
        rec, fixed = parse_assessment("assessments/sample.md", markup(), SHA)
        self.assertTrue(fixed)
        self.assertEqual(
            rec.grades(),
            {"S1": "A", "S2": "—", "S3": "—", "S3_STAR": "?", "S4": "A(P)", "S5": "C(P)"},
        )
        self.assertEqual(len(rec.primary_evidence), 2)
        self.assertEqual(rec.review_ref, REVIEW)
        self.assertTrue(rec.version_id.endswith(rec.sha256))
        self.assertIn(SHA, rec.assessment_url)

    def test_sharded_canonical_path_accepted(self):
        record, _ = parse_assessment("assessments/sa/sample.md", markup(), SHA)
        self.assertTrue(record.assessment_url.endswith("/assessments/sa/sample.md"))
        self.assertEqual(record.grades()["S1"], "A")

    def test_wrong_shard_and_extra_nesting_rejected(self):
        for path in ("assessments/xx/sample.md", "assessments/sa/extra/sample.md"):
            with self.subTest(path=path), self.assertRaises(SourceError):
                parse_assessment(path, markup(), SHA)

    def test_quoted_question_mark_also_accepted(self):
        rec, fixed = parse_assessment("assessments/sample.md", markup(s3star='"?"'), SHA)
        self.assertFalse(fixed)
        self.assertEqual(rec.grades()["S3_STAR"], "?")

    def test_other_question_syntax_rejected(self):
        with self.assertRaises(SourceError):
            parse_assessment("assessments/sample.md", markup(s3star="??"), SHA)

    def test_unknown_grade_rejected(self):
        with self.assertRaises(SourceError):
            parse_assessment("assessments/sample.md", markup(s3="Z"), SHA)

    def test_boolean_grade_rejected(self):
        with self.assertRaises(SourceError):
            parse_assessment("assessments/sample.md", markup(s3="true"), SHA)

    def test_duplicate_yaml_field_rejected(self):
        text = markup().replace(b"status: included", b"status: included\nstatus: proposed")
        with self.assertRaisesRegex(SourceError, "duplicate"):
            parse_assessment("assessments/sample.md", text, SHA)

    def test_no_assessment_proof_inference(self):
        doc = markup(extra="\n## Evidence gaps\n- speculative example\n")
        rec, _ = parse_assessment("assessments/sample.md", doc, SHA)
        self.assertEqual(len(rec.primary_evidence), 2)

    def test_missing_primary_evidence_means_zero_explicit_bullets(self):
        rec, _ = parse_assessment(
            "assessments/sample.md",
            markup().replace(b"## Primary evidence", b"## Evidence gaps"),
            SHA,
        )
        self.assertEqual(rec.primary_evidence, ())
        self.assertIn(b"## Evidence gaps", rec.raw_bytes)

    def test_record_identity_differs_on_grade_correction(self):
        old, _ = parse_assessment("assessments/sample.md", markup(s3="—"), SHA)
        new, _ = parse_assessment("assessments/sample.md", markup(s3="A"), SHA)
        self.assertEqual(old.external_id, new.external_id)
        self.assertEqual(old.review_ref, new.review_ref)
        self.assertNotEqual(old.version_id, new.version_id)
        self.assertEqual(old.grades()["S3"], "—")
        self.assertEqual(new.grades()["S3"], "A")

    def test_path_mismatch_or_abbreviated_commit_rejected(self):
        with self.assertRaises(SourceError):
            parse_assessment("assessments/other.md", markup(), SHA)
        with self.assertRaises(SourceError):
            parse_assessment("assessments/sample.md", markup(), SHA[:8])

    def test_old_included_without_required_contract_fails_closed(self):
        data = markup().replace(b"profile_version: 0.2.4\n", b"")
        with self.assertRaisesRegex(SourceError, "profile_version"):
            parse_assessment("assessments/sample.md", data, SHA)
        rec, _ = parse_assessment("assessments/sample.md", data, SHA, require_contract=False)
        self.assertEqual(rec.profile_version, "")
        self.assertEqual(rec.grades()["S3"], "—")

    def test_other_status_preserves_literal_grades(self):
        rec, _ = parse_assessment("assessments/sample.md", markup(status="proposed"), SHA)
        self.assertEqual(rec.status, "proposed")


class GitSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "local-test@example.invalid")
        git(self.repo, "config", "user.name", "Local Test")
        (self.repo / "assessments").mkdir()
        (self.repo / "data").mkdir()
        self.target = self.repo / "assessments" / "sample.md"
        self.target.write_bytes(markup(s3="—", status="proposed"))
        self._commit_catalog(include=False)
        self.first = git(self.repo, "rev-parse", "HEAD")
        self.target.write_bytes(markup(s3="A", status="included"))
        self._commit_catalog(include=True)
        self.second = git(self.repo, "rev-parse", "HEAD")

    def _commit_catalog(self, include: bool):
        hdr = "catalog_position|harness_id|project_name\n"
        row = "1|sample|Sample Harness\n" if include else ""
        (self.repo / "data" / "catalog.psv").write_text(hdr + row)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "local fixture")

    def test_does_not_follow_mutable_head(self):
        a = read_snapshot(self.repo, self.first)
        b = read_snapshot(self.repo, self.second)
        self.assertEqual(len(a.accepted), 0)
        self.assertEqual(len(a.proposed), 1)
        self.assertEqual(len(b.accepted), 1)
        self.assertEqual(b.accepted["sample"].grades()["S3"], "A")
        result = diff_snapshots(a, b)
        self.assertEqual(result["added"], ["sample"])
        self.assertEqual(result["proposed_to_included"], ["sample"])

    def test_source_correction_same_review_ref(self):
        first = read_snapshot(self.repo, self.second)
        self.target.write_bytes(markup(s3="C", status="included"))
        self._commit_catalog(include=True)
        final = read_snapshot(self.repo, git(self.repo, "rev-parse", "HEAD"))
        change = diff_snapshots(first, final)
        self.assertEqual(change["changed"], ["sample"])
        self.assertEqual(change["changed_same_review_ref"], ["sample"])
        self.assertEqual(change["changed_grades"]["sample"]["S3"], {"old": "A", "new": "C"})

    def test_fail_closed_catalog_mismatch(self):
        self.target.write_bytes(markup(s3="A", status="included"))
        (self.repo / "data" / "catalog.psv").write_text("catalog_position|harness_id\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "corrupt catalog")
        with self.assertRaises(SourceError):
            read_snapshot(self.repo, git(self.repo, "rev-parse", "HEAD"))

    def test_sharded_snapshot_and_pinned_historical_reader(self):
        old_record = inspect_one_historical_record(self.repo, self.second, "sample")
        shard = self.repo / "assessments" / "sa"
        shard.mkdir()
        self.target.rename(shard / "sample.md")
        self._commit_catalog(include=True)
        pinned = git(self.repo, "rev-parse", "HEAD")
        snapshot = read_snapshot(self.repo, pinned)
        new_record = inspect_one_historical_record(self.repo, pinned, "sample")
        self.assertEqual(snapshot.accepted["sample"].sha256, old_record.sha256)
        self.assertEqual(new_record.review_ref, old_record.review_ref)
        self.assertTrue(new_record.assessment_url.endswith("/assessments/sa/sample.md"))
        self.assertTrue(old_record.assessment_url.endswith("/assessments/sample.md"))

    def test_duplicate_flat_and_sharded_rejected(self):
        shard = self.repo / "assessments" / "sa"
        shard.mkdir()
        (shard / "sample.md").write_bytes(self.target.read_bytes())
        self._commit_catalog(include=True)
        with self.assertRaisesRegex(SourceError, "duplicate"):
            read_snapshot(self.repo, git(self.repo, "rev-parse", "HEAD"))
        with self.assertRaisesRegex(SourceError, "exactly one"):
            inspect_one_historical_record(self.repo, git(self.repo, "rev-parse", "HEAD"), "sample")

    def test_unpinned_head_rejected(self):
        with self.assertRaises(SourceError):
            read_snapshot(self.repo, "HEAD")


if __name__ == "__main__":
    unittest.main()
