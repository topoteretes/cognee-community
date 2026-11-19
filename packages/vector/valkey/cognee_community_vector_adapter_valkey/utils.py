from __future__ import annotations

import json
import struct
from functools import singledispatch
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult

"""
Internal helper function. Not part of the public API.
"""


def _parse_host_port(url: str) -> tuple[str, int]:
    """
    Parse a url and extract the host and port.

    Args:
        url (str): The connection URL, e.g., "valkey://localhost:6379".

    Returns:
        tuple[str, int]: A tuple containing:
            - host (str): The hostname from the URL, defaults to "localhost" if missing.
            - port (int): The port number from the URL, defaults to 6379 if missing.
    """

    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    return host, port


def _to_float32_bytes(vec) -> bytes:
    """
    Convert a sequence of numeric values into a bytes representation using 32-bit floats.

    Args:
        vec (Iterable[float]): A sequence of numbers (e.g., list, tuple) to be converted.

    Returns:
        bytes: A binary representation of the input values packed as consecutive 32-bit floats.

    Notes:
        - Uses `struct.pack` with the format string `"{len(vec)}f"`, which packs all values as
          IEEE 754 single-precision floats.
        - Ensures compatibility with vector databases or embedding engines that require raw
          float32 byte arrays.
    """

    return struct.pack(f"{len(vec)}f", *map(float, vec))


@singledispatch
def _serialize_for_json(obj: Any) -> Any:
    """Convert objects to JSON-serializable format.
    This is default serialization: return the object as-is.

    Args:
        obj: Object to serialize (UUID, dict, list, or any other type).

    Returns:
        JSON-serializable representation of the object.
    """
    return obj


@_serialize_for_json.register
def _(obj: UUID) -> str:
    return str(obj)


@_serialize_for_json.register
def _(obj: dict) -> dict:
    return {k: _serialize_for_json(v) for k, v in obj.items()}


@_serialize_for_json.register
def _(obj: list) -> list:
    return [_serialize_for_json(item) for item in obj]


def _b2s(x: bytes | bytearray | str) -> str:
    """Convert bytes or bytearray to a UTF-8 string if possible,
        otherwise return a string representation.

    Args:
        x (Any): The input value, which may be bytes, bytearray, or any other type.

    Returns:
        Any: A decoded UTF-8 string if `x` is bytes or bytearray; otherwise, returns `x` unchanged.
            If decoding fails, returns the string representation of `x`.
    """

    if isinstance(x, (bytes, bytearray)):
        try:
            return x.decode("utf-8")
        except Exception:
            return str(x)
    return x


def _build_scored_results_from_ft(
    raw: Any,
    *,
    use_key_suffix_when_missing_id: bool = True,
) -> list[ScoredResult]:
    """Build a list of `ScoredResult` objects from raw FT (Full-Text) search response.

    Args:
        raw (Any): The raw response from Valkey's FT search command, expected to be a list or tuple
                   where the second element is a mapping of keys to field dictionaries.
        use_key_suffix_when_missing_id (bool): If True, use the key string as the ID when the `id`
                   field is missing in the response.

    Returns:
        list[ScoredResult]: A list of scored results, each containing:
            - id (str): Extracted from `id` field or fallback to key.
            - payload (dict): Parsed JSON from `payload_data` field, or raw string if malformed.
            - score (float | None): Extracted from `__vector_score` field if present.

    Notes:
        - Handles both byte keys and string keys by decoding them.
        - Gracefully falls back when fields are missing or payload is invalid JSON.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 2 or not isinstance(raw[1], dict):
        return []

    mapping: dict[Any, dict[Any, Any]] = raw[1]  # the { key -> fields } dict
    scored: list[ScoredResult] = []

    for key_bytes, fields in mapping.items():
        key_str = _b2s(key_bytes)

        # Extract id
        raw_id = fields.get(b"id") if b"id" in fields else fields.get("id")
        if raw_id is not None:
            result_id = _b2s(raw_id)
        else:
            result_id = key_str

        # Extract score
        score = (
            fields.get(b"__vector_score")
            if b"__vector_score" in fields
            else fields.get("__vector_score")
        )
        if score is not None:
            score = float(score)

        # Extract and parse payload_data
        payload_raw = (
            fields.get(b"payload_data") if b"payload_data" in fields else fields.get("payload_data")
        )
        payload: dict[str, Any] = {}
        if payload_raw is not None:
            payload_str = _b2s(payload_raw)
            if isinstance(payload_str, str):
                try:
                    obj = json.loads(payload_str)
                    if isinstance(obj, dict):
                        payload = obj
                    else:
                        # If it's not a dict (e.g., list), wrap it
                        payload = {"_payload": obj}
                except json.JSONDecodeError:
                    # Keep the raw string if malformed
                    payload = {"_payload_raw": payload_str}

        scored.append(
            ScoredResult(
                id=result_id,
                payload=payload,
                score=score,
            )
        )

    return scored
