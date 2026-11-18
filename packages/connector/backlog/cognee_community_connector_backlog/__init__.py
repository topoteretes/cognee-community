"""Cognee Community Backlog (Nulab) Data-Source Connector.

Provides pulled dlt ingestion for Nulab Backlog (`backlog.com` and `backlog.jp`) issues,
ordered comment threads, and document pages with incremental `updatedSince` cursors,
forget-on-delete `_deleted` tombstones, rate-limit backoff, PII email stripping,
and API key redaction.
"""

from .backlog import (
    DOCUMENT_SOURCE_ATTR,
    BacklogComment,
    BacklogDocumentPage,
    BacklogIssueRecord,
    BacklogRateLimitState,
    backlog_source,
    parse_backlog_document,
    parse_backlog_issue,
    parse_rate_limit_headers,
    redact_api_key,
    sanitize_user_display_name,
    sync_backlog_projects,
    validate_space_url,
)

__all__ = [
    "DOCUMENT_SOURCE_ATTR",
    "BacklogComment",
    "BacklogDocumentPage",
    "BacklogIssueRecord",
    "BacklogRateLimitState",
    "backlog_source",
    "parse_backlog_document",
    "parse_backlog_issue",
    "parse_rate_limit_headers",
    "redact_api_key",
    "sanitize_user_display_name",
    "sync_backlog_projects",
    "validate_space_url",
]
