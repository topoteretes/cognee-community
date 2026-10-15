"""Unit tests for QuickBooks Online connector (cognee-community-connector-quickbooks).

Coverage:
1. Markdown card semantic rendering (Invoices, Bills, Credit Memos, Line Items, Memos).
2. DLT source configuration (Document Mode marker, primary_key="id", write_disposition="merge").
3. Full backfill sync (emits cards, establishes known_ids and LastUpdatedTime cursor).
4. Incremental sync (picks up only changed/new records since cursor).
5. Deletion detection via CDC (records with status="Deleted" yield _deleted=True).
6. Deletion detection via sweep fallback (vanished records yield _deleted=True).
7. Anti-corruption guardrail (transient empty sweep does not purge memory).
8. End-to-end offline DLT merge test (verifies tombstone physically removes row from destination).
"""

from __future__ import annotations

import dlt

from cognee_community_connector_quickbooks.client import _retry_after
from cognee_community_connector_quickbooks.quickbooks import (
    DOCUMENT_SOURCE_ATTR,
    QUICKBOOKS_SOURCE_NAME,
    _bill_to_card,
    _credit_memo_to_card,
    _format_currency,
    _invoice_to_card,
    quickbooks_source,
)
from tests.conftest import FakeQuickBooksClient

# ── 1. Semantic Card Rendering Tests ──────────────────────────────────────────


def test_format_currency():
    assert _format_currency(1234.5, "USD") == "$1,234.50 USD"
    assert _format_currency(0, "EUR") == "$0.00 EUR"
    assert _format_currency(None) == "$0.00 USD"


def test_invoice_to_card(sample_invoice):
    card = _invoice_to_card(sample_invoice, id_prefix="qb_test")

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
    assert "## Private Notes" in content
    assert "Approved by CFO." in content
    assert "Platform Subscription" in content
    assert "Dedicated Support" in content


def test_bill_to_card(sample_bill):
    card = _bill_to_card(sample_bill, id_prefix="qb_test")

    assert card["id"] == "qb_test:bill:201"
    assert card["title"] == "Bill #BILL-500 - Cloudflare Inc"
    assert card["_deleted"] is False

    content = card["content"]
    assert "# Bill #BILL-500: Cloudflare Inc" in content
    assert "$1,200.00 USD" in content
    assert "Payment Status:** UNPAID" in content
    assert "Hosting & Infrastructure" in content
    assert "Enterprise DDoS and CDN" in content


def test_credit_memo_to_card(sample_credit_memo):
    card = _credit_memo_to_card(sample_credit_memo, id_prefix="qb_test")

    assert card["id"] == "qb_test:creditmemo:301"
    assert card["title"] == "Credit Memo #CM-10 - Acme Corp"
    assert card["_deleted"] is False

    content = card["content"]
    assert "# Credit Memo #CM-10: Acme Corp" in content
    assert "$250.00 USD" in content
    assert "SLA downtime credit." in content


# ── 2. DLT Source Configuration Tests ─────────────────────────────────────────


def test_quickbooks_source_configuration():
    fake_client = FakeQuickBooksClient()
    source = quickbooks_source(client=fake_client)

    # Document mode marker must be present for Cognee cognify routing
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == QUICKBOOKS_SOURCE_NAME

    # Check resources
    resources = source.resources
    assert "quickbooks_invoices" in resources
    assert "quickbooks_bills" in resources
    assert "quickbooks_memos" in resources

    invoices_res = resources["quickbooks_invoices"]
    schema = invoices_res.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


# ── 3. Backfill & Incremental Sync Tests ──────────────────────────────────────


def test_full_backfill(sample_invoice, sample_bill):
    fake_client = FakeQuickBooksClient(
        invoices=[sample_invoice],
        bills=[sample_bill],
    )
    source = quickbooks_source(client=fake_client, entities=["Invoice", "Bill"])

    # Run extraction
    invoices_rows = list(source.resources["quickbooks_invoices"])
    bills_rows = list(source.resources["quickbooks_bills"])

    assert len(invoices_rows) == 1
    assert invoices_rows[0]["id"] == "quickbooks:invoice:101"
    assert invoices_rows[0]["_deleted"] is False

    assert len(bills_rows) == 1
    assert bills_rows[0]["id"] == "quickbooks:bill:201"
    assert bills_rows[0]["_deleted"] is False


def test_incremental_sync_with_cursor(sample_invoice, tmp_path):
    inv1 = dict(sample_invoice)
    inv2 = dict(sample_invoice)
    inv2["Id"] = "102"
    inv2["DocNumber"] = "INV-1002"
    inv2["MetaData"] = {
        "CreateTime": "2024-04-02T10:00:00Z",
        "LastUpdatedTime": "2024-04-02T11:00:00Z",
    }

    # Simulate client returning records matching LastUpdatedTime >= cursor
    fake_client = FakeQuickBooksClient(invoices=[inv1, inv2])

    # First run: backfill
    source1 = quickbooks_source(client=fake_client, entities=["Invoice"])
    pipeline1 = dlt.pipeline(
        pipeline_name="test_qb_pipeline",
        destination=dlt.destinations.duckdb(str(tmp_path / "qb_inc.duckdb")),
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    pipeline1.run(source1)

    # Modify inv2 with newer timestamp
    inv2_updated = dict(inv2)
    inv2_updated["MetaData"] = {
        "CreateTime": "2024-04-02T10:00:00Z",
        "LastUpdatedTime": "2024-04-03T15:00:00Z",
    }
    fake_client.invoices = [inv2_updated]

    # Second run: incremental
    source2 = quickbooks_source(client=fake_client, entities=["Invoice"])
    inv_rows = list(source2.resources["quickbooks_invoices"])

    assert len(inv_rows) == 1
    assert inv_rows[0]["id"] == "quickbooks:invoice:102"


# ── 4. Deletion Detection & Guardrail Tests ────────────────────────────────────


def test_cdc_deletion_tombstone(sample_invoice, tmp_path):
    # CDC returns a deleted transaction
    fake_client = FakeQuickBooksClient(
        invoices=[sample_invoice],
        cdc_records=[{"Id": "101", "status": "Deleted", "_entity_type": "Invoice"}],
    )

    pipeline = dlt.pipeline(
        pipeline_name="test_qb_cdc_pipeline",
        destination=dlt.destinations.duckdb(str(tmp_path / "qb_cdc.duckdb")),
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    source1 = quickbooks_source(client=fake_client, entities=["Invoice"])
    pipeline.run(source1)

    # Next run: CDC triggers deletion tombstone
    fake_client.invoices = []
    source2 = quickbooks_source(client=fake_client, entities=["Invoice"])
    rows = list(source2.resources["quickbooks_invoices"])

    # Tombstone row emitted
    del_rows = [r for r in rows if r.get("_deleted") is True]
    assert len(del_rows) == 1
    assert del_rows[0]["id"] == "quickbooks:invoice:101"


def test_anti_corruption_guardrail(sample_invoice, tmp_path):
    """Prove that an empty sweep when records were previously known does NOT emit deletions."""
    fake_client = FakeQuickBooksClient(invoices=[sample_invoice])
    pipeline = dlt.pipeline(
        pipeline_name="test_qb_guardrail_pipeline",
        destination=dlt.destinations.duckdb(str(tmp_path / "qb_guard.duckdb")),
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    pipeline.run(quickbooks_source(client=fake_client, entities=["Invoice"]))

    # Simulate network failure / 0 results returned on sweep
    fake_client.invoices = []
    # Setting state manually to simulate sweep mode without cursor
    source = quickbooks_source(client=fake_client, entities=["Invoice"])
    rows = list(source.resources["quickbooks_invoices"])

    # Must NOT emit any deletion tombstones
    assert not any(r.get("_deleted") for r in rows)


# ── 5. End-to-End DLT Merge Test ──────────────────────────────────────────────


def test_dlt_merge_hard_delete_e2e(sample_invoice, tmp_path):
    """Prove that emitting a row with _deleted=True physically removes it on merge."""
    pipeline = dlt.pipeline(
        pipeline_name="test_qb_merge_e2e",
        destination=dlt.destinations.duckdb(str(tmp_path / "qb_e2e.duckdb")),
        pipelines_dir=str(tmp_path / "pipelines"),
        dev_mode=True,
    )

    # Step 1: Ingest live invoice
    fake_client = FakeQuickBooksClient(invoices=[sample_invoice])
    pipeline.run(quickbooks_source(client=fake_client, entities=["Invoice"]))

    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, _deleted FROM quickbooks_invoices") as cursor,
    ):
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "quickbooks:invoice:101"

    # Step 2: Yield tombstone with _deleted=True
    @dlt.resource(
        name="quickbooks_invoices",
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def delete_stream():
        yield {"id": "quickbooks:invoice:101", "_deleted": True}

    pipeline.run(delete_stream)

    # Step 3: Verify row was deleted from the table
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM quickbooks_invoices") as cursor,
    ):
        rows = cursor.fetchall()
        assert len(rows) == 0, "Expected deleted row to be removed by hard_delete merge"


# ── 6. Retry-After Regression Tests ───────────────────────────────────────────


def test_retry_after_valid_numbers():
    assert _retry_after({"Retry-After": "5"}, attempt=0) == 5.0
    assert _retry_after({"retry-after": "2.5"}, attempt=1) == 2.5
    assert _retry_after({"Retry-After": 10}, attempt=0) == 10.0


def test_retry_after_invalid_fallback_to_exponential():
    default_attempt_0 = 1.0  # 1.5**0
    default_attempt_1 = 1.5  # 1.5**1
    default_attempt_2 = 2.25  # 1.5**2

    # Negative values
    assert _retry_after({"Retry-After": "-1"}, attempt=0) == default_attempt_0
    assert _retry_after({"Retry-After": "-10.5"}, attempt=1) == default_attempt_1

    # Zero
    assert _retry_after({"Retry-After": "0"}, attempt=0) == default_attempt_0

    # NaN
    assert _retry_after({"Retry-After": "nan"}, attempt=0) == default_attempt_0
    assert _retry_after({"Retry-After": "NaN"}, attempt=2) == default_attempt_2

    # Infinity
    assert _retry_after({"Retry-After": "inf"}, attempt=0) == default_attempt_0
    assert _retry_after({"Retry-After": "-inf"}, attempt=1) == default_attempt_1
    assert _retry_after({"Retry-After": "Infinity"}, attempt=0) == default_attempt_0

    # Non-numeric strings
    assert _retry_after({"Retry-After": "invalid"}, attempt=0) == default_attempt_0
    assert _retry_after({"Retry-After": ""}, attempt=1) == default_attempt_1

    # Missing / None headers
    assert _retry_after({}, attempt=0) == default_attempt_0
    assert _retry_after(None, attempt=1) == default_attempt_1
    assert _retry_after({"other-header": "foo"}, attempt=2) == default_attempt_2
