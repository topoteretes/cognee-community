"""Sign in to Google Calendar once and list the calendars you can sync.

Runs the OAuth installed-app flow with only the calendar.readonly scope: a
browser opens, you approve read access, and the token is cached in
GOOGLE_CALENDAR_TOKEN_PATH (written private, 0600) so later syncs refresh
silently.  Then it prints every calendar id, so you can pick calendar_ids.

    GOOGLE_CALENDAR_CREDENTIALS_PATH=~/client_secret.json \\
    GOOGLE_CALENDAR_TOKEN_PATH=~/.cognee_google_calendar_token.json \\
        python examples/login.py

Treat the token file like a password: never commit or share it.
"""

import os

from cognee_community_connector_google_calendar import build_calendar_service, list_calendars


def main():
    credentials_path = os.path.expanduser(os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", ""))
    token_path = os.path.expanduser(
        os.getenv("GOOGLE_CALENDAR_TOKEN_PATH", "~/.cognee_google_calendar_token.json")
    )
    if not credentials_path:
        raise SystemExit("Set GOOGLE_CALENDAR_CREDENTIALS_PATH to your OAuth client JSON file.")

    service = build_calendar_service(credentials_path, token_path)
    print(f"Signed in. Token cached at {token_path}\n")
    print("Calendars you can sync (pass their ids as calendar_ids):")
    for calendar in list_calendars(service):
        marker = "  (primary)" if calendar["primary"] else ""
        print(f"  {calendar['id']}  {calendar['name']}{marker}")


if __name__ == "__main__":
    main()
