"""Run the Evernote OAuth 1.0a flow and cache the access token.

Evernote's EDAM API is authenticated by an OAuth 1.0a token. This script walks the
three-legged flow in a terminal:

    1. request a temporary token;
    2. print the Evernote approval URL — open it and approve the app;
    3. paste back the verification code the page shows.

The resulting token is cached at ``~/.cognee/evernote_token.json`` (override with
``EVERNOTE_TOKEN_PATH``), which is where ``evernote_source()`` looks by default.

Usage:
    uv run python examples/authorize.py

Environment:
    EVERNOTE_CONSUMER_KEY      required (Evernote API key)
    EVERNOTE_CONSUMER_SECRET   optional (public clients)
    EVERNOTE_SANDBOX           set to 1 to use sandbox.evernote.com
    EVERNOTE_TOKEN_PATH        override the cache location

If you already have an Evernote *developer token* for your own account, skip this
script entirely and set EVERNOTE_AUTH_TOKEN instead — EDAM accepts both as the same
opaque token.
"""

import os

from cognee_community_connector_evernote import authorize

# Sandbox has its own API key, separate from production.
SANDBOX = os.environ.get("EVERNOTE_SANDBOX", "").strip().lower() in ("1", "true", "yes")


def main() -> None:
    consumer_key = os.environ.get("EVERNOTE_CONSUMER_KEY")
    if not consumer_key:
        print("Set EVERNOTE_CONSUMER_KEY to your Evernote API key first.")
        print("  Request one at https://dev.evernote.com/portal/manage")
        print(
            f"  (keys are environment-specific — this script targets the "
            f"{'SANDBOX' if SANDBOX else 'PRODUCTION'} host)"
        )
        return

    print(f"Evernote OAuth 1.0a — {'sandbox' if SANDBOX else 'production'}\n")

    token, saved = authorize(
        consumer_key=consumer_key,
        consumer_secret=os.environ.get("EVERNOTE_CONSUMER_SECRET"),
        sandbox=SANDBOX,
    )

    print(f"\nAuthorized. Token cached at:\n  {saved}")
    print(f"Token prefix: {token[:24]}…")
    print("\nNow run the demo:\n  uv run python examples/example.py")


if __name__ == "__main__":
    main()
