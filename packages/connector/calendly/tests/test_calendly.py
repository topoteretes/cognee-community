"""Unit tests for the Calendly connector.

The Calendly REST API is fully mocked via ``FakeCalendlySession`` — no
``requests`` traffic and no live token are required, so these run in CI.
Coverage:

  - rendering: location text, invitee Q&A/notes, event -> document content
  - pagination follows Calendly's bare page_token cursor
  - the min_start_time cursor: first-run lookback, later-run recheck window
  - canceled events emit hard-delete markers and skip fetching invitees
  - an edited/new invitee answer is reflected on the next sync
  - the dlt resource is wired with merge + id PK + the hard_delete column
  - the resource declares the document-source marker (routes through cognify)
  - a real dlt merge removes a canceled event's row (end-to-end forget-on-delete)

The end-to-end "deletion removes from memory" guarantee is provided by
cognee's existing ``orphan_cleanup`` path; here we prove the connector emits
the markers that drive it, and that dlt acts on them.
"""

from datetime import datetime, timezone

import pytest

from cognee_community_connector_calendly.calendly import (
    CalendlySyncConfig,
    _current_user_uri,
    _deleted_row,
    _event_id,
    _event_to_row,
    _invitee_id,
    _location_text,
    _paginate,
    _render_invitee,
    _render_qa,
    calendly_source,
    sync_events,
)

USER_URI = "https://api.calendly.com/users/USER1"


# ---------------------------------------------------------------------------
# Fake Calendly REST API
# ---------------------------------------------------------------------------
def _event(uuid, *, start_time, status="active", name="1:1", invitees_uri=None):
    uri = f"https://api.calendly.com/scheduled_events/{uuid}"
    return {
        "uri": uri,
        "name": name,
        "status": status,
        "start_time": start_time,
        "end_time": start_time,
        "location": {"type": "google_conference", "join_url": "https://meet/xyz"},
    }


def _invitee(uuid, *, email, name=None, status="active", qa=None, event_uuid="e1"):
    return {
        "uri": f"https://api.calendly.com/scheduled_events/{event_uuid}/invitees/{uuid}",
        "email": email,
        "name": name or email,
        "status": status,
        "questions_and_answers": qa or [],
    }


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):  # pragma: no cover - only hit on non-2xx
        raise AssertionError("unexpected error response")


class FakeCalendlySession:
    """Minimal stand-in for a ``requests`` session hitting the Calendly API."""

    def __init__(self, events, invitees_by_event=None, user_uri=USER_URI, page_size=2):
        self.events = events
        self.invitees_by_event = invitees_by_event or {}
        self.user_uri = user_uri
        self.page_size = page_size
        self.calls = []

    def request(self, method, url, params=None, **kwargs):
        params = params or {}
        self.calls.append((method, url, dict(params)))

        if url.endswith("/users/me"):
            return _Resp({"resource": {"uri": self.user_uri}})

        if url.endswith("/scheduled_events"):
            return self._paged(self._filtered_events(params), params)

        m_event_id = url.rsplit("/scheduled_events/", 1)
        if len(m_event_id) == 2 and m_event_id[1].endswith("/invitees"):
            event_uuid = m_event_id[1][: -len("/invitees")]
            return self._paged(self.invitees_by_event.get(event_uuid, []), params)

        raise AssertionError(f"unexpected URL: {url}")

    def _filtered_events(self, params):
        if "page_token" in params:
            return self.events  # filters only apply on the first page's params
        floor = params.get("min_start_time", "")
        events = [e for e in self.events if e["start_time"] >= floor]
        if params.get("organization"):
            pass  # single-tenant fake: nothing to filter against
        elif params.get("user"):
            assert params["user"] == self.user_uri
        return events

    def _paged(self, items, params):
        if "page_token" in params:
            # Fake keeps it simple: a page_token is only ever "page2".
            page_items = items[self.page_size :]
        else:
            page_items = items[: self.page_size]
        next_token = "page2" if len(items) > self.page_size and "page_token" not in params else None
        return _Resp({"collection": page_items, "pagination": {"next_page_token": next_token}})


# ---------------------------------------------------------------------------
# Pure rendering helpers
# ---------------------------------------------------------------------------
def test_event_id_and_invitee_id_read_last_uri_segment():
    assert _event_id({"uri": "https://api.calendly.com/scheduled_events/abc-123"}) == "abc-123"
    assert _invitee_id({"uri": ".../invitees/xyz-9/"}) == "xyz-9"
    assert _event_id({}) == ""


def test_location_text_prefers_join_url_then_falls_back_to_type():
    assert _location_text({"location": {"type": "zoom", "join_url": "https://zoom/x"}}) == (
        "zoom: https://zoom/x"
    )
    assert _location_text({"location": {"type": "physical", "location": "Room 4"}}) == (
        "physical: Room 4"
    )
    assert _location_text({"location": {"type": "ask_invitee"}}) == "ask_invitee"
    assert _location_text({}) == "Not specified"


def test_render_qa_labels_the_notes_question_and_skips_blank_answers():
    qa = [
        {"question": "Please share anything that will help prepare for our meeting.",
         "answer": "Discuss Q3 roadmap", "position": 0},
        {"question": "Phone number?", "answer": "", "position": 1},  # skipped: blank
        {"question": "Company size?", "answer": "50-100", "position": 2},
    ]
    lines = _render_qa(qa)
    assert any(line.startswith("  - Notes: Discuss Q3 roadmap") for line in lines)
    assert any("Company size?" in line and "50-100" in line for line in lines)
    assert not any("Phone number?" in line for line in lines)


def test_render_qa_handles_missing_or_empty():
    assert _render_qa(None) == []
    assert _render_qa([]) == []


def test_render_invitee_includes_cancellation_reason():
    invitee = _invitee("i1", email="a@example.com", status="canceled")
    invitee["cancellation"] = {"reason": "conflict"}
    rendered = _render_invitee(invitee)
    assert "canceled" in rendered
    assert "conflict" in rendered


def test_event_to_row_folds_invitee_qa_into_one_document():
    event = _event("e1", start_time="2026-01-10T10:00:00Z")
    invitee = _invitee(
        "i1",
        email="alice@example.com",
        name="Alice",
        qa=[{"question": "Please share anything that will help.", "answer": "Talk pricing"}],
    )
    row = _event_to_row(event, [invitee])

    assert row["id"] == "e1"
    assert row["status"] == "active"
    assert row["_deleted"] is False
    assert "Alice" in row["content"]
    assert "Talk pricing" in row["content"]  # invitee's note is linked into the event doc
    assert "1:1" in row["content"]


def test_event_to_row_with_no_invitees_still_produces_content():
    event = _event("e1", start_time="2026-01-10T10:00:00Z")
    row = _event_to_row(event, [])
    assert "Invitees" not in row["content"]
    assert row["id"] == "e1"


def test_deleted_row_shape():
    assert _deleted_row("e1") == {"id": "e1", "_deleted": True}


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
def test_paginate_follows_bare_page_token_and_drops_other_params():
    session = FakeCalendlySession(
        events=[
            _event("e1", start_time="2026-01-01T00:00:00Z"),
            _event("e2", start_time="2026-01-02T00:00:00Z"),
            _event("e3", start_time="2026-01-03T00:00:00Z"),
        ],
        page_size=2,
    )
    items = list(
        _paginate(
            session,
            "https://api.calendly.com/scheduled_events",
            {"min_start_time": "2020-01-01T00:00:00Z", "user": USER_URI},
        )
    )
    assert [_event_id(e) for e in items] == ["e1", "e2", "e3"]
    # First call carries the real filters; the second carries ONLY page_token.
    assert session.calls[0][2]["min_start_time"] == "2020-01-01T00:00:00Z"
    assert session.calls[1][2] == {"page_token": "page2"}


def test_current_user_uri_reads_resource_uri():
    session = FakeCalendlySession(events=[])
    assert _current_user_uri(session) == USER_URI


# ---------------------------------------------------------------------------
# sync_events — cursor / forget-on-delete (pure, given a session + state dict)
# ---------------------------------------------------------------------------
def _config(**overrides):
    defaults = dict(
        user_uri=USER_URI,
        organization_uri=None,
        lookback_days=7,
        recheck_window_days=2,
        now=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return CalendlySyncConfig(**defaults)


def test_first_sync_backfills_from_lookback_and_sets_recheck_cursor():
    session = FakeCalendlySession(
        events=[_event("e1", start_time="2026-01-10T00:00:00Z")],
        invitees_by_event={"e1": [_invitee("i1", email="a@example.com", event_uuid="e1")]},
        page_size=50,
    )
    state: dict = {}
    rows = list(sync_events(session, state, _config()))

    assert [r["id"] for r in rows] == ["e1"]
    assert rows[0]["_deleted"] is False
    # First call's min_start_time is now - lookback_days (7 days before 2026-01-15).
    events_call = next(c for c in session.calls if c[1].endswith("/scheduled_events"))
    assert events_call[2]["min_start_time"] == "2026-01-08T12:00:00Z"
    # Cursor advances to now - recheck_window_days (2 days) for the NEXT run.
    assert state["min_start_time"] == "2026-01-13T12:00:00Z"


def test_incremental_sync_uses_stored_cursor_as_floor():
    session = FakeCalendlySession(events=[], page_size=50)
    state = {"min_start_time": "2026-01-14T00:00:00Z"}
    list(sync_events(session, state, _config()))

    events_call = next(c for c in session.calls if c[1].endswith("/scheduled_events"))
    assert events_call[2]["min_start_time"] == "2026-01-14T00:00:00Z"
    # Advances again for the next run regardless of what was found.
    assert state["min_start_time"] == "2026-01-13T12:00:00Z"


def test_canceled_event_emits_hard_delete_and_skips_invitee_fetch():
    session = FakeCalendlySession(
        events=[
            _event("e1", start_time="2026-01-10T00:00:00Z", status="active"),
            _event("e2", start_time="2026-01-11T00:00:00Z", status="canceled"),
        ],
        invitees_by_event={"e1": [_invitee("i1", email="a@example.com", event_uuid="e1")]},
        page_size=50,
    )
    rows = list(sync_events(session, {}, _config()))

    by_id = {r["id"]: r for r in rows}
    assert by_id["e1"]["_deleted"] is False
    assert by_id["e2"] == {"id": "e2", "_deleted": True}
    # No invitees call was made for the canceled event e2.
    assert not any("/scheduled_events/e2/invitees" in call[1] for call in session.calls)


def test_new_invitee_answer_is_reflected_on_resync():
    # Same active event, but the invitee's answer changed between two runs —
    # the connector always re-fetches invitees for events in-window, so the
    # re-synced row must carry the fresh answer, not the stale one.
    session1 = FakeCalendlySession(
        events=[_event("e1", start_time="2026-01-10T00:00:00Z")],
        invitees_by_event={
            "e1": [_invitee("i1", email="a@example.com", event_uuid="e1",
                             qa=[{"question": "Notes", "answer": "v1"}])]
        },
        page_size=50,
    )
    rows1 = list(sync_events(session1, {}, _config()))
    assert "v1" in rows1[0]["content"]

    session2 = FakeCalendlySession(
        events=[_event("e1", start_time="2026-01-10T00:00:00Z")],
        invitees_by_event={
            "e1": [_invitee("i1", email="a@example.com", event_uuid="e1",
                             qa=[{"question": "Notes", "answer": "v2"}])]
        },
        page_size=50,
    )
    rows2 = list(sync_events(session2, {}, _config()))
    assert "v2" in rows2[0]["content"]
    assert "v1" not in rows2[0]["content"]


def test_organization_scope_is_pushed_down_as_a_param():
    session = FakeCalendlySession(events=[], page_size=50)
    list(sync_events(session, {}, _config(user_uri=None, organization_uri="https://api.calendly.com/organizations/ORG1")))
    events_call = next(c for c in session.calls if c[1].endswith("/scheduled_events"))
    assert events_call[2]["organization"] == "https://api.calendly.com/organizations/ORG1"
    assert "user" not in events_call[2]


# ---------------------------------------------------------------------------
# calendly_source — dlt wiring — requires dlt
# ---------------------------------------------------------------------------
def test_calendly_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    resource = calendly_source(session=FakeCalendlySession(events=[]))
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

    from cognee_community_connector_calendly.calendly import CALENDLY_SOURCE_NAME

    resource = calendly_source(session=FakeCalendlySession(events=[]))
    assert CALENDLY_SOURCE_NAME == "calendly"
    assert document_source_tag(resource) == "calendly"


def test_calendly_source_requires_token_or_session():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="Personal Access Token"):
        calendly_source()


def test_calendly_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee\\[calendly\\]"):
        calendly_source(session=FakeCalendlySession(events=[]))


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker
# ---------------------------------------------------------------------------
def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    pipeline = dlt.pipeline(
        pipeline_name="test_calendly_e2e",
        destination=dlt.destinations.duckdb(str(tmp_path / "calendly.duckdb")),
        dataset_name="calendar",
    )

    # Events start in the future (relative to wall-clock "now") so they are
    # always inside the min_start_time window regardless of lookback/recheck
    # settings — there is no upper bound on the query, only a floor.
    def _iso_in(days):
        from datetime import timedelta

        return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace(
            "+00:00", "Z"
        )

    # Sync #1: two live events land in the destination.
    session1 = FakeCalendlySession(
        events=[
            _event("e1", start_time=_iso_in(1)),
            _event("e2", start_time=_iso_in(2)),
        ],
        invitees_by_event={
            "e1": [_invitee("i1", email="a@example.com", event_uuid="e1")],
            "e2": [_invitee("i2", email="b@example.com", event_uuid="e2")],
        },
        page_size=50,
    )
    pipeline.run(calendly_source(session=session1))
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM calendly_events")[0][0] == 2

    # Sync #2: event "e2" canceled upstream, event "e1" unchanged. The
    # connector emits a hard-delete marker for "e2"; dlt's merge removes it.
    session2 = FakeCalendlySession(
        events=[
            _event("e1", start_time=_iso_in(1)),
            _event("e2", start_time=_iso_in(2), status="canceled"),
        ],
        invitees_by_event={"e1": [_invitee("i1", email="a@example.com", event_uuid="e1")]},
        page_size=50,
    )
    pipeline.run(calendly_source(session=session2))
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM calendly_events")
    assert [r[0] for r in rows] == ["e1"]  # event "e2" forgotten, event "e1" retained
