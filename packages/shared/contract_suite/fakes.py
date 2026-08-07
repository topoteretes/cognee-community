"""Deterministic stand-ins for cognee infrastructure used in offline tests."""

import hashlib


class FakeEmbeddingEngine:
    """Implements cognee's EmbeddingEngine protocol without any network access.

    Vectors are deterministic functions of the input text so tests can assert
    that identical texts embed identically and similar lookups are repeatable.
    """

    def __init__(self, vector_size: int = 8, batch_size: int = 16):
        self.vector_size = vector_size
        self.batch_size = batch_size
        # Some adapters read the underlying model name for collection naming.
        self.model = "fake-embedding-model"

    async def embed_text(self, text: list[str]) -> list[list[float]]:
        return [self._embed_one(item) for item in text]

    def get_vector_size(self) -> int:
        return self.vector_size

    def get_batch_size(self) -> int:
        return self.batch_size

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Repeat the digest as needed, map bytes into [-1, 1].
        needed = self.vector_size
        raw = (digest * ((needed // len(digest)) + 1))[:needed]
        return [(byte - 128) / 128.0 for byte in raw]
