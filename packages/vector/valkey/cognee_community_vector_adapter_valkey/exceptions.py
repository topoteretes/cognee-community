"""Exceptions for the Valkey vector adapter."""

from cognee.infrastructure.databases.vector.exceptions import (
    CollectionNotFoundError as CogneeCollectionNotFoundError,
)


class ValkeyVectorEngineInitializationError(Exception):
    """Exception raised when vector engine initialization fails."""

    pass


class CollectionNotFoundError(CogneeCollectionNotFoundError):
    """Collection-not-found error.

    Subclasses cognee's CollectionNotFoundError so core retrieval code
    (which catches the cognee exception to treat missing collections as
    empty results) handles it correctly.
    """

    pass
