"""Example: Use the Calendly connector with Cognee."""

import os
import asyncio
from cognee_community_connector_calendly import calendly_source


async def main():
    # Set your Calendly API token (or set CALENDLY_API_KEY env var)
    os.environ["CALENDLY_API_KEY"] = "your_personal_access_token_here"

    # Create the source
    source = calendly_source(
        # Optional: filter by date range
        # min_start_time="2026-09-01T00:00:00Z",
        # max_start_time="2026-12-31T23:59:59Z",
    )

    # List all events
    for doc in source():
        print(f"Event: {doc['name']}")
        print(f"  Start: {doc['start_time']}")
        print(f"  Status: {doc['status']}")
        print(f"  Attendees: {len(doc['attendees'])}")
        for attendee in doc['attendees']:
            print(f"    - {attendee['name']} ({attendee['email']})")
        print()


if __name__ == "__main__":
    asyncio.run(main())
