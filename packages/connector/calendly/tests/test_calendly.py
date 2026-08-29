from cognee_community_connector_calendly.calendly import (
    _invitee_to_row,
    iter_calendly_rows,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, params))

        if url not in self.responses:
            raise AssertionError(f"Unexpected URL: {url}")

        response = self.responses[url]

        if isinstance(response, list):
            if not response:
                raise AssertionError(f"No responses remaining for URL: {url}")
            response = response.pop(0)

        return FakeResponse(response)


EVENT = {
    "uri": "https://api.calendly.com/scheduled_events/E1",
    "name": "Architecture Review",
    "event_type": "https://api.calendly.com/event_types/T1",
    "start_time": "2026-08-30T10:00:00Z",
    "end_time": "2026-08-30T10:30:00Z",
}

INVITEE = {
    "uri": "https://api.calendly.com/scheduled_events/E1/invitees/I1",
    "name": "Alice",
    "email": "alice@example.com",
    "status": "active",
    "questions_and_answers": [
        {
            "question": "What do you want to discuss?",
            "answer": "Graph performance and scaling.",
        }
    ],
}


def test_invitee_row_contains_question_context():
    row = _invitee_to_row(INVITEE, EVENT)

    assert row["id"] == INVITEE["uri"]
    assert row["event_name"] == "Architecture Review"
    assert "Graph performance and scaling." in row["text"]
    assert row["data_id"]
    assert row["_deleted"] is False


def test_iter_calendly_rows_uses_incremental_cursor():
    client = FakeClient(
        {
            "https://api.calendly.com/users/me": {
                "resource": {"uri": "https://api.calendly.com/users/U1"}
            },
            "https://api.calendly.com/scheduled_events": [
                {
                    "collection": [EVENT],
                    "pagination": {},
                },
                {
                    "collection": [],
                    "pagination": {},
                },
            ],
            "https://api.calendly.com/scheduled_events/E1/invitees": {
                "collection": [INVITEE],
                "pagination": {},
            },
        }
    )

    cursor = "2026-08-01T00:00:00Z"

    rows = list(
        iter_calendly_rows(
            client,
            token="test-token",
            min_start_time=cursor,
        )
    )

    assert len(rows) == 1

    event_calls = [call for call in client.calls if call[0].endswith("/scheduled_events")]

    assert event_calls[0][1]["min_start_time"] == cursor
    assert event_calls[0][1]["status"] == "active"
    assert event_calls[1][1]["status"] == "canceled"


def test_event_type_selection_filters_events():
    other_event = {
        **EVENT,
        "uri": "https://api.calendly.com/scheduled_events/E2",
        "event_type": "https://api.calendly.com/event_types/T2",
    }

    client = FakeClient(
        {
            "https://api.calendly.com/users/me": {
                "resource": {"uri": "https://api.calendly.com/users/U1"}
            },
            "https://api.calendly.com/scheduled_events": [
                {
                    "collection": [EVENT, other_event],
                    "pagination": {},
                },
                {
                    "collection": [],
                    "pagination": {},
                },
            ],
            "https://api.calendly.com/scheduled_events/E1/invitees": {
                "collection": [INVITEE],
                "pagination": {},
            },
        }
    )

    rows = list(
        iter_calendly_rows(
            client,
            token="test-token",
            event_type_uris=[EVENT["event_type"]],
        )
    )

    assert len(rows) == 1
    assert rows[0]["event_uri"] == EVENT["uri"]


def test_cancelled_event_emits_hard_delete_marker():
    cancelled_event = {
        **EVENT,
        "status": "canceled",
    }

    client = FakeClient(
        {
            "https://api.calendly.com/users/me": {
                "resource": {"uri": "https://api.calendly.com/users/U1"}
            },
            "https://api.calendly.com/scheduled_events": [
                {
                    "collection": [],
                    "pagination": {},
                },
                {
                    "collection": [cancelled_event],
                    "pagination": {},
                },
            ],
            "https://api.calendly.com/scheduled_events/E1/invitees": {
                "collection": [INVITEE],
                "pagination": {},
            },
        }
    )

    rows = list(iter_calendly_rows(client, token="test-token"))

    assert rows == [
        {
            "id": INVITEE["uri"],
            "_deleted": True,
        }
    ]
