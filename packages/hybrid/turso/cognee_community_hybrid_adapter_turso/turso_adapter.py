"""
Turso/libSQL hybrid adapter.

Issue #125 defines this adapter as a single backend intended to implement
both cognee's graph and vector database contracts.

This initial scaffold intentionally does not claim interface compatibility
until the concrete libSQL storage implementation is complete.
"""


class TursoAdapter:
    """
    Placeholder for the Turso/libSQL hybrid adapter.

    Planned responsibilities:
    - graph nodes and edges stored in libSQL tables
    - vector embeddings stored using libSQL vector capabilities
    - graph and vector operations exposed through cognee interfaces
    - optional per-dataset isolation using separate embedded database files
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "TursoAdapter is currently a package scaffold; implementation is pending."
        )
