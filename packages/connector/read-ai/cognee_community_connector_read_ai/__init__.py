"""Read.ai meeting documents and acknowledged local synchronization."""

from .models import (
    AccountMismatchError,
    AuthenticationError,
    BudgetExceededError,
    ConfigurationError,
    IncompleteSyncError,
    ReadAIError,
    SyncLimits,
    SyncResult,
    UnsupportedBackendError,
    VerificationError,
)
from .read_ai import read_ai_source


async def sync_read_ai(source, *, dataset_name, user=None):
    from .sync import sync_read_ai as sync

    return await sync(source, dataset_name=dataset_name, user=user)


async def retire_read_ai(source, *, dataset_name, user=None):
    from .sync import retire_read_ai as retire

    return await retire(source, dataset_name=dataset_name, user=user)


__all__ = [
    "AccountMismatchError",
    "AuthenticationError",
    "BudgetExceededError",
    "ConfigurationError",
    "IncompleteSyncError",
    "ReadAIError",
    "SyncLimits",
    "SyncResult",
    "UnsupportedBackendError",
    "VerificationError",
    "read_ai_source",
    "retire_read_ai",
    "sync_read_ai",
]
