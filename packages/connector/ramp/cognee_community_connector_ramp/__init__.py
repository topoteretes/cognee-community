"""Ramp data-source connector for cognee."""

from .ramp import (
    RAMP_SOURCE_NAME,
    RAMP_TABLE_NAME,
    RampClient,
    ramp_source,
)

__all__ = [
    "RAMP_SOURCE_NAME",
    "RAMP_TABLE_NAME",
    "RampClient",
    "ramp_source",
]
