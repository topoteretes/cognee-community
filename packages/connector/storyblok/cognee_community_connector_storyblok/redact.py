"""The scrubbing step: redact secrets from text before it is ingested.

Every text field the source emits goes through :func:`scrub`. Story content is
meant to be read, but drafts and internal fields can still hold a pasted key.
Pattern matching lowers that risk; it cannot promise to catch every format.
The same step as the Terraform Cloud and Pulumi connectors'.
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
# key = value / key: value where the key names a secret. A quoted value runs to its
# closing quote, spaces and escaped quotes included; a bare one ends at a space or
# comma. Values in parentheses such as "(sensitive value)" or "(known after apply)"
# are Terraform's own markers. The key around the secret word is bounded, so long
# dotted text cannot make the match quadratic.
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (\b[\w.-]{0,64}?(?:password|passwd|secret|token|api[_-]?key|access[_-]?key
        |private[_-]?key|client[_-]?secret|connection[_-]?string)[\w.-]{0,64}["']?\s*[=:]\s*)
    (?:
        "(?!\()((?:\\.|[^"\\\n]){4,})"
      | '(?!\()((?:\\.|[^'\\\n]){4,})'
      | (?!\()(?![\[{"'])([^"'\s,]{4,})
    )
    """
)


def _redact_assignment(match: re.Match[str]) -> str:
    key, double, single = match.group(1), match.group(2), match.group(3)
    if double is not None:
        return f'{key}"[REDACTED:secret]"'
    if single is not None:
        return f"{key}'[REDACTED:secret]'"
    return f"{key}[REDACTED:secret]"


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
    text, found = _SECRET_ASSIGNMENT.subn(_redact_assignment, text)
    return text, count + found
