import httpx
import logging

logger = logging.getLogger("cognee.connectors.jira")

class JiraConnector:
    def __init__(self, access_token: str, domain: str):
        """
        Initializes the Jira data-source connector using an enterprise
        Atlassian REST API token and company cloud domain.
        """
        self.access_token = access_token
        self.base_url = f"https://{domain}.atlassian.net/rest/api/3"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

    def fetch_assigned_issues(self) -> list:
        """
        Ingests active project tickets and sprint issue metrics 
        to parse straight into Cognee's long-term memory graph layer.
        """
        try:
            with httpx.Client() as client:
                response = client.get(f"{self.base_url}/search", headers=self.headers, timeout=5.0)
                if response.status_code == 200:
                    return response.json().get("issues", [])
                logger.error(f"Jira API returned error status: {response.status_code}")
                return []
        except Exception as e:
            logger.error(f"Failed to stream data-source from Jira: {e}")
            return []
