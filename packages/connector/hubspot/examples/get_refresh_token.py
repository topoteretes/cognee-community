"""Get a HubSpot OAuth refresh token for the connector, once.

Runs the authorization-code flow for your HubSpot app: a browser opens, you pick
the account and approve the read scopes, then paste back the address the browser
lands on. The refresh token is saved to a private file and never printed.

Before you run it, on your app's Auth page:
  - set the redirect URL to http://localhost:3000 (or set HUBSPOT_REDIRECT_URI)
  - add the scopes below (or set HUBSPOT_SCOPES to the space-separated list your app has)

    uv run python examples/get_refresh_token.py

It asks for the client ID and the client secret (hidden). Then:

    HUBSPOT_CLIENT_ID=<client id>
    HUBSPOT_CLIENT_SECRET=<client secret>
    HUBSPOT_REFRESH_TOKEN=<contents of the saved file>

Treat the refresh token like a password: never commit or share it. Revoke it by
uninstalling the app from the account (Settings > Integrations > Connected apps).
"""

import getpass
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# Read-only. The scopes in the request must match the ones on the app's Auth page.
SCOPES = [
    "oauth",
    "crm.objects.contacts.read",
    "crm.objects.companies.read",
    "crm.objects.deals.read",
    "crm.objects.tickets.read",
    "crm.schemas.contacts.read",
    "crm.schemas.companies.read",
    "crm.schemas.deals.read",
    "crm.schemas.tickets.read",
    "sales-email-read",
]
REDIRECT = os.environ.get("HUBSPOT_REDIRECT_URI", "http://localhost:3000")
TOKEN_FILE = os.path.normpath(
    os.path.expanduser(
        os.environ.get("HUBSPOT_REFRESH_TOKEN_FILE", "~/.cognee_hubspot_refresh_token")
    )
)


def main():
    scopes = os.environ.get("HUBSPOT_SCOPES") or " ".join(SCOPES)
    client_id = os.environ.get("HUBSPOT_CLIENT_ID") or input("Client ID: ").strip()
    client_secret = getpass.getpass("Client secret (hidden): ").strip()

    url = "https://app.hubspot.com/oauth/authorize?" + urllib.parse.urlencode(
        {"client_id": client_id, "redirect_uri": REDIRECT, "scope": scopes}
    )
    print("\n1. Approve access in the browser (choose the account to sync).")
    print("   If it does not open, visit:\n   " + url)
    webbrowser.open(url)
    print("2. The browser then lands on a localhost page that does not load. That is expected.")
    pasted = input("   Paste its full address here: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [pasted])[0]

    request = urllib.request.Request(
        "https://api.hubapi.com/oauth/v1/token",
        data=urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": REDIRECT,
                "code": code,
            }
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            refresh_token = json.load(response)["refresh_token"]
    except urllib.error.HTTPError as error:
        # HubSpot's error body names the problem (wrong secret, redirect or scope)
        # and holds no token.
        raise SystemExit(
            f"HubSpot refused the code: HTTP {error.code} {error.read()[:300]!r}"
        ) from None

    # Written private (0600 where the OS supports it), since it grants read access.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w") as f:
        f.write(refresh_token)
    print(f"\nDone. Refresh token saved to {TOKEN_FILE} (not printed).")
    print("Set HUBSPOT_REFRESH_TOKEN from that file, plus HUBSPOT_CLIENT_ID and")
    print("HUBSPOT_CLIENT_SECRET, and run examples/example.py.")


if __name__ == "__main__":
    main()
