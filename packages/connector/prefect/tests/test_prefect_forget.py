"""Tests for forget-on-delete behavior in the Prefect connector."""

from fake_prefect import FakePrefect

from cognee_community_connector_prefect.prefect import (
    PrefectAPIError,
    _infer_ui_url,
    _iter_rows,
)


def _sync(fake: FakePrefect, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    ui_url = _infer_ui_url(fake.base_url)
    rows = list(_iter_rows(fake, ui_url, state, stats, **kwargs))
    return rows, stats


def test_deleted_flow_emits_tombstone():
    fake = FakePrefect()
    fake.add_flow("f1", "Flow 1")
    fake.add_flow("f2", "Flow 2")
    state = {}

    rows1, _ = _sync(fake, state)
    assert len(rows1) == 2

    # Delete flow 1 from Prefect
    del fake.flows["f1"]

    rows2, stats2 = _sync(fake, state)
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0] == {"id": "flow:f1", "_deleted": True}
    assert stats2["deleted"] == 1
    assert "flow:f1" not in state["fingerprints"]


def test_deleted_deployment_and_run_emit_tombstones():
    fake = FakePrefect()
    fake.add_flow("f1", "Flow 1")
    fake.add_deployment("d1", "Dep 1", "f1")
    fake.add_flow_run("r1", "Run 1", "f1", deployment_id="d1")
    state = {}

    _sync(fake, state)
    assert len(state["fingerprints"]) == 3

    del fake.deployments["d1"]
    del fake.flow_runs["r1"]

    rows2, stats2 = _sync(fake, state)
    tombstone_ids = {r["id"] for r in rows2 if r.get("_deleted")}
    assert tombstone_ids == {"deployment:d1", "flow_run:r1"}
    assert stats2["deleted"] == 2
    assert "deployment:d1" not in state["fingerprints"]
    assert "flow_run:r1" not in state["fingerprints"]


def test_deselected_kind_forgets_stored_records():
    fake = FakePrefect()
    fake.add_flow("f1", "Flow 1")
    fake.add_deployment("d1", "Dep 1", "f1")
    state = {}

    _sync(fake, state, include=("flows", "deployments"))
    assert len(state["fingerprints"]) == 2

    # Run with only flows included
    rows2, _ = _sync(fake, state, include=("flows",))
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "deployment:d1"
    assert "deployment:d1" not in state["fingerprints"]


def test_tag_removed_from_record_forgets_it_when_filtered():
    fake = FakePrefect()
    fake.add_flow("f1", "Flow 1", tags=["critical"])
    state = {}

    _sync(fake, state, flow_tags=["critical"])
    assert "flow:f1" in state["fingerprints"]

    # Flow tag changed in Prefect
    fake.flows["f1"]["tags"] = ["normal"]

    rows2, _ = _sync(fake, state, flow_tags=["critical"])
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "flow:f1"
    assert "flow:f1" not in state["fingerprints"]


def test_transient_error_during_deletion_check_preserves_record():
    fake = FakePrefect()
    fake.add_flow("f1", "Flow 1")
    state = {}

    _sync(fake, state)
    del fake.flows["f1"]

    # Fail the single GET lookup
    fake.fail_paths["/flows/f1"] = PrefectAPIError("Server error on single lookup")

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 0  # Not deleted yet due to failure
    assert stats2["failed"] == 1
    # Still retained in fingerprints to retry next time
    assert "flow:f1" in state["fingerprints"]
