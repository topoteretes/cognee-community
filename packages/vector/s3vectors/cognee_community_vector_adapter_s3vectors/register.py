from .s3vectors_adapter import S3VectorsAdapter

def register():
    from cognee.infrastructure.databases.vector.supported_databases import supported_databases
    supported_databases["s3vectors"] = S3VectorsAdapter

register()
