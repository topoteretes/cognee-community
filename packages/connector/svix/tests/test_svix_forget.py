"""Tests for forget-on-delete behavior in the Svix connector."""

from fake_svix import FakeSvix

from cognee_community_connector_svix.svix import (
    SvixAPIError,
    _iter_rows,
)


def _sync(fake: FakeSvix, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://dashboard.svix.com", state, stats, **kwargs))
    return rows, stats


def test_deleted_app_emits_tombstone():
    fake = FakeSvix()
    fake.add_app("app_1", "App 1")
    fake.add_app("app_2", "App 2")
    state = {}

    rows1, _ = _sync(fake, state)
    assert len(rows1) == 2

    del fake.apps["app_1"]

    rows2, stats2 = _sync(fake, state)
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0] == {"id": "app:app_1", "_deleted": True}
    assert stats2["deleted"] == 1
    assert "app:app_1" not in state["fingerprints"]
    assert "app:app_2" in state["fingerprints"]


def test_deleted_endpoint_and_event_type_emit_tombstones():
    fake = FakeSvix()
    fake.add_app("app_1", "App 1")
    fake.add_endpoint("app_1", "ep_1", "https://api.example.com/hook")
    fake.add_event_type("user.created")
    state = {}

    _sync(fake, state)
    assert len(state["fingerprints"]) == 3

    del fake.endpoints["app_1"]["ep_1"]
    del fake.event_types["user.created"]

    rows2, stats2 = _sync(fake, state)
    tombstone_ids = {r["id"] for r in rows2 if r.get("_deleted")}
    assert tombstone_ids == {"endpoint:app_1:ep_1", "event_type:user.created"}
    assert stats2["deleted"] == 2
    assert "endpoint:app_1:ep_1" not in state["fingerprints"]
    assert "event_type:user.created" not in state["fingerprints"]


def test_deselected_kind_forgets_stored_records():
    fake = FakeSvix()
    fake.add_app("app_1", "App 1")
    fake.add_event_type("user.created")
    state = {}

    _sync(fake, state, include=("applications", "event_types"))
    assert len(state["fingerprints"]) == 2

    rows2, _ = _sync(fake, state, include=("applications",))
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "event_type:user.created"
    assert "event_type:user.created" not in state["fingerprints"]


def test_transient_error_during_deletion_check_preserves_record():
    fake = FakeSvix()
    fake.add_app("app_1", "App 1")
    state = {}

    _sync(fake, state)
    del fake.apps["app_1"]

    fake.fail_paths["api/v1/app/app_1"] = SvixAPIError("Server error on single lookup")

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 0
    assert stats2["failed"] == 1
    assert "app:app_1" in state["fingerprints"]
