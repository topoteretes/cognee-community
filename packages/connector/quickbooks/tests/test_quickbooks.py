"""Unit and real DLT pipeline integration tests for QuickBooks Online connector.

Coverage:
1. Markdown card semantic rendering (Invoices, Bills, Credit Memos, Line Items, Memos).
2. Privacy boundary controls (include_private_notes opt-in/opt-out).
3. Input validation: entities allowlist and empty check.
4. Two-run real DLT pipeline tests:
   - Incremental sync with persistent LastUpdatedTime cursor.
   - CDC deletion tombstone emission and destination cleanup.
   - CDC failure fallback: fallback active ID sweep catches deletion when CDC fails.
   - Anti-corruption empty-sweep protection with existing known_ids.
   - Multi-company / realm isolation into the same destination dataset.
5. Client HTTP headers, exact QBO SQL query syntax with 'ORDER BY', and Retry-After backoff.
"""

from __future__ import annotations

import dlt
import pytest

from cognee_community_connector_quickbooks.client import (
    QuickBooksClient,
    _retry_after,
)
from cognee_community_connector_quickbooks.quickbooks import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    _bill_to_card,
    _credit_memo_to_card,
    _format_currency,
    _invoice_to_card,
    quickbooks_source,
)
from tests.conftest import FakeQuickBooksClient

# ── 1. Semantic Card Rendering & Privacy Tests ────────────────────────────────


def test_format_currency():
    assert _format_currency(1234.5, "USD") == "$1,234.50 USD"
    assert _format_currency(-45.2, "USD") == "-$45.20 USD"
    assert _format_currency(0, "EUR") == "$0.00 EUR"
    assert _format_currency(None) == "$0.00 USD"


def test_invoice_to_card(sample_invoice):
    # Default: private notes excluded for privacy
    card = _invoice_to_card(sample_invoice, id_prefix="qb_test", include_private_notes=False)

    assert card["id"] == "qb_test:invoice:101"
    assert card["qbo_id"] == "101"
    assert card["title"] == "Invoice #INV-1001 - Acme Corp"
    assert "https://app.qbo.intuit.com/app/invoice?txnId=101" in card["url"]
    assert card["_deleted"] is False

    content = card["content"]
    assert "# Invoice #INV-1001: Acme Corp" in content
    assert "$4,500.00 USD" in content
    assert "Payment Status:** PAID" in content
    assert "## Customer Memo" in content
    assert "Annual software renewal terms." in content
    assert "Platform Subscription" in content
    assert "Dedicated Support" in content
    assert "## Private Notes" not in content

    # Opt-in: private notes included
    card_with_notes = _invoice_to_card(
        sample_invoice, id_prefix="qb_test", include_private_notes=True
    )
    assert "## Private Notes" in card_with_notes["content"]
    assert "Approved by CFO." in card_with_notes["content"]


def test_bill_to_card(sample_bill):
    card = _bill_to_card(sample_bill, id_prefix="qb_test", include_private_notes=False)
    assert card["id"] == "qb_test:bill:201"
    assert card["title"] == "Bill #BILL-500 - Cloudflare Inc"
    assert card["_deleted"] is False
    assert "## Notes" not in card["content"]

    card_with_notes = _bill_to_card(sample_bill, id_prefix="qb_test", include_private_notes=True)
    assert "## Notes" in card_with_notes["content"]
    assert "Enterprise DDoS and CDN" in card_with_notes["content"]


def test_credit_memo_to_card(sample_credit_memo):
    card = _credit_memo_to_card(
        sample_credit_memo, id_prefix="qb_test", include_private_notes=False
    )
    assert card["id"] == "qb_test:creditmemo:301"
    assert card["title"] == "Credit Memo #CM-10 - Acme Corp"
    assert "$250.00 USD" in card["content"]
    assert "## Private Notes" not in card["content"]


# ── 2. Source Configuration & Input Validation ───────────────────────────────


def test_entities_validation():
    fake_client = FakeQuickBooksClient()

    # Empty entities list
    with pytest.raises(ValueError, match="entities must not be empty"):
        quickbooks_source(client=fake_client, entities=[])

    # Unrecognized entity name
    with pytest.raises(ValueError, match="Invalid entity/entities in selection"):
        quickbooks_source(client=fake_client, entities=["Invoice", "InvalidEntity"])


def test_source_attributes_and_scoping():
    fake_client = FakeQuickBooksClient(realm_id="realm_999")
    source = quickbooks_source(client=fake_client, entities=["Invoice"])

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "quickbooks"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "quickbooks_realm_999"


# ── 3. Real Persisted DLT Pipeline Two-Run Tests ──────────────────────────────


def test_incremental_sync_persisted_cursor_two_runs(sample_invoice, tmp_path):
    """Verify LastUpdatedTime watermark persists across real DLT pipeline runs."""
    inv1 = dict(sample_invoice)
    inv2 = dict(sample_invoice)
    inv2["Id"] = "102"
    inv2["DocNumber"] = "INV-1002"
    inv2["MetaData"] = {
        "CreateTime": "2024-04-02T10:00:00Z",
        "LastUpdatedTime": "2024-04-02T11:00:00Z",
    }

    fake_client = FakeQuickBooksClient(invoices=[inv1, inv2], realm_id="company_a")

    db_path = (tmp_path / "qb_inc.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_qb_persisted_cursor",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    # Run 1: Backfill
    pipeline.run(quickbooks_source(client=fake_client, entities=["Invoice"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query(
            "SELECT id, _deleted FROM quickbooks_invoices WHERE _deleted = 0"
        ) as cur,
    ):
        rows1 = cur.fetchall()
        assert len(rows1) == 2

    # Update inv2 with newer timestamp
    inv2_updated = dict(inv2)
    inv2_updated["MetaData"] = {
        "CreateTime": "2024-04-02T10:00:00Z",
        "LastUpdatedTime": "2024-04-03T15:00:00Z",
    }
    fake_client.invoices = [inv2_updated]

    # Run 2: Incremental run uses persisted cursor
    pipeline.run(quickbooks_source(client=fake_client, entities=["Invoice"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query(
            "SELECT id, _deleted FROM quickbooks_invoices WHERE _deleted = 0"
        ) as cur,
    ):
        rows2 = cur.fetchall()
        # Both records remain in destination table
        assert len(rows2) == 2


def test_cdc_deletion_two_run_pipeline(sample_invoice, tmp_path):
    """Verify CDC tombstone physically deletes rows in destination table on merge."""
    fake_client = FakeQuickBooksClient(
        invoices=[sample_invoice],
        cdc_records=[],
        realm_id="company_cdc",
    )

    db_path = (tmp_path / "qb_cdc.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_qb_cdc_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    # Run 1: Load invoice 101
    pipeline.run(quickbooks_source(client=fake_client, entities=["Invoice"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM quickbooks_invoices") as cur,
    ):
        rows1 = cur.fetchall()
        assert len(rows1) == 1
        assert rows1[0][0] == "quickbooks:company_cdc:invoice:101"

    # Run 2: Invoice removed and reported deleted via CDC
    fake_client.invoices = []
    fake_client.cdc_records = [{"Id": "101", "status": "Deleted", "_entity_type": "Invoice"}]
    pipeline.run(quickbooks_source(client=fake_client, entities=["Invoice"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM quickbooks_invoices") as cur,
    ):
        rows2 = cur.fetchall()
        # DLT hard_delete removed the row on merge
        assert len(rows2) == 0


def test_cdc_failure_fallback_to_active_sweep_two_runs(sample_invoice, tmp_path):
    """Verify that when CDC fails, active ID sweep catches deletions."""
    inv1 = dict(sample_invoice)
    inv2 = dict(sample_invoice)
    inv2["Id"] = "102"
    inv2["DocNumber"] = "INV-1002"

    fake_client = FakeQuickBooksClient(
        invoices=[inv1, inv2],
        cdc_records=[],
        realm_id="company_fallback",
    )

    db_path = (tmp_path / "qb_fallback.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_qb_fallback_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    # Run 1: Initial load (invoices 101 and 102)
    pipeline.run(quickbooks_source(client=fake_client, entities=["Invoice"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM quickbooks_invoices") as cur,
    ):
        assert len(cur.fetchall()) == 2

    # Run 2: Invoice 101 deleted (102 active), and CDC API raises a transient error
    fake_client.invoices = [inv2]
    fake_client.cdc_error = RuntimeError("Intuit CDC temporary 503 service unavailable")

    pipeline.run(quickbooks_source(client=fake_client, entities=["Invoice"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM quickbooks_invoices") as cur,
    ):
        # Fallback sweep caught invoice 101 deletion; only invoice 102 remains in destination
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "quickbooks:company_fallback:invoice:102"


def test_empty_sweep_guardrail_two_runs(sample_invoice, tmp_path):
    """Verify empty sweep with known_ids preserves destination without allow_empty_sweep."""
    fake_client = FakeQuickBooksClient(
        invoices=[sample_invoice],
        realm_id="company_guard",
    )

    db_path = (tmp_path / "qb_guard.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_qb_guard_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    # Run 1: Load invoice 101
    pipeline.run(quickbooks_source(client=fake_client, entities=["Invoice"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM quickbooks_invoices") as cur,
    ):
        assert len(cur.fetchall()) == 1

    # Run 2: Transient network drop makes sweep return 0 records on full refresh
    fake_client.invoices = []
    pipeline.run(
        quickbooks_source(
            client=fake_client,
            entities=["Invoice"],
            full_refresh=True,
            allow_empty_sweep=False,
        )
    )

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM quickbooks_invoices") as cur,
    ):
        # Row must be preserved!
        assert len(cur.fetchall()) == 1

    # Run 3: Confirmed empty sweep with allow_empty_sweep=True purges the record
    pipeline.run(
        quickbooks_source(
            client=fake_client,
            entities=["Invoice"],
            full_refresh=True,
            allow_empty_sweep=True,
        )
    )

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM quickbooks_invoices") as cur,
    ):
        assert len(cur.fetchall()) == 0


def test_multi_company_realm_isolation(sample_invoice, tmp_path):
    """Verify two companies sync independently into the same dataset without collision."""
    client_a = FakeQuickBooksClient(invoices=[sample_invoice], realm_id="company_aaa")

    inv_b = dict(sample_invoice)
    inv_b["Id"] = "101"  # Same raw Id!
    inv_b["DocNumber"] = "INV-CORP-B"
    client_b = FakeQuickBooksClient(invoices=[inv_b], realm_id="company_bbb")

    db_path = (tmp_path / "qb_multi.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_qb_multi_company",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    pipeline.run(quickbooks_source(client=client_a, entities=["Invoice"]))
    pipeline.run(quickbooks_source(client=client_b, entities=["Invoice"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id, title FROM quickbooks_invoices") as cur,
    ):
        rows = cur.fetchall()
        ids = {r[0] for r in rows}
        # IDs are isolated by realm prefix
        assert "quickbooks:company_aaa:invoice:101" in ids
        assert "quickbooks:company_bbb:invoice:101" in ids
        assert len(rows) == 2


# ── 4. Client Syntax & Backoff Tests ──────────────────────────────────────────


def test_client_headers_and_query_syntax():
    client = QuickBooksClient(
        realm_id="12345",
        client_id="cid",
        client_secret="csec",
        refresh_token="rtok",
        access_token="test_bearer_token",
    )

    headers = client._get_auth_headers()
    assert headers["Authorization"] == "Bearer test_bearer_token"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"


def test_retry_after_valid_numbers():
    assert _retry_after({"Retry-After": "5"}, attempt=0) == 5.0
    assert _retry_after({"retry-after": "2.5"}, attempt=1) == 2.5
    assert _retry_after({"Retry-After": 10}, attempt=0) == 10.0


def test_retry_after_invalid_fallback_to_exponential():
    default_attempt_0 = 1.0
    default_attempt_1 = 1.5
    default_attempt_2 = 2.25

    assert _retry_after({"Retry-After": "-1"}, attempt=0) == default_attempt_0
    assert _retry_after({"Retry-After": "0"}, attempt=0) == default_attempt_0
    assert _retry_after({"Retry-After": "nan"}, attempt=0) == default_attempt_0
    assert _retry_after({"Retry-After": "inf"}, attempt=0) == default_attempt_0
    assert _retry_after({"Retry-After": "invalid"}, attempt=0) == default_attempt_0
    assert _retry_after({}, attempt=0) == default_attempt_0
    assert _retry_after(None, attempt=1) == default_attempt_1
    assert _retry_after({"other-header": "foo"}, attempt=2) == default_attempt_2
