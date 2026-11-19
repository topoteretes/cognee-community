"""Tests for row emission, frozen schema, redaction, and pagination in Novu connector."""

from fake_novu import FakeNovu

from cognee_community_connector_novu.novu import ROW_COLUMNS, _iter_rows


def _workspace() -> FakeNovu:
    fake = FakeNovu()
    fake.add_workflow("wf_1", "Password Reset", description="Sends password reset link")
    fake.add_topic("security-alerts", "Security Alerts")
    fake.add_group("grp_1", "Authentication")
    fake.add_integration("integ_1", "sendgrid", "email")
    return fake


def _sync(fake: FakeNovu, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://dashboard.novu.co", state, stats, **kwargs))
    return rows, stats


def test_first_sync_emits_every_record_with_frozen_row_shape():
    fake = _workspace()
    rows, stats = _sync(fake, {})

    ids = {r["id"] for r in rows}
    assert ids == {
        "workflow:wf_1",
        "topic:security-alerts",
        "group:grp_1",
        "integration:integ_1",
    }
    assert all(set(r) == set(ROW_COLUMNS) for r in rows)
    assert all(r["_deleted"] is False and r["title"] and r["content"] for r in rows)

    urls = {r["id"]: r["url"] for r in rows}
    assert urls["workflow:wf_1"] == "https://dashboard.novu.co/workflows/edit/wf_1"
    assert urls["topic:security-alerts"] == "https://dashboard.novu.co/topics/security-alerts"
    assert urls["group:grp_1"] == "https://dashboard.novu.co/settings/organization"
    assert urls["integration:integ_1"] == "https://dashboard.novu.co/integrations"
    assert stats["failed"] == 0


def test_provider_credentials_are_strictly_redacted():
    fake = _workspace()
    rows, _ = _sync(fake, {})

    all_text = " ".join(f"{r['title']} {r['content']}" for r in rows)
    assert "sg_secret_key_12345" not in all_text
    assert "whsec_live_12345" not in all_text
    assert "[Redacted & Protected]" in all_text


def test_pagination_fetches_all_pages_seamlessly():
    fake = FakeNovu()
    for idx in range(1, 65):
        fake.add_workflow(f"wf_{idx}", f"Workflow {idx}")

    rows, stats = _sync(fake, {})
    assert len(rows) == 64
    assert stats["emitted"] == 64


def test_include_filters_selected_resources():
    fake = _workspace()
    rows, _ = _sync(fake, {}, include=("workflows",))
    assert len(rows) == 1
    assert rows[0]["id"] == "workflow:wf_1"
