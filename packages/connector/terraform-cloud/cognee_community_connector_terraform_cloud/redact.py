"""The scrubbing step: redact secrets from text before it is ingested.

Every text field the source emits goes through :func:`scrub`. Pattern matching
lowers the risk of a secret reaching memory; it cannot promise to catch every
format, which is why attribute values are never read from plan logs at all.
"""

import re

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# (kind, pattern). Order matters: specific formats run before the generic one.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("terraform_token", re.compile(r"\b[A-Za-z0-9]{14}\.atlasv1\.[A-Za-z0-9_-]{60,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("azure_key", re.compile(r"(?<=AccountKey=)[^;\s\"']+|(?<=SharedAccessKey=)[^;\s\"']+")),
]
# Credentials inside a URL: scheme://user:password@host
_URL_CREDENTIALS = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
# key = value / key: value where the key names a secret. Values in parentheses
# such as "(sensitive value)" or "(known after apply)" are Terraform's own markers.
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key
        |private[_-]?key|client[_-]?secret|connection[_-]?string)[\w.-]*["']?\s*[=:]\s*)
    (["']?)(?!\()(?![\[{])([^"'\s,]{4,})\2
    """
)


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def scrub(text: str) -> tuple[str, int]:
    """Return ``text`` with secrets replaced by ``[REDACTED:<kind>]``, and the count."""
    # Strip color codes first so they cannot split a secret apart.
    text = strip_ansi(text)
    count = 0
    for kind, pattern in _PATTERNS:
        text, found = pattern.subn(f"[REDACTED:{kind}]", text)
        count += found
    text, found = _URL_CREDENTIALS.subn(r"\1[REDACTED:credentials]@", text)
    count += found
    text, found = _SECRET_ASSIGNMENT.subn(
        lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:secret]{m.group(2)}", text
    )
    return text, count + found
