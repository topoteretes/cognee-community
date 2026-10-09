import os
import dlt
import requests

def calendly_source(personal_access_token: str = None, endpoints: list = None, organization: str = None):
    """
    Calendly dlt source to extract events and types.
    """
    if personal_access_token is None:
        personal_access_token = os.environ.get("CALENDLY_PERSONAL_ACCESS_TOKEN")

    if not personal_access_token:
        raise ValueError("Please set CALENDLY_PERSONAL_ACCESS_TOKEN environment variable or pass it to the source.")

    endpoints = endpoints or ["event_types", "scheduled_events"]

    @dlt.resource(name="calendly_data", write_disposition="replace")
    def calendly_resource():
        headers = {
            "Authorization": f"Bearer {personal_access_token}"
        }

        # User is needed for events query if org not provided
        current_user_url = "https://api.calendly.com/users/me"
        response = requests.get(current_user_url, headers=headers)
        if response.status_code == 200:
            user_data = response.json().get("resource", {})
            org_uri = user_data.get("current_organization")
            user_uri = user_data.get("uri")
        else:
            response.raise_for_status()
            
        org_uri = organization or org_uri
        
        for endpoint in endpoints:
            url = f"https://api.calendly.com/{endpoint}"
            
            # Calendly requires user or organization in some endpoints
            params = {}
            if endpoint == "scheduled_events":
                params["organization"] = org_uri
            elif endpoint == "event_types":
                params["user"] = user_uri

            while url:
                response = requests.get(url, headers=headers, params=params)
                if response.status_code == 200:
                    data = response.json()
                    
                    collection = data.get("collection", [])
                    for item in collection:
                        # Convert to cognee document structure
                        name = item.get("name", f"{endpoint}_{item.get('uri', '').split('/')[-1]}")
                        text_content = str(item)
                        yield {
                            "id": item.get("uri", "").split("/")[-1],
                            "name": name,
                            "text": text_content,
                            "_cognee_document_source": "calendly"
                        }
                    
                    pagination = data.get("pagination", {})
                    url = pagination.get("next_page")
                    params = {} # Clear params for next page as it's included in URL
                else:
                    response.raise_for_status()

    return calendly_resource()
