"""Tests for row emission, frozen schema, redaction, and pagination in Svix connector."""

from fake_svix import FakeSvix

from cognee_community_connector_svix.svix import ROW_COLUMNS, _iter_rows


def _workspace() -> FakeSvix:
    fake = FakeSvix()
    fake.add_app("app_1", "Production SaaS", uid="org_123", rateLimit=500)
    fake.add_endpoint(
        "app_1",
        "ep_1",
        "https://api.example.com/webhooks/svix",
        description="Main Billing Receiver",
        secret="whsec_supersecretkey123",
        filterTypes=["invoice.paid", "user.created"],
    )
    fake.add_event_type("invoice.paid", description="Triggered when customer pays")
    return fake


def _sync(fake: FakeSvix, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://dashboard.svix.com", state, stats, **kwargs))
    return rows, stats


def test_first_sync_emits_every_record_with_frozen_row_shape():
    fake = _workspace()
    rows, stats = _sync(fake, {})

    ids = {r["id"] for r in rows}
    assert ids == {"app:app_1", "endpoint:app_1:ep_1", "event_type:invoice.paid"}
    assert all(set(r) == set(ROW_COLUMNS) for r in rows)
    assert all(r["_deleted"] is False and r["title"] and r["content"] for r in rows)

    urls = {r["id"]: r["url"] for r in rows}
    assert urls["app:app_1"] == "https://dashboard.svix.com/apps/app_1"
    assert urls["endpoint:app_1:ep_1"] == "https://dashboard.svix.com/apps/app_1/endpoints/ep_1"
    assert urls["event_type:invoice.paid"] == "https://dashboard.svix.com/event-types/invoice.paid"
    assert stats["failed"] == 0


def test_endpoint_secrets_and_headers_are_strictly_redacted():
    fake = _workspace()
    rows, _ = _sync(fake, {})

    all_text = " ".join(f"{r['title']} {r['content']}" for r in rows)
    assert "whsec_supersecretkey123" not in all_text
    assert "[Redacted & Protected]" in all_text


def test_pagination_fetches_all_pages_seamlessly():
    fake = FakeSvix()
    for idx in range(1, 65):
        fake.add_app(f"app_{idx}", f"Application {idx}")

    rows, stats = _sync(fake, {})
    assert len(rows) == 64
    assert stats["emitted"] == 64


def test_include_filters_selected_resources():
    fake = _workspace()
    rows, _ = _sync(fake, {}, include=("applications",))
    assert len(rows) == 1
    assert rows[0]["id"] == "app:app_1"


def test_include_archived_events_flag():
    fake = FakeSvix()
    fake.add_event_type("active.event", archived=False)
    fake.add_event_type("legacy.event", archived=True)

    rows1, _ = _sync(fake, {}, include=("event_types",), include_archived_events=False)
    assert {r["id"] for r in rows1} == {"event_type:active.event"}

    rows2, _ = _sync(fake, {}, include=("event_types",), include_archived_events=True)
    assert {r["id"] for r in rows2} == {"event_type:active.event", "event_type:legacy.event"}
