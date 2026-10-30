import httpx
import logging

logger = logging.getLogger("cognee.connectors.zendesk")

class ZendeskConnector:
    def __init__(self, access_token: str, subdomain: str):
        """
        Initializes the Zendesk data-source connector using an enterprise
        API access token and company subdomain.
        """
        self.access_token = access_token
        self.base_url = f"https://{subdomain}://"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

    def fetch_active_tickets(self) -> list:
        """
        Ingests active customer support ticket streams to parse 
        directly into Cognee's long-term memory graph layer.
        """
        try:
            with httpx.Client() as client:
                response = client.get(f"{self.base_url}/tickets.json", headers=self.headers, timeout=5.0)
                if response.status_code == 200:
                    return response.json().get("tickets", [])
                logger.error(f"Zendesk API returned error status: {response.status_code}")
                return []
        except Exception as e:
            logger.error(f"Failed to stream data-source from Zendesk: {e}")
            return []
