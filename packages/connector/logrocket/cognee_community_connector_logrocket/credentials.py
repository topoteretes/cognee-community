"""Shared, secret-safe credential resolution for MCP and read-only REST."""

from __future__ import annotations

import os


class CredentialError(RuntimeError):
    """Only a fixed error code may leave credential-provider resolution."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"LogRocket credential resolution failed: {code}; details withheld.")


def resolve_api_key(api_key: str | None, api_key_env: str = "LOGROCKET_API_KEY") -> str:
    """Resolve explicit value, selected environment variable, then DLT secrets.

    DLT's ``sources.logrocket.api_key`` key supports its normal providers, including
    ``SOURCES__LOGROCKET__API_KEY`` and ``.dlt/secrets.toml``. Do not cache secrets,
    log provider errors, or fall through after an invalid higher-priority value.
    """
    value = api_key
    if value is None:
        value = os.environ.get(api_key_env) or None
    if value is None:
        try:
            import dlt

            value = dlt.secrets.get("sources.logrocket.api_key")
        except Exception:
            raise CredentialError("credential_provider_failure") from None
    if value is None:
        raise CredentialError("missing_api_key")
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise CredentialError("invalid_api_key")
    return value
