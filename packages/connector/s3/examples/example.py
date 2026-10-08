from cognee_community_connector_s3 import s3_source

source = s3_source(bucket="my-bucket", prefix="docs/")
# Pass source to your Cognee document ingestion workflow.
print(source)
