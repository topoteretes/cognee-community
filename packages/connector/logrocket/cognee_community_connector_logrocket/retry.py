"""Shared retry classification and server-directed backoff."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx2

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def leaves(error: BaseException):
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            yield from leaves(child)
    else:
        yield error


def retryable(error: BaseException) -> bool:
    return all(
        (isinstance(item, httpx2.HTTPStatusError) and item.response.status_code in TRANSIENT_STATUS)
        or isinstance(item, (httpx2.TransportError, TimeoutError))
        for item in leaves(error)
    )


def retry_after(value: str | None, *, now: datetime | None = None) -> float:
    if not value:
        return 0
    if value.isascii() and value.isdigit():
        # Huge valid delays should fail the caller's deadline, not overflow.
        return float(value) if len(value) < 15 else float("inf")
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return 0
        return max(0, (date - (now or datetime.now(UTC))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 0


def retry_delay(error: BaseException, attempt: int) -> float:
    return max(
        [float(2**attempt)]
        + [
            retry_after(item.response.headers.get("Retry-After"))
            for item in leaves(error)
            if isinstance(item, httpx2.HTTPStatusError)
        ]
    )
