import hashlib
from typing import List, Dict, Any, Optional
import requests

REVENUECAT_API_BASE_URL = "https://api.revenuecat.com/v1"

def get_auth_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "cognee-revenuecat-connector/0.1.0"
    }

def fetch_subscriber_info(
    api_key: str,
    app_user_id: str,
    api_base_url: str = REVENUECAT_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> Dict[str, Any]:
    """
    Fetches subscriber entitlements and active subscriptions from RevenueCat API.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/subscribers/{app_user_id.strip()}"

    response = http.get(url, headers=get_auth_headers(api_key), timeout=25)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and "subscriber" in data:
        return data["subscriber"]
    return data

def format_subscriber_markdown(app_user_id: str, subscriber: Dict[str, Any]) -> str:
    """
    Transforms a RevenueCat subscriber entity into semantic Markdown.
    """
    original_app_user_id = subscriber.get("original_app_user_id", app_user_id)
    first_seen = subscriber.get("first_seen", "N/A")
    management_url = subscriber.get("management_url", "N/A")
    
    entitlements = subscriber.get("entitlements", {})
    subscriptions = subscriber.get("subscriptions", {})

    active_entitlements = []
    for ent_id, ent_info in entitlements.items():
        expires_date = ent_info.get("expires_date", "Lifetime")
        product_identifier = ent_info.get("product_identifier", "Unknown")
        active_entitlements.append(f"- **{ent_id}:** Product `{product_identifier}` (Expires: {expires_date})")

    active_ent_str = "\n".join(active_entitlements) if active_entitlements else "None (Free Tier)"

    subs_summary = []
    for sub_id, sub_info in subscriptions.items():
        store = sub_info.get("store", "unknown")
        purchase_date = sub_info.get("original_purchase_date", "N/A")
        period_type = sub_info.get("period_type", "normal")
        unsubscribe_detected_at = sub_info.get("unsubscribe_detected_at")
        status = "Cancelled / Churned" if unsubscribe_detected_at else "Active"
        subs_summary.append(
            f"- **Product `{sub_id}`:** Store: {store} | Status: {status} | Period: {period_type} | Purchased: {purchase_date}"
        )

    subs_str = "\n".join(subs_summary) if subs_summary else "No active subscriptions"

    return f"""# RevenueCat Subscriber Profile: {original_app_user_id}

- **App User ID:** {app_user_id}
- **First Seen:** {first_seen}
- **Management URL:** {management_url}

### Active Entitlements
{active_ent_str}

### Subscription History & Stores
{subs_str}
"""

class RevenueCatConnector:
    """
    Cognee Community Data-Source Connector for RevenueCat in-app subscriptions and subscriber data.
    """
    def __init__(
        self,
        api_key: str,
        session: Optional[requests.Session] = None
    ):
        if not api_key or not api_key.strip():
            raise ValueError("RevenueCat Secret API key is required.")
        self.api_key = api_key.strip()
        self.session = session or requests.Session()

    def fetch_documents_for_users(self, app_user_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Extracts subscriber profiles for a list of app user IDs into Cognee documents.
        """
        documents = []

        for uid in app_user_ids:
            if not uid:
                continue
            try:
                sub = fetch_subscriber_info(self.api_key, uid, session=self.session)
                doc_id = hashlib.sha256(f"revenuecat_sub_{uid}".encode()).hexdigest()
                text = format_subscriber_markdown(uid, sub)
                
                entitlements_list = list(sub.get("entitlements", {}).keys())
                has_active_ent = len(entitlements_list) > 0
                
                documents.append({
                    "id": doc_id,
                    "text": text,
                    "metadata": {
                        "source": "revenuecat",
                        "type": "subscriber",
                        "app_user_id": uid,
                        "entitlements": entitlements_list,
                        "has_active_subscription": has_active_ent
                    }
                })
            except Exception:
                pass

        return documents
