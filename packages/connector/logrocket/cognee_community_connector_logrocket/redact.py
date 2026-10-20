"""Conservative redaction for free text emitted by the LogRocket connector."""

from __future__ import annotations

import re

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_IPV4 = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_LOGROCKET_KEY = re.compile(r"\bpat:[^\s:'\"]+:[^\s:'\"]+:[A-Za-z0-9_-]+\b")


def redact_text(value: object, *, limit: int = 20_000) -> str:
    """Return bounded text with common credentials and direct identifiers removed."""
    if not isinstance(value, str):
        return ""
    text = value[:limit]
    text = _LOGROCKET_KEY.sub("[redacted-logrocket-key]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _EMAIL.sub("[redacted-email]", text)
    return _IPV4.sub("[redacted-ip]", text)
