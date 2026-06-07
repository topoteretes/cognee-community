"""Quantization config helpers for the Qdrant adapter.

Reads four optional environment variables:

  QDRANT_QUANTIZATION
      One of: "none" (default), "tq4", "tq2", "tq1.5", "tq1",
      "sq", "bq1", "bq2", "pq".
  QDRANT_QUANTIZATION_ALWAYS_RAM
      "true" (default) or "false". When true, quantized vectors are
      pinned in RAM for fastest search. Safe default because quantized
      data is small (~768 bytes per 1536-dim vector at TQ4).
  QDRANT_QUANTIZATION_RESCORE
      "true" (default) or "false". When true, top-k results are
      re-scored against full-precision vectors.
  QDRANT_QUANTIZATION_OVERSAMPLING
      Float, default "2.0". Multiplier for how many extra candidates
      to pull from the quantized index before rescoring.
"""

import os

from qdrant_client import models


_TQ_BITS = {
    "tq4": models.TurboQuantBitSize.BITS4,
    "tq2": models.TurboQuantBitSize.BITS2,
    "tq1.5": models.TurboQuantBitSize.BITS1_5,
    "tq1": models.TurboQuantBitSize.BITS1,
}


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes")


def _quantization_kind() -> str:
    return os.getenv("QDRANT_QUANTIZATION", "none").lower()


def is_quantization_enabled() -> bool:
    return _quantization_kind() != "none"


def build_quantization_config():
    """Return the quantization config to pass to client.create_collection,
    or None when quantization is disabled."""
    kind = _quantization_kind()
    if kind == "none":
        return None

    always_ram = _flag("QDRANT_QUANTIZATION_ALWAYS_RAM", True)

    if kind in _TQ_BITS:
        return models.TurboQuantization(
            turbo=models.TurboQuantQuantizationConfig(
                bits=_TQ_BITS[kind],
                always_ram=always_ram,
            )
        )

    if kind == "sq":
        return models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8,
                always_ram=always_ram,
            )
        )

    if kind in ("bq1", "bq2"):
        encoding = (
            models.BinaryQuantizationEncoding.ONE_BIT
            if kind == "bq1"
            else models.BinaryQuantizationEncoding.TWO_BITS
        )
        return models.BinaryQuantization(
            binary=models.BinaryQuantizationConfig(
                encoding=encoding,
                always_ram=always_ram,
            )
        )

    if kind == "pq":
        return models.ProductQuantization(
            product=models.ProductQuantizationConfig(
                compression=models.CompressionRatio.X16,
                always_ram=always_ram,
            )
        )

    raise ValueError(
        f"Unknown QDRANT_QUANTIZATION value: {kind!r}. "
        "Expected one of: none, tq4, tq2, tq1.5, tq1, sq, bq1, bq2, pq."
    )


def build_search_params():
    """Return SearchParams with quantization tuning, or None when
    quantization is disabled. Pass into client.query_points and
    client.query_batch as search_params=..."""
    if not is_quantization_enabled():
        return None

    return models.SearchParams(
        quantization=models.QuantizationSearchParams(
            ignore=False,
            rescore=_flag("QDRANT_QUANTIZATION_RESCORE", True),
            oversampling=float(os.getenv("QDRANT_QUANTIZATION_OVERSAMPLING", "2.0")),
        )
    )
