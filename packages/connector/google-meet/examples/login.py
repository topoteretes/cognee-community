"""Sign in to Google Meet once and show the conferences the connector can see.

Runs the OAuth installed-app flow with only read-only scopes: a browser
opens, you approve read access, and the token is cached in
GOOGLE_MEET_TOKEN_PATH (written private, 0600) so later syncs refresh
silently.  Then it prints your recent conferences, to confirm access.

    GOOGLE_MEET_CREDENTIALS_PATH=~/client_secret.json \\
    GOOGLE_MEET_TOKEN_PATH=~/.cognee_google_meet_token.json \\
        python examples/login.py

Treat the token file like a password: never commit or share it.
"""

import os

from cognee_community_connector_google_meet import build_meet_services


def main():
    credentials_path = os.path.expanduser(os.getenv("GOOGLE_MEET_CREDENTIALS_PATH", ""))
    token_path = os.path.expanduser(
        os.getenv("GOOGLE_MEET_TOKEN_PATH", "~/.cognee_google_meet_token.json")
    )
    if not credentials_path:
        raise SystemExit("Set GOOGLE_MEET_CREDENTIALS_PATH to your OAuth client JSON file.")

    services = build_meet_services(credentials_path, token_path, login=True)
    print(f"Signed in. Token cached at {token_path}\n")

    response = services.meet.conferenceRecords().list(pageSize=10).execute()
    records = response.get("conferenceRecords", [])
    if not records:
        print("No conferences yet. Meet only lists meetings you organized, from the last 30 days.")
        return
    print("Your most recent conferences:")
    for record in records:
        status = "ended" if record.get("endTime") else "still running"
        print(f"  {record['startTime']}  {record['space']}  ({status})")


if __name__ == "__main__":
    main()
