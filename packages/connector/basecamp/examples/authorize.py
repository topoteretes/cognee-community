"""One-time Basecamp OAuth helper: get an access token and find your account id.

Basecamp has no personal API keys; tokens come from a Launchpad OAuth app.

1. Register an app at https://launchpad.37signals.com/integrations
   (product: Basecamp 5, redirect URI e.g. http://localhost:8000/callback).
2. Run:

       export BASECAMP_CLIENT_ID="..."
       export BASECAMP_CLIENT_SECRET="..."
       export BASECAMP_REDIRECT_URI="http://localhost:8000/callback"
       export BASECAMP_USER_AGENT="My Basecamp Sync (me@example.com)"
       uv run python examples/authorize.py

3. Open the printed link, click "Yes, I'll allow access", and paste the
   ``code`` value from the address you land on (the page itself may not load).

The tokens are written to a private file (mode 600) instead of being printed.
Access tokens last two weeks; the refresh token lets the connector renew them.
"""

import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://launchpad.37signals.com/authorization/new"
TOKEN_URL = "https://launchpad.37signals.com/authorization/token"
AUTHORIZATION_JSON = "https://launchpad.37signals.com/authorization.json"
TOKEN_FILE = Path(os.getenv("BASECAMP_TOKEN_FILE", "~/.basecamp_tokens.json")).expanduser()


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Set {name} first (see the docstring at the top of this file).")
    return value


def main() -> None:
    client_id = _env("BASECAMP_CLIENT_ID")
    client_secret = _env("BASECAMP_CLIENT_SECRET")
    redirect_uri = _env("BASECAMP_REDIRECT_URI")
    user_agent = _env("BASECAMP_USER_AGENT")

    query = urlencode(
        {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri}
    )
    print("Open this link and allow access:\n")
    print(f"  {AUTHORIZE_URL}?{query}\n")
    code = input("Paste the `code` value from the redirect address: ").strip()
    if not code:
        sys.exit("No code given.")

    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        headers={"User-Agent": user_agent},
        timeout=30,
    )
    if resp.status_code != 200 or "access_token" not in resp.json():
        sys.exit(f"Token exchange failed (HTTP {resp.status_code}). The code may have expired.")
    tokens = resp.json()

    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"\nTokens saved to {TOKEN_FILE} (expires_in={tokens.get('expires_in')}s).")

    auth = httpx.get(
        AUTHORIZATION_JSON,
        headers={"Authorization": f"Bearer {tokens['access_token']}", "User-Agent": user_agent},
        timeout=30,
    ).json()
    print("\nBasecamp accounts this token can use:")
    for account in auth.get("accounts", []):
        if account.get("product") == "bc3":
            print(f"  id={account['id']}  name={account.get('name')}")
    print("\nUse that id as BASECAMP_ACCOUNT_ID.")


if __name__ == "__main__":
    main()
