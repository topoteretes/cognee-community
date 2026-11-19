"""Unit tests for the Traefik sync walk, incremental state, and tombstone handling."""

import contextlib

from fake_traefik import FakeTraefik

from cognee_community_connector_traefik.traefik import (
    TraefikAPIError,
    _check_resource_name,
    _iter_rows,
)

ROW_COLUMNS = {"id", "title", "content", "url", "_deleted"}


def _sync(fake: FakeTraefik, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, state, stats, **kwargs))
    return rows, stats


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows}


def _tombstones(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


def test_first_sync_emits_all_documents(fake_traefik):
    state = {}
    rows, stats = _sync(fake_traefik, state)

    expected_ids = {
        "traefik:cluster:overview",
        "traefik:http:router:api-router@docker",
        "traefik:http:router:dashboard-router@docker",
        "traefik:http:service:api-service@docker",
        "traefik:http:service:dashboard-service@docker",
        "traefik:http:middleware:rate-limit-100@docker",
        "traefik:http:middleware:auth-forward@docker",
        "traefik:http:middleware:ip-allow-office@docker",
    }
    assert _ids(rows) == expected_ids
    assert all(set(row.keys()) == ROW_COLUMNS for row in rows)
    assert all(row["_deleted"] is False for row in rows)
    assert stats["emitted"] == len(expected_ids)
    assert stats["skipped"] == 0
    assert stats["deleted"] == 0


def test_second_sync_skips_unchanged_records(fake_traefik):
    state = {}
    # First sync
    _rows1, stats1 = _sync(fake_traefik, state)
    assert stats1["emitted"] > 0

    # Second sync without changes
    rows2, stats2 = _sync(fake_traefik, state)
    assert rows2 == []
    assert stats2["emitted"] == 0
    assert stats2["skipped"] == stats1["scanned"]
    assert stats2["deleted"] == 0


def test_modification_emits_updated_row(fake_traefik):
    state = {}
    _sync(fake_traefik, state)

    # Modify an existing router's rule
    fake_traefik.add_http_router(
        "api-router@docker",
        rule="Host(`api.example.com`) && PathPrefix(`/v2`)",
        service="api-service@docker",
    )

    rows, stats = _sync(fake_traefik, state)
    assert len(rows) == 1
    assert rows[0]["id"] == "traefik:http:router:api-router@docker"
    assert "PathPrefix(`/v2`)" in rows[0]["content"]
    assert rows[0]["_deleted"] is False
    assert stats["emitted"] == 1


def test_deletion_emits_tombstone(fake_traefik):
    state = {}
    _sync(fake_traefik, state)

    # Remove dashboard router
    fake_traefik.remove_http_router("dashboard-router@docker")

    rows, stats = _sync(fake_traefik, state)
    tombstones = _tombstones(rows)
    assert tombstones == {"traefik:http:router:dashboard-router@docker"}
    assert stats["deleted"] == 1


def test_include_filter_restricts_kinds(fake_traefik):
    state = {}
    rows, _stats = _sync(fake_traefik, state, include=("routers",))

    ids = _ids(rows)
    assert ids == {
        "traefik:http:router:api-router@docker",
        "traefik:http:router:dashboard-router@docker",
    }
    assert all("router" in r_id for r_id in ids)


def test_fail_closed_on_backend_error_preserves_fingerprints(fake_traefik):
    state = {}
    _sync(fake_traefik, state)
    saved_fingerprints = dict(state["fingerprints"])

    # Simulate backend crash
    fake_traefik.fail_with_500 = True

    with contextlib.suppress(TraefikAPIError):
        _sync(fake_traefik, state)

    # Existing fingerprints must NOT be wiped or marked as deleted!
    assert state["fingerprints"] == saved_fingerprints


def test_check_active_cancels_sync(fake_traefik):
    state = {}

    def cancel_checkpoint():
        raise InterruptedError("Sync canceled by host")

    try:
        _sync(fake_traefik, state, check_active=cancel_checkpoint)
        raise AssertionError("Should have raised InterruptedError")
    except InterruptedError:
        pass


def test_resource_name_validation():
    # Valid
    _check_resource_name("traefik_documents")
    _check_resource_name("traefik_prod_1")

    # Invalid
    for invalid in ("", "Traefik-Docs", "traefik docs", "a" * 65, 123):
        try:
            _check_resource_name(invalid)
            raise AssertionError(f"Expected error for {invalid}")
        except ValueError:
            pass
