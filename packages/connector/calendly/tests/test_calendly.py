"""Unit tests for the Calendly dlt connector (cognee_community_connector_calendly/calendly.py).

The Calendly API is fully mocked via ``FakeCalendlyClient`` — no live token and no
network access are required, so these run in CI. Coverage:

  - event/invitee rendering (meeting notes + invitee Q&A are the core context)
  - event → row flattening (id/title/content/url, no volatile fields)
  - full backfill yields active events and records the ``last_updated`` cursor
  - incremental re-sync re-fetches only events whose ``updated_at`` advanced
    (e.g. meeting notes added to a past event) and turns canceled / vanished
    events into hard-delete markers (forget-on-delete)
  - pagination across ``list_events`` / ``list_invitees``
  - the dlt resource is wired with merge + id PK + the hard_delete column + the
    document-mode marker
  - a dlt-gated e2e run proves a cancel marker physically removes the row
"""

from types import SimpleNamespace

import pytest

from cognee_community_connector_calendly.calendly import (
    CALENDLY_SOURCE_NAME,
    _event_to_row,
    _event_updated_at,
    _event_uuid,
    _iter_events,
    _iter_invitees,
    _iter_rows,
    _render_event,
    _render_invitee,
    _render_location,
)

USER_URI = "https://api.calendly.com/users/U-TEST"


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _event(
    uuid,
    status="active",
    *,
    name="Intro call",
    start="2026-09-05T10:00:00Z",
    updated_at="2026-09-01T00:00:00Z",
    notes="",
    **extra,
):
    return {
        "uri": f"https://api.calendly.com/scheduled_events/{uuid}",
        "name": name,
        "status": status,
        "start_time": start,
        "end_time": "2026-09-05T10:30:00Z",
        "updated_at": updated_at,
        "meeting_notes_plain": notes,
        **extra,
    }


def _invitee(name, email, qa=None, **extra):
    return {"name": name, "email": email, "questions_and_answers": qa or [], **extra}


class FakeCalendlyClient:
    """Stand-in for CalendlyClient backed by in-memory fixtures.

    Records every ``list_events`` call so tests can assert the ``status`` and
    ``invitee_email`` filters the sync path passes through.
    """

    def __init__(self, events=None, invitees=None):
        self.events = list(events or [])
        self.invitees = invitees or {}
        self.list_calls = []

    def current_user(self):
        return USER_URI

    def list_events(self, *, user_uri, status=None, invitee_email=None, page_token=None, count=100):
        self.list_calls.append(
            {
                "status": status,
                "invitee_email": invitee_email,
                "page_token": page_token,
            }
        )
        matching = [e for e in self.events if e.get("status") == status]
        return matching, None

    def list_invitees(self, event_uuid, *, page_token=None, count=100):
        return self.invitees.get(event_uuid, []), None


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def test_event_uuid_extracts_last_segment():
    assert _event_uuid("https://api.calendly.com/scheduled_events/abc-123") == "abc-123"
    assert _event_uuid("https://api.calendly.com/scheduled_events/abc-123/") == "abc-123"
    assert _event_uuid(None) == ""
    assert _event_uuid("") == ""


def test_event_updated_at_reads_cursor_field():
    assert _event_updated_at(_event("e1", updated_at="2026-09-02T00:00:00Z")) == "2026-09-02T00:00:00Z"
    assert _event_updated_at({}) == ""


def test_render_location_covers_join_url_and_physical():
    assert _render_location({"type": "zoom", "join_url": "https://zoom/x"}) == "zoom: https://zoom/x"
    assert _render_location({"type": "physical", "location": "HQ"}) == "physical: HQ"
    assert _render_location({"type": "zoom"}) == "zoom"
    assert _render_location(None) == ""


def test_render_invitee_includes_question_answers():
    invitee = _invitee(
        "Ada", "ada@example.com", [{"question": "What is your goal?", "answer": "Launch Q4"}]
    )
    rendered = _render_invitee(invitee)

    assert "- Ada <ada@example.com>" in rendered
    assert "Q: What is your goal?" in rendered
    assert "A: Launch Q4" in rendered


def test_render_event_includes_notes_and_invitee_answers():
    event = _event("e1", notes="Discussed the roadmap.")
    invitees = [
        _invitee("Ada", "ada@example.com", [{"question": "Goal?", "answer": "Launch Q4"}]),
        _invitee("Bob", "bob@example.com"),
    ]

    content = _render_event(event, invitees)

    assert "Intro call" in content
    assert "Start: 2026-09-05T10:00:00Z" in content
    assert "Discussed the roadmap." in content
    # Invitee questions & answers — the core context — must be fully captured.
    assert "Ada <ada@example.com>" in content
    assert "Q: Goal?" in content
    assert "A: Launch Q4" in content
    assert "Bob <bob@example.com>" in content


def test_event_to_row_flattens_event_and_omits_volatile_fields():
    row = _event_to_row(_event("e1"), [_invitee("Ada", "ada@example.com")])

    assert row["id"] == "e1"
    assert row["title"] == "Intro call"
    assert row["url"] == "https://api.calendly.com/scheduled_events/e1"
    assert row["_deleted"] is False
    assert "Ada" in row["content"]
    # Only identity/provenance + text are kept — no volatile updated_at, so a
    # metadata-only edit does not churn the content-hash data_id.
    assert "updated_at" not in row


# ---------------------------------------------------------------------------
# Pagination (DB-free)
# ---------------------------------------------------------------------------


def test_iter_events_follows_pagination():
    pages = {
        None: ([{"uri": "u/e1"}], "t1"),
        "t1": ([{"uri": "u/e2"}], None),
    }

    def list_events(*, user_uri, status=None, invitee_email=None, page_token=None):
        return pages[page_token]

    client = SimpleNamespace(list_events=list_events)
    result = list(_iter_events(client, USER_URI, status="active"))
    assert [e["uri"] for e in result] == ["u/e1", "u/e2"]


def test_iter_invitees_follows_pagination():
    pages = {None: ([{"email": "a@x.com"}], "t1"), "t1": ([{"email": "b@x.com"}], None)}

    def list_invitees(event_uuid, *, page_token=None):
        return pages[page_token]

    client = SimpleNamespace(list_invitees=list_invitees)
    result = list(_iter_invitees(client, "e1"))
    assert [i["email"] for i in result] == ["a@x.com", "b@x.com"]


# ---------------------------------------------------------------------------
# Sync strategy: updated_at cursor + forget-on-delete (DB-free)
# ---------------------------------------------------------------------------


def test_backfill_yields_active_events_and_records_cursor():
    client = FakeCalendlyClient(
        events=[_event("e1"), _event("e2"), _event("e3", status="canceled")],
        invitees={"e1": [_invitee("Ada", "ada@example.com")], "e2": []},
    )
    state = {}

    rows = list(_iter_rows(client, state, user_uri=USER_URI))

    assert {r["id"] for r in rows} == {"e1", "e2"}  # canceled not ingested on backfill
    assert all(r["_deleted"] is False for r in rows)
    assert state["known_ids"] == ["e1", "e2"]  # id set recorded for deletion detection
    assert state["last_updated"]  # updated_at cursor recorded for the next run
    # Backfill lists only active events.
    assert [c["status"] for c in client.list_calls] == ["active"]


def test_unchanged_event_is_skipped_and_changed_event_is_reemitted():
    client = FakeCalendlyClient(
        events=[
            _event("e1", updated_at="2026-09-02T00:00:00Z"),  # changed
            _event("e2", updated_at="2026-09-01T00:00:00Z"),  # unchanged
        ],
        invitees={"e1": [_invitee("Ada", "ada@example.com")], "e2": []},
    )
    state = {"known_ids": ["e1", "e2"], "last_updated": "2026-09-01T00:00:00Z"}

    rows = list(_iter_rows(client, state, user_uri=USER_URI))

    # Only the changed event is re-emitted; the unchanged one is skipped (its row
    # persists under merge). No tombstones.
    assert [r["id"] for r in rows] == ["e1"]
    assert all(r["_deleted"] is False for r in rows)
    assert state["last_updated"] == "2026-09-02T00:00:00Z"


def test_meeting_notes_added_to_past_event_are_reemitted():
    # The event's start_time is in the PAST, but a post-hoc notes edit bumps
    # updated_at — the old min_start_time window could never see this.
    client = FakeCalendlyClient(
        events=[
            _event(
                "e1",
                start="2026-08-01T10:00:00Z",  # already happened
                updated_at="2026-09-02T00:00:00Z",  # notes edited after the event
                notes="Discussed the roadmap.",
            ),
        ],
        invitees={"e1": [_invitee("Ada", "ada@example.com")]},
    )
    state = {"known_ids": ["e1"], "last_updated": "2026-08-31T00:00:00Z"}

    rows = list(_iter_rows(client, state, user_uri=USER_URI))

    assert [r["id"] for r in rows] == ["e1"]
    assert "Discussed the roadmap." in rows[0]["content"]


def test_canceled_event_is_tombstoned():
    client = FakeCalendlyClient(
        events=[_event("e2")],  # e1 canceled upstream → absent from the active sweep
        invitees={"e2": []},
    )
    state = {"known_ids": ["e1", "e2"], "last_updated": "2026-09-01T00:00:00Z"}

    rows = list(_iter_rows(client, state, user_uri=USER_URI))
    by_id = {r["id"]: r for r in rows}

    assert by_id["e1"] == {"id": "e1", "_deleted": True}  # cancellation → tombstone
    assert state["known_ids"] == ["e2"]


def test_vanished_event_is_tombstoned():
    # An event that disappears from Calendly entirely (not just canceled) must be
    # forgotten too — merge + a hard_delete hint alone could never flag it, because
    # a vanished event is never yielded. The id-difference sweep catches it.
    client = FakeCalendlyClient(events=[_event("e2")], invitees={"e2": []})
    state = {"known_ids": ["e1", "e2"], "last_updated": "2026-09-01T00:00:00Z"}

    rows = list(_iter_rows(client, state, user_uri=USER_URI))

    assert [r["id"] for r in rows] == ["e1"]
    assert rows[0]["_deleted"] is True


def test_new_event_with_old_timestamp_is_fetched():
    # An event absent from known_ids is fetched regardless of its updated_at, so an
    # event that arrives with a timestamp at/below the cursor boundary is not lost.
    client = FakeCalendlyClient(
        events=[
            _event("e1", updated_at="2026-09-01T00:00:00Z"),  # unchanged, skipped
            _event("e3", updated_at="2026-08-15T00:00:00Z"),  # new, older than cursor
        ],
        invitees={"e1": [], "e3": []},
    )
    state = {"known_ids": ["e1"], "last_updated": "2026-09-01T00:00:00Z"}

    rows = list(_iter_rows(client, state, user_uri=USER_URI))

    assert [r["id"] for r in rows] == ["e3"]
    assert all(r["_deleted"] is False for r in rows)


def test_invitee_email_scope_is_passed_through():
    client = FakeCalendlyClient(events=[_event("e1")], invitees={"e1": []})
    state = {}

    list(_iter_rows(client, state, user_uri=USER_URI, invitee_email="ada@example.com"))

    # The active sweep carries the invitee_email filter.
    assert all(c["invitee_email"] == "ada@example.com" for c in client.list_calls)


# ---------------------------------------------------------------------------
# calendly_source (dlt wiring) — requires dlt
# ---------------------------------------------------------------------------


def test_calendly_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    from cognee_community_connector_calendly.calendly import calendly_source

    resource = calendly_source(client=FakeCalendlyClient())

    assert resource.name == "calendly_events"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_calendly_source_declares_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_calendly.calendly import calendly_source

    source = calendly_source(client=FakeCalendlyClient())
    assert CALENDLY_SOURCE_NAME == "calendly"
    assert document_source_tag(source) == "calendly"


def test_calendly_source_requires_dlt(monkeypatch):
    # Simulate dlt being absent: the factory should raise a helpful ImportError.
    import builtins

    from cognee_community_connector_calendly.calendly import calendly_source

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee\\[calendly\\]"):
        calendly_source(client=object())


def test_e2e_dlt_merge_hard_delete_removes_canceled_event(tmp_path):
    """End-to-end, offline (no LLM): drive calendly_source through a real dlt
    merge and prove a ``_deleted`` marker physically removes the row from the
    destination — which is exactly what cognee's orphan_cleanup reconciles
    against.
    """
    dlt = pytest.importorskip("dlt")

    from cognee_community_connector_calendly.calendly import calendly_source

    pipelines_dir = str(tmp_path / "dlt_pipelines")
    db_path = tmp_path / "calendly.db"

    def sync(client):
        # Same pipeline name + dir on both runs so resource_state (the
        # known_ids / last_updated cursor) persists and run 2 takes the
        # incremental branch.
        pipeline = dlt.pipeline(
            pipeline_name="calendly_e2e_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="calendly_e2e",
            pipelines_dir=pipelines_dir,
        )
        pipeline.run(calendly_source(client=client))
        with pipeline.sql_client() as sql_client:
            rows = sql_client.execute_sql("SELECT id FROM calendly_events ORDER BY id")
        return [row[0] for row in rows]

    # Run 1 — backfill loads both active events and records the cursor.
    backfill = FakeCalendlyClient(
        events=[_event("e1"), _event("e2")],
        invitees={"e1": [_invitee("Ada", "ada@example.com")], "e2": [_invitee("Bob", "bob@example.com")]},
    )
    assert sync(backfill) == ["e1", "e2"]

    # Run 2 — e1 canceled upstream (absent from the active sweep); the hard-delete
    # marker must physically remove it from the destination, leaving only e2.
    incremental = FakeCalendlyClient(
        events=[_event("e2")],
        invitees={"e2": [_invitee("Bob", "bob@example.com")]},
    )
    assert sync(incremental) == ["e2"]
