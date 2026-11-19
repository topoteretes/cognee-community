"""Unit tests for the Trivy sync walk, incremental state, and tombstone handling."""

import copy

from cognee_community_connector_trivy.trivy import (
    TrivyReportParser,
    _check_resource_name,
    _iter_trivy_rows,
)

ROW_COLUMNS = {"id", "title", "content", "url", "_deleted"}


def _sync(reports: list[dict], state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_trivy_rows(reports, state, stats, **kwargs))
    return rows, stats


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows}


def _tombstones(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


def test_first_sync_emits_all_trivy_documents(sample_trivy_json):
    state = {}
    rows, stats = _sync([sample_trivy_json], state)

    assert len(rows) == 5  # 1 summary + 2 vulns + 1 misconfig + 1 secret
    assert all(set(row.keys()) == ROW_COLUMNS for row in rows)
    assert all(row["_deleted"] is False for row in rows)
    assert stats["emitted"] == 5
    assert stats["skipped"] == 0
    assert stats["deleted"] == 0


def test_second_sync_skips_unchanged_trivy_records(sample_trivy_json):
    state = {}
    _sync([sample_trivy_json], state)

    # Re-sync same report
    rows2, stats2 = _sync([sample_trivy_json], state)
    assert rows2 == []
    assert stats2["emitted"] == 0
    assert stats2["skipped"] == 5
    assert stats2["deleted"] == 0


def test_patched_vulnerability_emits_tombstone(sample_trivy_json):
    state = {}
    _sync([sample_trivy_json], state)

    # Simulate subsequent scan after patching CVE-2023-38545
    patched_report = copy.deepcopy(sample_trivy_json)
    # Remove CVE-2023-38545 from results
    vulns = patched_report["Results"][0]["Vulnerabilities"]
    patched_report["Results"][0]["Vulnerabilities"] = [
        v for v in vulns if v["VulnerabilityID"] != "CVE-2023-38545"
    ]

    rows, stats = _sync([patched_report], state)
    tombstones = _tombstones(rows)

    assert len(tombstones) == 1
    tombstone_id = next(iter(tombstones))
    assert "CVE-2023-38545" in tombstone_id
    assert stats["deleted"] == 1


def test_include_filter_vulnerabilities_only(sample_trivy_json):
    state = {}
    rows, stats = _sync([sample_trivy_json], state, include=("vulnerabilities",))

    assert len(rows) == 2
    assert all("trivy:vuln:" in r["id"] for r in rows)
    assert stats["emitted"] == 2


def test_include_filter_secrets_only(sample_trivy_json):
    state = {}
    rows, stats = _sync([sample_trivy_json], state, include=("secrets",))

    assert len(rows) == 1
    assert "trivy:secret:" in rows[0]["id"]
    assert stats["emitted"] == 1


def test_trivy_report_parser_methods(sample_trivy_json, tmp_path):
    # Dict
    assert TrivyReportParser.load_report(sample_trivy_json) == sample_trivy_json

    # File path
    file_path = tmp_path / "scan.json"
    import json

    file_path.write_text(json.dumps(sample_trivy_json), encoding="utf-8")
    loaded = TrivyReportParser.load_report(str(file_path))
    assert loaded["ArtifactName"] == sample_trivy_json["ArtifactName"]


def test_check_active_cancels_trivy_sync(sample_trivy_json):
    state = {}

    def cancel_checkpoint():
        raise InterruptedError("Cancelled")

    try:
        _sync([sample_trivy_json], state, check_active=cancel_checkpoint)
        raise AssertionError("Should have raised InterruptedError")
    except InterruptedError:
        pass


def test_resource_name_validation():
    _check_resource_name("trivy_documents")
    _check_resource_name("trivy_cluster_scans")

    for invalid in ("", "Trivy-Docs", "trivy scans", "x" * 65):
        try:
            _check_resource_name(invalid)
            raise AssertionError(f"Expected failure for {invalid}")
        except ValueError:
            pass
