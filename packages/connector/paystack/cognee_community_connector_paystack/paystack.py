import hashlib
from typing import List, Dict, Any, Optional
import requests

PAYSTACK_API_BASE_URL = "https://api.paystack.co"

def get_auth_headers(secret_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {secret_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "cognee-paystack-connector/0.1.0"
    }

def fetch_paystack_transactions(
    secret_key: str,
    per_page: int = 50,
    page: int = 1,
    api_base_url: str = PAYSTACK_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches transactions from Paystack API.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/transaction"
    params = {"perPage": per_page, "page": page}

    response = http.get(url, headers=get_auth_headers(secret_key), params=params, timeout=25)
    response.raise_for_status()
    data = response.json()
    if data.get("status") and isinstance(data.get("data"), list):
        return data["data"]
    return []

def fetch_paystack_refunds(
    secret_key: str,
    per_page: int = 50,
    page: int = 1,
    api_base_url: str = PAYSTACK_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches refund records from Paystack API.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/refund"
    params = {"perPage": per_page, "page": page}

    response = http.get(url, headers=get_auth_headers(secret_key), params=params, timeout=25)
    response.raise_for_status()
    data = response.json()
    if data.get("status") and isinstance(data.get("data"), list):
        return data["data"]
    return []

def format_transaction_markdown(tx: Dict[str, Any]) -> str:
    """
    Transforms a Paystack transaction into semantic Markdown.
    """
    tx_id = tx.get("id", "Unknown")
    reference = tx.get("reference", "N/A")
    status = tx.get("status", "unknown")
    amount_kobo = tx.get("amount", 0)
    currency = tx.get("currency", "NGN")
    # Paystack stores amounts in subunit (e.g. kobo / cents)
    amount_major = amount_kobo / 100.0 if isinstance(amount_kobo, (int, float)) else 0.0
    channel = tx.get("channel", "card")
    paid_at = tx.get("paid_at") or tx.get("created_at") or "N/A"
    gateway_response = tx.get("gateway_response", "N/A")
    
    customer = tx.get("customer", {})
    customer_email = customer.get("email", "Unknown Email")
    customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or "Anonymous"

    return f"""# Paystack Transaction: {reference} ({status.upper()})

- **Transaction ID:** {tx_id}
- **Reference:** {reference}
- **Status:** {status}
- **Amount:** {amount_major:,.2f} {currency}
- **Payment Channel:** {channel}
- **Customer Name:** {customer_name}
- **Customer Email:** {customer_email}
- **Gateway Response:** {gateway_response}
- **Timestamp:** {paid_at}
"""

def format_refund_markdown(rf: Dict[str, Any]) -> str:
    """
    Transforms a Paystack refund into semantic Markdown.
    """
    rf_id = rf.get("id", "Unknown")
    tx_ref = rf.get("transaction_reference", "N/A")
    amount = rf.get("amount", 0)
    currency = rf.get("currency", "NGN")
    amount_major = amount / 100.0 if isinstance(amount, (int, float)) else 0.0
    status = rf.get("status", "processed")
    refunded_at = rf.get("refunded_at") or rf.get("created_at") or "N/A"

    return f"""# Paystack Refund: {tx_ref} ({status.upper()})

- **Refund ID:** {rf_id}
- **Transaction Reference:** {tx_ref}
- **Amount Refunded:** {amount_major:,.2f} {currency}
- **Refund Status:** {status}
- **Refunded At:** {refunded_at}
"""

class PaystackConnector:
    """
    Cognee Community Data-Source Connector for Paystack payment gateway.
    """
    def __init__(
        self,
        secret_key: str,
        session: Optional[requests.Session] = None
    ):
        if not secret_key or not secret_key.strip():
            raise ValueError("Paystack secret key (e.g., sk_live_... or sk_test_...) is required.")
        self.secret_key = secret_key.strip()
        self.session = session or requests.Session()

    def fetch_documents(self, page_size: int = 50, include_refunds: bool = True) -> List[Dict[str, Any]]:
        """
        Extracts Paystack transaction & refund records as Cognee documents.
        """
        documents = []

        # 1. Transactions
        try:
            txs = fetch_paystack_transactions(self.secret_key, per_page=page_size, session=self.session)
            for tx in txs:
                tx_id = str(tx.get("id", ""))
                doc_id = hashlib.sha256(f"paystack_tx_{tx_id}".encode()).hexdigest()
                text = format_transaction_markdown(tx)
                documents.append({
                    "id": doc_id,
                    "text": text,
                    "metadata": {
                        "source": "paystack",
                        "type": "transaction",
                        "transaction_id": tx_id,
                        "reference": tx.get("reference"),
                        "status": tx.get("status"),
                        "currency": tx.get("currency"),
                        "channel": tx.get("channel")
                    }
                })
        except Exception:
            pass

        # 2. Refunds
        if include_refunds:
            try:
                refunds = fetch_paystack_refunds(self.secret_key, per_page=page_size, session=self.session)
                for rf in refunds:
                    rf_id = str(rf.get("id", ""))
                    doc_id = hashlib.sha256(f"paystack_refund_{rf_id}".encode()).hexdigest()
                    text = format_refund_markdown(rf)
                    documents.append({
                        "id": doc_id,
                        "text": text,
                        "metadata": {
                            "source": "paystack",
                            "type": "refund",
                            "refund_id": rf_id,
                            "transaction_reference": rf.get("transaction_reference"),
                            "status": rf.get("status")
                        }
                    })
            except Exception:
                pass

        return documents
