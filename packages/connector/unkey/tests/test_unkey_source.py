"""Tests for row emission, frozen schema, redaction, and pagination in Unkey connector."""

from fake_unkey import FakeUnkey

from cognee_community_connector_unkey.unkey import ROW_COLUMNS, _iter_rows


def _workspace() -> FakeUnkey:
    fake = FakeUnkey()
    fake.add_api("api_1", "Production Gateway", workspaceId="ws_101")
    fake.add_key(
        "api_1",
        "key_1",
        "Acme Customer Key",
        ownerId="cust_acme",
        plaintext="unkey_live_supersecret_plain123",
        hash="sha256_supersecret_hash456",
        ratelimit={"limit": 50, "duration": 1000},
    )
    fake.add_identity("id_1", externalId="cust_acme")
    return fake


def _sync(fake: FakeUnkey, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://app.unkey.com", state, stats, **kwargs))
    return rows, stats


def test_first_sync_emits_every_record_with_frozen_row_shape():
    fake = _workspace()
    rows, stats = _sync(fake, {})

    ids = {r["id"] for r in rows}
    assert ids == {"api:api_1", "key:api_1:key_1", "identity:id_1"}
    assert all(set(r) == set(ROW_COLUMNS) for r in rows)
    assert all(r["_deleted"] is False and r["title"] and r["content"] for r in rows)

    urls = {r["id"]: r["url"] for r in rows}
    assert urls["api:api_1"] == "https://app.unkey.com/apis/api_1"
    assert urls["key:api_1:key_1"] == "https://app.unkey.com/apis/api_1/keys/key_1"
    assert urls["identity:id_1"] == "https://app.unkey.com/identities/id_1"
    assert stats["failed"] == 0


def test_key_plaintext_and_hashes_are_strictly_redacted():
    fake = _workspace()
    rows, _ = _sync(fake, {})

    all_text = " ".join(f"{r['title']} {r['content']}" for r in rows)
    assert "unkey_live_supersecret_plain123" not in all_text
    assert "sha256_supersecret_hash456" not in all_text
    assert "[Redacted & Protected]" in all_text


def test_pagination_fetches_all_pages_seamlessly():
    fake = FakeUnkey()
    fake.add_api("api_1", "Production Gateway")
    for idx in range(1, 65):
        fake.add_key("api_1", f"key_{idx}", f"Key {idx}")

    rows, stats = _sync(fake, {}, include=("keys",))
    assert len(rows) == 64
    assert stats["emitted"] == 64


def test_include_filters_selected_resources():
    fake = _workspace()
    rows, _ = _sync(fake, {}, include=("apis",))
    assert len(rows) == 1
    assert rows[0]["id"] == "api:api_1"
