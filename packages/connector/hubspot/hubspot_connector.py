import httpx
import logging
from datetime import datetime

logger = logging.getLogger("cognee.connectors.hubspot")

class HubSpotConnector:
    def __init__(self, access_token: str):
        """
        Initializes the HubSpot data-source connector using an enterprise
        Private App Access Token.
        """
        self.access_token = access_token
        self.base_url = "https://hubapi.com"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

    def fetch_engagement_notes(self, from_date: str = None) -> list:
        """
        Ingests client engagement notes and business interactions
        to parse into Cognee's long-term memory graph layer.
        """
        endpoint = f"{self.base_url}/objects/notes"
        params = {"limit": 50}
        
        try:
            with httpx.Client() as client:
                response = client.get(endpoint, headers=self.headers, params=params)
                if response.status_code == 200:
                    data = response.json()
                    results = data.get("results", [])
                    
                    # Incremental sync: filter by fromDate if specified
                    if from_date:
                        parsed_date = datetime.strptime(from_date, "%Y-%m-%d")
                        return [note for note in results if datetime.strptime(note["createdAt"][:10], "%Y-%m-%d") >= parsed_date]
                    return results
                
                logger.error(f"HubSpot API returned error status: {response.status_code}")
                return []
        except Exception as e:
            logger.error(f"Failed to stream data-source from HubSpot: {e}")
            return []
