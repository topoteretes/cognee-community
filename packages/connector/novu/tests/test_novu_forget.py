"""Tests for forget-on-delete behavior in the Novu connector."""

from fake_novu import FakeNovu

from cognee_community_connector_novu.novu import (
    NovuAPIError,
    _iter_rows,
)


def _sync(fake: FakeNovu, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://dashboard.novu.co", state, stats, **kwargs))
    return rows, stats


def test_deleted_workflow_emits_tombstone():
    fake = FakeNovu()
    fake.add_workflow("wf_1", "Workflow 1")
    fake.add_workflow("wf_2", "Workflow 2")
    state = {}

    rows1, _ = _sync(fake, state)
    assert len(rows1) == 2

    del fake.workflows["wf_1"]

    rows2, stats2 = _sync(fake, state)
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0] == {"id": "workflow:wf_1", "_deleted": True}
    assert stats2["deleted"] == 1
    assert "workflow:wf_1" not in state["fingerprints"]
    assert "workflow:wf_2" in state["fingerprints"]


def test_deleted_topic_and_integration_emit_tombstones():
    fake = FakeNovu()
    fake.add_topic("top_1", "Topic 1")
    fake.add_integration("integ_1", "twilio", "sms")
    state = {}

    _sync(fake, state)
    assert len(state["fingerprints"]) == 2

    del fake.topics["top_1"]
    del fake.integrations["integ_1"]

    rows2, stats2 = _sync(fake, state)
    tombstone_ids = {r["id"] for r in rows2 if r.get("_deleted")}
    assert tombstone_ids == {"topic:top_1", "integration:integ_1"}
    assert stats2["deleted"] == 2
    assert "topic:top_1" not in state["fingerprints"]
    assert "integration:integ_1" not in state["fingerprints"]


def test_deselected_kind_forgets_stored_records():
    fake = FakeNovu()
    fake.add_workflow("wf_1", "Workflow 1")
    fake.add_topic("top_1", "Topic 1")
    state = {}

    _sync(fake, state, include=("workflows", "topics"))
    assert len(state["fingerprints"]) == 2

    rows2, _ = _sync(fake, state, include=("workflows",))
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "topic:top_1"
    assert "topic:top_1" not in state["fingerprints"]


def test_transient_error_during_deletion_check_preserves_record():
    fake = FakeNovu()
    fake.add_workflow("wf_1", "Workflow 1")
    state = {}

    _sync(fake, state)
    del fake.workflows["wf_1"]

    fake.fail_paths["v1/workflows/wf_1"] = NovuAPIError("Server error on single lookup")

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 0
    assert stats2["failed"] == 1
    assert "workflow:wf_1" in state["fingerprints"]
