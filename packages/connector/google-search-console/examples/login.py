"""Sign in to Google once so the connector can read Search Console on your behalf.

Opens your browser on Google's consent screen for the read-only scope
(``webmasters.readonly``) and caches the token, with its refresh token, in a private file.
Later runs of ``example.py`` reuse and renew it without asking again.

Set these first (see the README for the Google Cloud steps):

    export GOOGLE_SEARCH_CONSOLE_CREDENTIALS_PATH=~/client_secret.json   # OAuth "Desktop app"
    export GOOGLE_SEARCH_CONSOLE_TOKEN_PATH=~/.cognee_google_search_console_token.json  # optional

Run:  uv run python examples/login.py
"""

from cognee_community_connector_google_search_console import authorize

if __name__ == "__main__":
    authorize()
    print("Signed in. Now run examples/example.py.")
