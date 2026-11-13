"""One-time Twist OAuth 2.0 helper: get an access token and find your workspace id.

1. Create a general integration at https://twist.com/app_console/create_app and
   set its OAuth redirect URL (for example http://localhost:8000/callback; it
   does not need to be a running server).
2. export TWIST_CLIENT_ID="..." TWIST_CLIENT_SECRET="..."
3. uv run python examples/authorize.py

Open the printed link, allow access, and paste the address you land on (or just
its ``code``). The token response is saved to ~/.twist_tokens.json (mode 600)
and the workspaces it can read are listed. The token itself is never printed.
"""

import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTHORIZE_URL = "https://twist.com/oauth/authorize"
TOKEN_URL = "https://twist.com/oauth/access_token"
# Read-only scopes: everything the connector needs, nothing it doesn't.
SCOPES = "workspaces:read,channels:read,threads:read,comments:read"
TOKENS_FILE = Path.home() / ".twist_tokens.json"


def _parse_code(answer: str, state: str) -> str:
    """Accept the full redirect address, just its query, or the bare code."""
    query = urlparse(answer).query if "://" in answer else answer.lstrip("?")
    if "=" not in query and "&" not in query:
        return query  # the bare code
    if "=" not in query.split("&", 1)[0]:
        # A query pasted from after "code=", like "<code>&state=...".
        query = "code=" + query
    params = parse_qs(query)
    if params.get("state", [state])[0] != state:
        sys.exit("The state in the address does not match. Start again.")
    code = params.get("code", [""])[0]
    if not code:
        sys.exit("No code found in what was pasted.")
    return code


def main() -> None:
    client_id = os.environ.get("TWIST_CLIENT_ID")
    client_secret = os.environ.get("TWIST_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set TWIST_CLIENT_ID and TWIST_CLIENT_SECRET first.")

    state = secrets.token_urlsafe(16)
    query = urlencode({"client_id": client_id, "scope": SCOPES, "state": state})
    print(f"Open this link and allow access:\n\n  {AUTHORIZE_URL}?{query}\n")
    answer = input("Paste the address you were sent to (or the code): ").strip()

    code = _parse_code(answer, state)

    response = httpx.post(
        TOKEN_URL,
        data={"client_id": client_id, "client_secret": client_secret, "code": code},
        timeout=30.0,
    )
    if response.status_code != 200:
        sys.exit(
            f"Token request failed with HTTP {response.status_code}. The code may be wrong, "
            "already used or expired; run this again and use the new link."
        )
    tokens = response.json()
    access_token = tokens.get("access_token")
    if not access_token:
        sys.exit(f"No access_token in the response (fields: {sorted(tokens)}).")

    # Created with mode 600 from the start, so the token is never readable by others.
    fd = os.open(TOKENS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(tokens, handle, indent=2)
    TOKENS_FILE.chmod(0o600)
    print(f"Saved the token response to {TOKENS_FILE} (fields: {sorted(tokens)}).")

    response = httpx.get(
        "https://api.twist.com/api/v3/workspaces/get",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30.0,
    )
    workspaces = response.json() if response.status_code == 200 else None
    if not isinstance(workspaces, list):
        sys.exit(f"Could not list workspaces (HTTP {response.status_code}).")
    print("\nWorkspaces this token can read (use the id as workspace_id):")
    for workspace in workspaces:
        print(f"  {workspace['id']}  {workspace['name']}")


if __name__ == "__main__":
    main()
