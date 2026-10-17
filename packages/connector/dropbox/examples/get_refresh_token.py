"""Get a Dropbox refresh token for the connector, once.

Uses the OAuth 2 PKCE flow with ``token_access_type="offline"``, so no app
secret is needed and the refresh token does not expire.  Run it, open the link
it prints, click Allow, and paste the code back:

    DROPBOX_APP_KEY=<your app key> python examples/get_refresh_token.py

Then export the printed value as DROPBOX_REFRESH_TOKEN (keep DROPBOX_APP_KEY
set too).  Treat the refresh token like a password: never commit or share it.
"""

import os

from dropbox import DropboxOAuth2FlowNoRedirect

SCOPES = ["files.metadata.read", "files.content.read"]


def main():
    app_key = os.getenv("DROPBOX_APP_KEY") or input("Dropbox app key: ").strip()
    flow = DropboxOAuth2FlowNoRedirect(
        app_key,
        use_pkce=True,
        token_access_type="offline",
        scope=SCOPES,
    )

    print("1. Open this link and click Allow:\n")
    print(f"   {flow.start()}\n")
    code = input("2. Paste the authorization code here: ").strip()
    result = flow.finish(code)

    print("\nDone. Add this to your environment (keep it secret):\n")
    print(f"DROPBOX_REFRESH_TOKEN={result.refresh_token}")


if __name__ == "__main__":
    main()
