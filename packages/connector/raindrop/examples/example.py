from cognee_community_connector_raindrop import raindrop_source

source = raindrop_source(collection_id=None, page_size=50)

# This is a source object for cognee.remember(...)
print(type(source))
