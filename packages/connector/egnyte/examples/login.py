"""Sign in to Egnyte once and print an access token for the connector.

For "public application" API keys, which use the OAuth authorization code
flow.  A browser opens on your Egnyte domain, you approve access, and Egnyte
redirects to the key's registered redirect URI.  That page may not load (the
default https://localhost/callback has no server behind it); copy its full URL
from the address bar and paste it here.  The script prints the access token
to use as EGNYTE_ACCESS_TOKEN.

    EGNYTE_DOMAIN=acme EGNYTE_API_KEY=... EGNYTE_API_SECRET=... \\
        python examples/login.py

Set EGNYTE_REDIRECT_URI if your key was registered with a different redirect.
Treat the token like a password: never commit or share it.
"""

import os
import secrets
import webbrowser

from cognee_community_connector_egnyte import authorization_url, code_from_redirect, exchange_code
from cognee_community_connector_egnyte.egnyte import DEFAULT_REDIRECT_URI


def main():
    domain = os.getenv("EGNYTE_DOMAIN")
    client_id = os.getenv("EGNYTE_API_KEY")
    client_secret = os.getenv("EGNYTE_API_SECRET")
    if not (domain and client_id and client_secret):
        raise SystemExit("Set EGNYTE_DOMAIN, EGNYTE_API_KEY and EGNYTE_API_SECRET.")
    redirect_uri = os.getenv("EGNYTE_REDIRECT_URI", DEFAULT_REDIRECT_URI)

    state = secrets.token_urlsafe(16)
    url = authorization_url(domain, client_id, state, redirect_uri=redirect_uri)
    print(f"Opening Egnyte to approve access. If no browser opens, visit:\n\n  {url}\n")
    webbrowser.open(url)

    redirect_url = input("After approving, paste the full URL you were redirected to:\n> ")
    code = code_from_redirect(redirect_url, state)
    token = exchange_code(domain, client_id, client_secret, code, redirect_uri=redirect_uri)
    print(f"\nSigned in. Set this as EGNYTE_ACCESS_TOKEN:\n\n  {token}\n")


if __name__ == "__main__":
    main()
