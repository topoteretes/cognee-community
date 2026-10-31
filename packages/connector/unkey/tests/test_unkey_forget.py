"""Tests for forget-on-delete behavior in the Unkey connector."""

from fake_unkey import FakeUnkey

from cognee_community_connector_unkey.unkey import (
    UnkeyAPIError,
    _iter_rows,
)


def _sync(fake: FakeUnkey, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://app.unkey.com", state, stats, **kwargs))
    return rows, stats


def test_deleted_api_emits_tombstone():
    fake = FakeUnkey()
    fake.add_api("api_1", "API 1")
    fake.add_api("api_2", "API 2")
    state = {}

    rows1, _ = _sync(fake, state)
    assert len(rows1) == 2

    del fake.apis["api_1"]

    rows2, stats2 = _sync(fake, state)
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0] == {"id": "api:api_1", "_deleted": True}
    assert stats2["deleted"] == 1
    assert "api:api_1" not in state["fingerprints"]
    assert "api:api_2" in state["fingerprints"]


def test_deleted_key_and_identity_emit_tombstones():
    fake = FakeUnkey()
    fake.add_api("api_1", "API 1")
    fake.add_key("api_1", "key_1", "Key 1")
    fake.add_identity("id_1")
    state = {}

    _sync(fake, state)
    assert len(state["fingerprints"]) == 3

    del fake.keys["api_1"]["key_1"]
    del fake.identities["id_1"]

    rows2, stats2 = _sync(fake, state)
    tombstone_ids = {r["id"] for r in rows2 if r.get("_deleted")}
    assert tombstone_ids == {"key:api_1:key_1", "identity:id_1"}
    assert stats2["deleted"] == 2
    assert "key:api_1:key_1" not in state["fingerprints"]
    assert "identity:id_1" not in state["fingerprints"]


def test_deselected_kind_forgets_stored_records():
    fake = FakeUnkey()
    fake.add_api("api_1", "API 1")
    fake.add_identity("id_1")
    state = {}

    _sync(fake, state, include=("apis", "identities"))
    assert len(state["fingerprints"]) == 2

    rows2, _ = _sync(fake, state, include=("apis",))
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "identity:id_1"
    assert "identity:id_1" not in state["fingerprints"]


def test_transient_error_during_deletion_check_preserves_record():
    fake = FakeUnkey()
    fake.add_api("api_1", "API 1")
    state = {}

    _sync(fake, state)
    del fake.apis["api_1"]

    fake.fail_paths["v1/apis.getApi"] = UnkeyAPIError("Server error on single lookup")

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 0
    assert stats2["failed"] == 1
    assert "api:api_1" in state["fingerprints"]
