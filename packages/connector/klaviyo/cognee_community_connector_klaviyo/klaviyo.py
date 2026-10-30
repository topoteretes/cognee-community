import os
import dlt
import requests

def klaviyo_source(api_key: str = None, endpoints: list = None):
    """
    Klaviyo dlt source to extract marketing data.
    """
    if api_key is None:
        api_key = os.environ.get("KLAVIYO_API_KEY")

    if not api_key:
        raise ValueError("Please set KLAVIYO_API_KEY environment variable or pass it to the source.")

    endpoints = endpoints or ["campaigns"]

    @dlt.resource(name="klaviyo_data", write_disposition="replace")
    def klaviyo_resource():
        headers = {
            "Authorization": f"Klaviyo-API-Key {api_key}",
            "revision": "2024-02-15",
            "accept": "application/json"
        }
        
        for endpoint in endpoints:
            url = f"https://a.klaviyo.com/api/{endpoint}/"
            
            while url:
                response = requests.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    for item in data.get("data", []):
                        # Convert to cognee document structure
                        attributes = item.get("attributes", {})
                        name = attributes.get("name", f"{endpoint}_{item.get('id')}")
                        text_content = str(attributes)
                        yield {
                            "id": item.get("id"),
                            "name": name,
                            "text": text_content,
                            "_cognee_document_source": "klaviyo"
                        }
                    
                    links = data.get("links", {})
                    url = links.get("next")
                else:
                    response.raise_for_status()

    return klaviyo_resource()
