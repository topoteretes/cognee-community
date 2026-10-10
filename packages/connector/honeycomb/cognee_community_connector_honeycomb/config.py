"""Configuration models for the Honeycomb Cognee data-source connector."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class HoneycombConfig(BaseModel):
    """Configuration for connecting to the Honeycomb REST API v1.

    Supports Honeycomb US (api.honeycomb.io) and EU (api.eu1.honeycomb.io),
    ingestion of datasets, column definitions, flexible boards, triggers,
    SLOs, burn alerts, and deployment markers, with full snapshot reconciliation
    (write_disposition="replace") and credential redaction.
    """

    api_key: str = Field(
        default_factory=lambda: os.environ.get("HONEYCOMB_API_KEY", ""),
        description="Honeycomb Management / Configuration API Key.",
    )
    base_url: str = Field(
        default_factory=lambda: os.environ.get("HONEYCOMB_BASE_URL", "https://api.honeycomb.io"),
        description="Base URL of Honeycomb API (e.g. https://api.honeycomb.io).",
    )
    dataset_slugs: list[str] | None = Field(
        default=None,
        description=(
            "Optional allowlist of dataset slugs to ingest. If None, all datasets are synced."
        ),
    )
    include_columns: bool = Field(
        default=True,
        description="Whether to ingest dataset column definitions and types.",
    )
    include_boards: bool = Field(
        default=True,
        description="Whether to ingest Honeycomb flexible boards and query definitions.",
    )
    include_triggers: bool = Field(
        default=True,
        description="Whether to ingest alerting triggers and alert thresholds.",
    )
    include_slos: bool = Field(
        default=True,
        description="Whether to ingest Service Level Objectives (SLOs) and burn alerts.",
    )
    include_markers: bool = Field(
        default=True,
        description="Whether to ingest deployment / incident markers.",
    )
    max_retries: int = Field(
        default=5,
        ge=1,
        description="Maximum HTTP retry attempts on 429/5xx status codes.",
    )
    retry_backoff_factor: float = Field(
        default=0.5,
        gt=0.0,
        description="Base multiplier for exponential backoff delays.",
    )
    timeout_seconds: float = Field(
        default=15.0,
        gt=0.0,
        description="HTTP request timeout in seconds.",
    )
    batch_size: int = Field(
        default=50,
        ge=1,
        description="Batch size for document emission.",
    )
    dataset_name: str = Field(
        default="honeycomb_memory",
        min_length=1,
        description="Target Cognee dataset name.",
    )
    write_disposition: Literal["replace", "append", "merge"] = Field(
        default="replace",
        description="dlt write disposition ('replace' enables orphan cleanup on deletion).",
    )

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("base_url must start with 'http://' or 'https://'")
        return cleaned
