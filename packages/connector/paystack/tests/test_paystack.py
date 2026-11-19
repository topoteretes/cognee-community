import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_paystack import (
    PaystackConnector,
    fetch_paystack_transactions,
    fetch_paystack_refunds,
    format_transaction_markdown,
    format_refund_markdown
)

MOCK_TRANSACTIONS = [
    {
        "id": 298374829,
        "status": "success",
        "reference": "ref_982347293",
        "amount": 2500000,  # 25,000 NGN
        "currency": "NGN",
        "channel": "card",
        "paid_at": "2026-10-28T14:30:00.000Z",
        "gateway_response": "Successful",
        "customer": {
            "first_name": "Ngozi",
            "last_name": "Eze",
            "email": "ngozi.eze@example.com"
        }
    }
]

MOCK_REFUNDS = [
    {
        "id": 837492,
        "transaction_reference": "ref_982347293",
        "amount": 500000,  # 5,000 NGN
        "currency": "NGN",
        "status": "processed",
        "refunded_at": "2026-10-29T10:00:00.000Z"
    }
]

def test_format_transaction_markdown():
    tx = MOCK_TRANSACTIONS[0]
    md = format_transaction_markdown(tx)
    assert "# Paystack Transaction: ref_982347293 (SUCCESS)" in md
    assert "- **Amount:** 25,000.00 NGN" in md
    assert "- **Customer Name:** Ngozi Eze" in md
    assert "- **Customer Email:** ngozi.eze@example.com" in md
    assert "- **Gateway Response:** Successful" in md

def test_format_refund_markdown():
    rf = MOCK_REFUNDS[0]
    md = format_refund_markdown(rf)
    assert "# Paystack Refund: ref_982347293 (PROCESSED)" in md
    assert "- **Amount Refunded:** 5,000.00 NGN" in md
    assert "- **Transaction Reference:** ref_982347293" in md

@patch("requests.Session.get")
def test_fetch_paystack_transactions(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": True, "data": MOCK_TRANSACTIONS}
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    txs = fetch_paystack_transactions("sk_test_12345")
    assert len(txs) == 1
    assert txs[0]["reference"] == "ref_982347293"

@patch("requests.Session.get")
def test_fetch_paystack_refunds(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": True, "data": MOCK_REFUNDS}
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    refunds = fetch_paystack_refunds("sk_test_12345")
    assert len(refunds) == 1
    assert refunds[0]["amount"] == 500000

@patch("cognee_community_connector_paystack.paystack.fetch_paystack_transactions")
@patch("cognee_community_connector_paystack.paystack.fetch_paystack_refunds")
def test_paystack_connector_fetch_documents(mock_rf, mock_tx):
    mock_tx.return_value = MOCK_TRANSACTIONS
    mock_rf.return_value = MOCK_REFUNDS

    connector = PaystackConnector(secret_key="sk_test_abc123")
    docs = connector.fetch_documents()

    assert len(docs) == 2
    assert docs[0]["metadata"]["type"] == "transaction"
    assert docs[1]["metadata"]["type"] == "refund"
    assert docs[0]["metadata"]["reference"] == "ref_982347293"

def test_paystack_connector_invalid_key():
    with pytest.raises(ValueError):
        PaystackConnector(secret_key="")
