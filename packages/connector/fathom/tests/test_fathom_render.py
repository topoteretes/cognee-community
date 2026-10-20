"""Rendering: what a meeting becomes as a document row. No network, no dlt."""

import json

from fake_fathom import make_meeting

from cognee_community_connector_fathom.fathom import (
    Options,
    action_signature,
    meeting_row,
    render_meeting,
)

ALL = Options(include_transcript=True, include_highlights=True)


def test_default_options_render_summary_and_action_items_but_not_the_transcript():
    text = render_meeting(make_meeting(1), Options())

    assert "We agreed pricing for deal 1." in text
    assert "- [ ] Send the proposal 1 (Alice Johnson)" in text
    assert "Transcript" not in text
    assert "Ten percent off" not in text
    assert "Highlights" not in text


def test_every_section_is_opt_in():
    meeting = make_meeting(1)

    only_summary = render_meeting(meeting, Options(include_action_items=False))
    assert "Action items" not in only_summary
    assert "## Summary" in only_summary

    everything = render_meeting(meeting, ALL)
    assert "[00:05:32] Jane Smith: Ten percent off is fine for 1." in everything
    assert '- Objection: Price - "Too pricey"' in everything


def test_summary_heading_is_not_doubled():
    text = render_meeting(make_meeting(1), Options())
    assert text.count("## Summary") == 1

    plain = make_meeting(2, default_summary={"markdown_formatted": "We shipped it."})
    assert "## Summary\n\nWe shipped it." in render_meeting(plain, Options())


def test_no_email_address_reaches_the_document():
    meeting = make_meeting(1)
    row = meeting_row(meeting, ALL)

    blob = json.dumps(row)
    for email in ("alice.johnson@ourco.com", "jane.smith@acme.com"):
        assert email not in blob
    assert "@" not in blob
    # the company is kept: "what did we agree with Acme" needs acme.com in the text
    assert "Jane Smith (acme.com, external)" in row["content"]
    assert "Alice Johnson (ourco.com)" in row["content"]


def test_the_public_share_link_is_never_stored():
    row = meeting_row(make_meeting(1), ALL)
    assert row["url"] == "https://fathom.video/calls/1"
    assert "share" not in json.dumps(row)


def test_row_has_the_frozen_column_set():
    row = meeting_row(make_meeting(7), Options())

    assert set(row) == {"id", "title", "content", "url", "cognee_node_set", "_deleted"}
    assert row["id"] == "7"
    assert row["_deleted"] is False
    assert row["title"] == "Meeting 7"
    assert row["cognee_node_set"] == ["external", "type:Customer call", "team:Sales"]


def test_reordered_action_items_render_identically():
    first = {"description": "A", "recording_timestamp": "00:01:00", "completed": False}
    second = {"description": "B", "recording_timestamp": "00:02:00", "completed": True}
    one = make_meeting(1, action_items=[first, second])
    two = make_meeting(1, action_items=[second, first])

    assert meeting_row(one, Options()) == meeting_row(two, Options())
    assert action_signature(one["action_items"]) == action_signature(two["action_items"])


def test_completing_an_action_item_changes_text_and_signature():
    open_item = {"description": "Send it", "completed": False, "assignee": {"name": "Ann"}}
    done_item = {**open_item, "completed": True}

    before = make_meeting(1, action_items=[open_item])
    after = make_meeting(1, action_items=[done_item])

    assert "- [ ] Send it (Ann)" in render_meeting(before, Options())
    assert "- [x] Send it (Ann)" in render_meeting(after, Options())
    assert action_signature(before["action_items"]) != action_signature(after["action_items"])


def test_rendering_is_deterministic_and_has_no_clock_in_it():
    meeting = make_meeting(1)
    assert render_meeting(meeting, ALL) == render_meeting(dict(meeting), ALL)


def test_missing_optional_fields_do_not_break_the_row():
    bare = {"recording_id": 5, "created_at": "2026-10-19T00:00:00Z", "title": None}
    row = meeting_row(bare, Options())

    assert row["title"] == "Fathom meeting 5"
    assert row["cognee_node_set"] == []
    assert row["url"] is None


def test_node_sets_follow_internal_calls():
    meeting = make_meeting(1, calendar_invitees_domains_type="only_internal", meeting_type=None)
    assert meeting_row(meeting, Options())["cognee_node_set"] == ["internal", "team:Sales"]


def test_calendar_title_only_shows_when_it_differs():
    same = make_meeting(1, title="Acme pricing", meeting_title="acme PRICING")
    assert "Calendar event" not in render_meeting(same, Options())
    assert "Calendar event: Calendar 1" in render_meeting(make_meeting(1), Options())
