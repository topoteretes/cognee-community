from cognee.infrastructure.databases.vector import use_vector_adapter

from .pinecone_adapter import PineconeAdapter


def register() -> None:
    """Register the Pinecone adapter under cognee's "pinecone" vector provider.

    Call once before configuring cognee with ``vector_db_provider="pinecone"``.
    Idempotent.
    """
    use_vector_adapter("pinecone", PineconeAdapter)


# Register on import so the documented ``from cognee_community_vector_adapter_pinecone
# import register`` usage keeps working as a side effect, while ``register()`` is also
# callable directly (see __init__.py re-export). Both are idempotent.
register()
