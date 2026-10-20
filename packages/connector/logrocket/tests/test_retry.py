from datetime import UTC, datetime

import httpx2
import pytest

from cognee_community_connector_logrocket.retry import retry_after, retryable


@pytest.mark.parametrize(
    "value,expected",
    [
        ("7", 7),
        ("Wed, 21 Oct 2026 10:00:07 GMT", 7),
        ("Wed, 21 Oct 2026 09:59:00 GMT", 0),
        ("NaN", 0),
        ("-1", 0),
        ("Infinity", 0),
        ("invalid", 0),
        (None, 0),
    ],
)
def test_retry_after(value, expected):
    assert retry_after(value, now=datetime(2026, 10, 21, 10, tzinfo=UTC)) == expected


def test_mixed_group_with_permanent_failure_is_not_retried():
    assert not retryable(
        ExceptionGroup("private", [httpx2.ConnectError("private"), ValueError("private")])
    )
