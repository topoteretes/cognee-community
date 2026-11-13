import pytest

from cognee_community_connector_read_ai.models import IncompleteSyncError, Selection
from cognee_community_connector_read_ai.render import normalize_meeting, render_meeting

MID = "01HFYH0A6JM4R7MZ2E6X5T9BNP"
START = 1733800000000


def raw_meeting(**overrides):
    return {
        "id": MID,
        "title": "Project review",
        "start_time_ms": START,
        "end_time_ms": START + 3600000,
        "participants": [{"name": "Ada", "email": "private@example.test"}],
        "summary": "The orbital sail passed the test.",
        "metrics": {"read_score": 0, "nested": {"engagement": 0.75}},
        "transcript": {
            "turns": [
                {
                    "speaker": {"name": "Ada", "email": "speaker@example.test"},
                    "start_time_ms": START + 65005,
                    "end_time_ms": START + 70000,
                    "text": "Launch the sail.",
                }
            ],
            "text": "ignored fallback text",
        },
        "recording_download": "https://example.test/secret.mp4",
        "report_url": "https://example.test/secret?token=secret",
        **overrides,
    }


def test_deterministic_selected_content_and_private_metadata_exclusion():
    normalized = normalize_meeting(raw_meeting(), Selection())
    row = render_meeting(normalized)
    text = row["content"]
    assert text.startswith("## Summary\nThe orbital sail passed the test.")
    assert "[00:01:05.005] Ada: Launch the sail." in text
    assert "- read_score: 0" in text
    assert "- nested.engagement: 0.75" in text
    assert "Started: 2024-12-10T03:06:40Z" in text
    assert "private@example" not in text and "speaker@example" not in text
    assert "secret" not in text and "ignored fallback" not in text
    assert row["_deleted"] is False
    assert render_meeting(normalize_meeting(raw_meeting(), Selection())) == row


@pytest.mark.parametrize("empty", [None, "", "  "])
def test_summary_missing_null_empty_is_unavailable(empty):
    selection = Selection(include_transcript=False, include_metrics=False)
    meeting = normalize_meeting(raw_meeting(summary=empty), selection)
    assert meeting["sections"]["summary"] is None
    assert meeting["availability"]["summary"] == "unavailable"
    assert render_meeting(meeting) is None


def test_section_projection_replaces_metrics_and_null_nested_metrics_disappear():
    selection = Selection(include_summary=False, include_transcript=False)
    meeting = normalize_meeting(
        raw_meeting(metrics={"read_score": 0, "engagement": None}), selection
    )
    assert meeting["sections"]["metrics"] == {"read_score": 0}
    assert "engagement" not in render_meeting(meeting)["content"]
    assert meeting["availability"]["summary"] == "unselected"


def test_active_meeting_is_pending_without_rendering_enrichments():
    meeting = normalize_meeting(raw_meeting(end_time_ms=None), Selection())
    assert meeting["active"]
    assert set(meeting["availability"].values()) == {"unavailable"}
    assert render_meeting(meeting) is None


def test_text_fallback_when_no_transcript_turns():
    selection = Selection(include_summary=False, include_metrics=False)
    meeting = normalize_meeting(raw_meeting(transcript={"text": "Ada: launch"}), selection)
    assert "Ada: launch" in render_meeting(meeting)["content"]


@pytest.mark.parametrize(
    "override",
    [
        {"id": "../../another-path"},
        {"start_time_ms": True},
        {"end_time_ms": START - 1},
        {"participants": ["Ada"]},
        {"summary": {}},
        {"metrics": "bad"},
        {"metrics": {"value": float("nan")}},
        {"metrics": {"value": "email@example.test"}},
        {"metrics": {"value": True}},
        {"transcript": []},
        {"transcript": {"turns": "bad"}},
        {"transcript": {"turns": [{"text": "hello", "start_time_ms": START - 1}]}},
        {"transcript": {"turns": [{"text": {}, "start_time_ms": START}]}},
    ],
)
def test_malformed_sections_are_not_authoritative_clears(override):
    with pytest.raises(IncompleteSyncError):
        normalize_meeting(raw_meeting(**override), Selection())


def test_unselected_fields_do_not_affect_documents():
    selection = Selection(include_transcript=False, include_metrics=False)
    meeting = normalize_meeting(raw_meeting(metrics="ignored", transcript="ignored"), selection)
    row = render_meeting(meeting)
    assert "## Transcript" not in row["content"] and "## Metrics" not in row["content"]
