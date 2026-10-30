import httpx
import logging

logger = logging.getLogger("cognee.connectors.salesforce")

class SalesforceConnector:
    def __init__(self, access_token: str, instance_url: str):
        """
        Initializes the Salesforce data-source connector using an enterprise
        REST API access token and instance URL.
        """
        self.access_token = access_token
        self.base_url = f"{instance_url}/services/data/v57.0"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}", 
            "Content-Type": "application/json"
        }

    def fetch_account_records(self) -> list:
        """
        Ingests Salesforce account records and communication logs
        to parse into Cognee's long-term memory graph layer.
        """
        try:
            with httpx.Client() as client:
                response = client.get(f"{self.base_url}/sobjects/Account", headers=self.headers, timeout=5.0)
                if response.status_code == 200:
                    return response.json().get("recentItems", [])
                logger.error(f"Salesforce API returned error status: {response.status_code}")
                return []
        except Exception as e:
            logger.error(f"Failed to stream data-source from Salesforce: {e}")
            return []
