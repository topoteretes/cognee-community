import os

from cognee_community_connector_raindrop import raindrop_source

os.environ["RAINDROP_API_TOKEN"] = "<your-token>"

source = raindrop_source(collection_id=None, page_size=50)

# This is a source object for cognee.remember(...)
print(type(source))
