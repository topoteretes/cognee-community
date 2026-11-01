"""
Paystack Connector for Cognee.
Synchronizes payment transactions, refunds, and customer histories from Paystack into Cognee knowledge graphs.
"""

from .paystack import (
    PaystackConnector,
    fetch_paystack_transactions,
    fetch_paystack_refunds,
    format_transaction_markdown,
    format_refund_markdown
)

__all__ = [
    "PaystackConnector",
    "fetch_paystack_transactions",
    "fetch_paystack_refunds",
    "format_transaction_markdown",
    "format_refund_markdown"
]
