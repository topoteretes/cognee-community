import dlt
from dlt.sources.helpers import requests
from typing import Iterator, Any

@dlt.source
def chargebee_source(api_key: str = dlt.secrets.value, site: str = dlt.config.value):
    """
    Chargebee structured data-source connector for Cognee.
    Deliberately omits DOCUMENT_SOURCE_ATTR to route to the relational manifest path.
    """
    return [
        customers(api_key, site),
        subscriptions(api_key, site),
        invoices(api_key, site)
    ]

def _paginate(endpoint_url: str, api_key: str, params: dict) -> Iterator[Any]:
    """Helper to paginate Chargebee API list endpoints."""
    # Chargebee uses HTTP Basic Auth with the API key as the username
    auth = (api_key, '') 
    
    while True:
        response = requests.get(endpoint_url, auth=auth, params=params)
        response.raise_for_status()
        data = response.json()
        
        list_data = data.get("list", [])
        for item in list_data:
            # Chargebee wraps resources (e.g., {"customer": {"id": "123"}})
            # We extract the inner dictionary for a clean schema
            for _, resource_data in item.items():
                yield resource_data
        
        next_offset = data.get("next_offset")
        if not next_offset:
            break
        params["offset"] = next_offset

@dlt.resource(primary_key="id", write_disposition="merge")
def customers(
    api_key: str = dlt.secrets.value, 
    site: str = dlt.config.value, 
    updated_at: dlt.sources.incremental = dlt.sources.incremental("updated_at")
):
    endpoint = f"https://{site}.chargebee.com/api/v2/customers"
    params = {
        "limit": 100,
        "sort_by[asc]": "updated_at",
        "include_deleted": "true"
    }
    if updated_at.last_value:
        params["updated_at[after]"] = updated_at.last_value
        
    for record in _paginate(endpoint, api_key, params):
        if record.get("deleted"):
            yield dlt.mark.make_deleted(record)
        else:
            yield record

@dlt.resource(primary_key="id", write_disposition="merge")
def subscriptions(
    api_key: str = dlt.secrets.value, 
    site: str = dlt.config.value, 
    updated_at: dlt.sources.incremental = dlt.sources.incremental("updated_at")
):
    endpoint = f"https://{site}.chargebee.com/api/v2/subscriptions"
    params = {
        "limit": 100,
        "sort_by[asc]": "updated_at",
        "include_deleted": "true"
    }
    if updated_at.last_value:
        params["updated_at[after]"] = updated_at.last_value
        
    for record in _paginate(endpoint, api_key, params):
        if record.get("deleted"):
            yield dlt.mark.make_deleted(record)
        else:
            yield record

@dlt.resource(primary_key="id", write_disposition="merge")
def invoices(
    api_key: str = dlt.secrets.value, 
    site: str = dlt.config.value, 
    updated_at: dlt.sources.incremental = dlt.sources.incremental("updated_at")
):
    endpoint = f"https://{site}.chargebee.com/api/v2/invoices"
    params = {
        "limit": 100,
        "sort_by[asc]": "updated_at",
        "include_deleted": "true"
    }
    if updated_at.last_value:
        params["updated_at[after]"] = updated_at.last_value
        
    for record in _paginate(endpoint, api_key, params):
        if record.get("deleted"):
            yield dlt.mark.make_deleted(record)
        else:
            yield record