import os
import dlt
import requests

def intercom_source(access_token: str = None, endpoints: list = None):
    """
    Intercom dlt source to extract support data (contacts, conversations, etc).
    """
    if access_token is None:
        access_token = os.environ.get("INTERCOM_ACCESS_TOKEN")

    if not access_token:
        raise ValueError("Please set INTERCOM_ACCESS_TOKEN environment variable or pass it to the source.")

    endpoints = endpoints or ["contacts", "conversations"]

    @dlt.resource(name="intercom_data", write_disposition="replace")
    def intercom_resource():
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Intercom-Version": "2.11",
            "Accept": "application/json"
        }
        
        for endpoint in endpoints:
            url = f"https://api.intercom.io/{endpoint}"
            
            while url:
                response = requests.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    
                    # Intercom API returns lists under keys named like the endpoint (e.g., 'contacts', 'conversations')
                    # If not found, fallback to 'data'
                    items = data.get(endpoint, data.get("data", []))
                    
                    for item in items:
                        # Convert to cognee document structure
                        name = item.get("name", item.get("title", f"{endpoint}_{item.get('id')}"))
                        text_content = str(item)
                        yield {
                            "id": str(item.get("id")),
                            "name": name,
                            "text": text_content,
                            "_cognee_document_source": "intercom"
                        }
                    
                    pages = data.get("pages", {})
                    url = pages.get("next")
                else:
                    response.raise_for_status()

    return intercom_resource()
