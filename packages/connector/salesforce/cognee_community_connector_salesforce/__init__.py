"""Salesforce data connector for cognee community."""

from .client import SalesforceAPIError, SalesforceAuthError, SalesforceClient
from .salesforce import salesforce_source

__all__ = [
    "SalesforceAPIError",
    "SalesforceAuthError",
    "SalesforceClient",
    "salesforce_source",
]
