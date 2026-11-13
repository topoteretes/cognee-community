"""Mailgun email delivery, domain reputation & template data-source connector for Cognee."""

from .mailgun import (
    MAILGUN_SOURCE_NAME,
    MAILGUN_TABLE_NAME,
    MailgunAPIError,
    MailgunAuthError,
    MailgunClient,
    MailgunConfig,
    MailgunRateLimitError,
    mailgun_connector,
    mailgun_source,
)

__all__ = [
    "MAILGUN_SOURCE_NAME",
    "MAILGUN_TABLE_NAME",
    "MailgunAPIError",
    "MailgunAuthError",
    "MailgunClient",
    "MailgunConfig",
    "MailgunRateLimitError",
    "mailgun_connector",
    "mailgun_source",
]
