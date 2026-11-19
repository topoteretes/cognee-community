import httpx
import logging

logger = logging.getLogger("cognee.connectors.stripe")

class StripeConnector:
    def __init__(self, api_key: str):
        """
        Initializes the Stripe data-source connector using an enterprise
        restricted API secret key framework.
        """
        self.api_key = api_key
        self.base_url = "https://stripe.com"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}"
        }

    def fetch_recent_invoices(self) -> list:
        """
        Ingests corporate invoice logs and transactional datasets 
        to parse straight into Cognee's long-term memory graph layer.
        """
        try:
            with httpx.Client() as client:
                response = client.get(f"{self.base_url}/invoices", headers=self.headers, timeout=5.0)
                if response.status_code == 200:
                    return response.json().get("data", [])
                logger.error(f"Stripe API returned error status: {response.status_code}")
                return []
        except Exception as e:
            logger.error(f"Failed to stream data-source from Stripe: {e}")
            return []
