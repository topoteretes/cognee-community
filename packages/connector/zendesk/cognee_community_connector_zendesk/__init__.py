"""Zendesk support tickets and customer context connector for Cognee."""

from .zendesk import ZendeskClient, zendesk_source, zendesk_data

__all__ = [
    "ZendeskClient",
    "zendesk_source",
    "zendesk_data",
]
