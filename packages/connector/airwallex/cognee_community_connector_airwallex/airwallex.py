import hashlib
from typing import List, Dict, Any, Optional
import requests

AIRWALLEX_API_BASE_URL = "https://api.airwallex.com/api/v1"

def get_auth_headers(bearer_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer_token.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "cognee-airwallex-connector/0.1.0"
    }

def fetch_airwallex_transfers(
    bearer_token: str,
    page_size: int = 50,
    page_num: int = 0,
    api_base_url: str = AIRWALLEX_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches international payouts and transfers from Airwallex API.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/transfers"
    params = {"page_size": page_size, "page_num": page_num}

    response = http.get(url, headers=get_auth_headers(bearer_token), params=params, timeout=25)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    elif isinstance(data, list):
        return data
    return []

def fetch_airwallex_balances(
    bearer_token: str,
    api_base_url: str = AIRWALLEX_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches real-time multi-currency treasury balances from Airwallex.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/balances/current"

    response = http.get(url, headers=get_auth_headers(bearer_token), timeout=25)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    elif isinstance(data, list):
        return data
    return []

def format_transfer_markdown(transfer: Dict[str, Any]) -> str:
    """
    Transforms an Airwallex transfer into semantic Markdown.
    """
    transfer_id = transfer.get("id", "Unknown")
    status = transfer.get("status", "unknown")
    amount = transfer.get("amount", 0.0)
    currency = transfer.get("currency", "USD")
    reason = transfer.get("reason", "N/A")
    failure_reason = transfer.get("failure_reason", "None")
    created_at = transfer.get("created_at", "N/A")
    beneficiary = transfer.get("beneficiary", {})
    ben_name = beneficiary.get("name") or beneficiary.get("company_name") or "Unknown Beneficiary"
    bank_name = beneficiary.get("bank_details", {}).get("bank_name", "N/A")

    return f"""# Airwallex Payout: {transfer_id} ({status.upper()})

- **Transfer ID:** {transfer_id}
- **Status:** {status}
- **Amount:** {amount:,.2f} {currency}
- **Beneficiary:** {ben_name}
- **Bank Name:** {bank_name}
- **Transfer Reason:** {reason}
- **Failure Reason:** {failure_reason}
- **Timestamp:** {created_at}
"""

def format_balance_markdown(balance: Dict[str, Any]) -> str:
    """
    Transforms an Airwallex balance entry into semantic Markdown.
    """
    currency = balance.get("currency", "USD")
    available = balance.get("available_amount", 0.0)
    pending = balance.get("pending_amount", 0.0)
    reserved = balance.get("reserved_amount", 0.0)
    total = available + pending + reserved

    return f"""# Airwallex Treasury Balance: {currency}

- **Currency:** {currency}
- **Available Amount:** {available:,.2f} {currency}
- **Pending Amount:** {pending:,.2f} {currency}
- **Reserved Amount:** {reserved:,.2f} {currency}
- **Total Ledger Balance:** {total:,.2f} {currency}
"""

class AirwallexConnector:
    """
    Cognee Community Data-Source Connector for Airwallex treasury & global payments platform.
    """
    def __init__(
        self,
        api_token: str,
        session: Optional[requests.Session] = None
    ):
        if not api_token or not api_token.strip():
            raise ValueError("Airwallex API Bearer token must be provided.")
        self.api_token = api_token.strip()
        self.session = session or requests.Session()

    def fetch_documents(self, page_size: int = 50, include_balances: bool = True) -> List[Dict[str, Any]]:
        """
        Extracts Airwallex transfers and multi-currency balances as Cognee documents.
        """
        documents = []

        # 1. Transfers / Payouts
        try:
            transfers = fetch_airwallex_transfers(self.api_token, page_size=page_size, session=self.session)
            for tf in transfers:
                tf_id = str(tf.get("id", ""))
                doc_id = hashlib.sha256(f"airwallex_transfer_{tf_id}".encode()).hexdigest()
                text = format_transfer_markdown(tf)
                documents.append({
                    "id": doc_id,
                    "text": text,
                    "metadata": {
                        "source": "airwallex",
                        "type": "transfer",
                        "transfer_id": tf_id,
                        "status": tf.get("status"),
                        "currency": tf.get("currency"),
                        "amount": tf.get("amount")
                    }
                })
        except Exception:
            pass

        # 2. Balances
        if include_balances:
            try:
                balances = fetch_airwallex_balances(self.api_token, session=self.session)
                for bal in balances:
                    currency = str(bal.get("currency", ""))
                    doc_id = hashlib.sha256(f"airwallex_balance_{currency}".encode()).hexdigest()
                    text = format_balance_markdown(bal)
                    documents.append({
                        "id": doc_id,
                        "text": text,
                        "metadata": {
                            "source": "airwallex",
                            "type": "balance",
                            "currency": currency,
                            "available_amount": bal.get("available_amount")
                        }
                    })
            except Exception:
                pass

        return documents
