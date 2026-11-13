import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_airwallex import (
    AirwallexConnector,
    fetch_airwallex_transfers,
    fetch_airwallex_balances,
    format_transfer_markdown,
    format_balance_markdown
)

MOCK_TRANSFERS = [
    {
        "id": "trf_982347",
        "status": "SETTLED",
        "amount": 14500.50,
        "currency": "USD",
        "reason": "Cloud server infrastructure invoice payment",
        "failure_reason": None,
        "created_at": "2026-10-27T11:20:00Z",
        "beneficiary": {
            "name": "Cloudflare Global Inc.",
            "bank_details": {
                "bank_name": "JPMorgan Chase Bank"
            }
        }
    }
]

MOCK_BALANCES = [
    {
        "currency": "USD",
        "available_amount": 125000.00,
        "pending_amount": 15000.00,
        "reserved_amount": 5000.00
    }
]

def test_format_transfer_markdown():
    tf = MOCK_TRANSFERS[0]
    md = format_transfer_markdown(tf)
    assert "# Airwallex Payout: trf_982347 (SETTLED)" in md
    assert "- **Amount:** 14,500.50 USD" in md
    assert "- **Beneficiary:** Cloudflare Global Inc." in md
    assert "- **Bank Name:** JPMorgan Chase Bank" in md
    assert "- **Transfer Reason:** Cloud server infrastructure invoice payment" in md

def test_format_balance_markdown():
    bal = MOCK_BALANCES[0]
    md = format_balance_markdown(bal)
    assert "# Airwallex Treasury Balance: USD" in md
    assert "- **Available Amount:** 125,000.00 USD" in md
    assert "- **Total Ledger Balance:** 145,000.00 USD" in md

@patch("requests.Session.get")
def test_fetch_airwallex_transfers(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": MOCK_TRANSFERS}
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    transfers = fetch_airwallex_transfers("token_12345")
    assert len(transfers) == 1
    assert transfers[0]["id"] == "trf_982347"

@patch("requests.Session.get")
def test_fetch_airwallex_balances(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": MOCK_BALANCES}
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    balances = fetch_airwallex_balances("token_12345")
    assert len(balances) == 1
    assert balances[0]["currency"] == "USD"

@patch("cognee_community_connector_airwallex.airwallex.fetch_airwallex_transfers")
@patch("cognee_community_connector_airwallex.airwallex.fetch_airwallex_balances")
def test_airwallex_connector_fetch_documents(mock_bal, mock_tf):
    mock_tf.return_value = MOCK_TRANSFERS
    mock_bal.return_value = MOCK_BALANCES

    connector = AirwallexConnector(api_token="token_abc")
    docs = connector.fetch_documents()

    assert len(docs) == 2
    assert docs[0]["metadata"]["type"] == "transfer"
    assert docs[1]["metadata"]["type"] == "balance"
    assert docs[0]["metadata"]["transfer_id"] == "trf_982347"

def test_airwallex_connector_invalid_token():
    with pytest.raises(ValueError):
        AirwallexConnector(api_token="")
