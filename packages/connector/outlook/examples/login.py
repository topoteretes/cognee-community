"""Sign in to Microsoft Graph once, so later syncs refresh silently.

Runs MSAL's device-code login: it prints a short code, you enter it at
https://microsoft.com/devicelogin and approve the read-only permissions.
The tokens are saved to OUTLOOK_TOKEN_CACHE_PATH (written private, 0600).

    OUTLOOK_CLIENT_ID=<client id> OUTLOOK_TOKEN_CACHE_PATH=~/.cognee_outlook_token_cache.json \\
        python examples/login.py

Treat the cache file like a password: never commit or share it.
"""

import os

from cognee_community_connector_outlook import GraphTokenProvider
from cognee_community_connector_outlook.graph_auth import CALENDARS_READ, MAIL_READ, USER_READ


def main():
    cache_path = os.getenv("OUTLOOK_TOKEN_CACHE_PATH")
    if not cache_path:
        raise SystemExit("Set OUTLOOK_TOKEN_CACHE_PATH to where the token cache should live.")
    provider = GraphTokenProvider(
        client_id=os.getenv("OUTLOOK_CLIENT_ID"),
        scopes=[USER_READ, MAIL_READ, CALENDARS_READ],
        token_cache_path=cache_path,
    )
    provider.login()
    print(f"Signed in. Token cache saved to {cache_path}")


if __name__ == "__main__":
    main()
