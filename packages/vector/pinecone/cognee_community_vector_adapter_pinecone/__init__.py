from .pinecone_adapter import PineconeAdapter


def register():
    """Register the Pinecone adapter with cognee's vector database engine."""
    try:
        import cognee

        cognee.use_vector_adapter("pinecone", PineconeAdapter)
    except ImportError as ie:
        raise ImportError(
            "cognee is not installed. Please install it with: pip install cognee"
        ) from ie


__all__ = ["PineconeAdapter", "register"]
