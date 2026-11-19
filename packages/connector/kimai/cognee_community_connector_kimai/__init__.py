"""Kimai data-source connector for cognee."""

from .kimai import (
    DOCUMENT_SOURCE_ATTR,
    format_activity_document,
    format_customer_document,
    format_project_document,
    format_timesheet_document,
    kimai_source,
    parse_kimai_records,
)

__all__ = [
    "DOCUMENT_SOURCE_ATTR",
    "format_customer_document",
    "format_project_document",
    "format_activity_document",
    "format_timesheet_document",
    "parse_kimai_records",
    "kimai_source",
]
