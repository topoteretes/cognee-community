"""Rendering a thread and a channel into the text cognee ingests."""

from cognee_community_connector_twist.twist import clean_markdown, render_channel, render_thread

CHANNEL = {"id": 852087, "name": "launch", "description": "Launch planning", "public": True}


def _thread(**extra):
    return {
        "id": 1,
        "title": "Checkout redesign",
        "content": "We need to pick between option A and option B.",
        "creator_name": "Anand",
        "posted_ts": 1_791_665_207,
        "closed": False,
        **extra,
    }


def _comment(index, content, **extra):
    return {
        "obj_index": index,
        "content": content,
        "creator_name": "Anand",
        "posted_ts": 1_791_665_302,
        "deleted": False,
        "system_message": None,
        **extra,
    }


def test_clean_markdown_drops_images_and_keeps_link_text():
    text = (
        "Post in [#General](twist-channel://852083), ask [Ada](twist-mention://2601).\n"
        "![screen.png](https://static.twist.com/x.png?width=1)\n\n\n\n"
        "See [the guide](https://twist.com/help?utm_source=twist)."
    )
    assert clean_markdown(text) == "Post in #General, ask Ada.\n\nSee the guide."
    assert clean_markdown(None) == ""


def test_thread_has_header_body_and_comments_in_order():
    text = render_thread(
        _thread(),
        [_comment(1, "Agreed, option B."), _comment(0, "Option B tested better.")],
        CHANNEL,
        "Anand Test",
    )
    assert text.splitlines()[:4] == [
        "Twist thread: Checkout redesign",
        "Channel: #launch in Anand Test",
        "Started by Anand on 2026-10-10",
        "",
    ]
    assert text.index("Option B tested better.") < text.index("Agreed, option B.")


def test_deleted_and_system_comments_are_not_rendered():
    text = render_thread(
        _thread(),
        [
            _comment(0, "-", deleted=True, deleted_by=962248),
            _comment(1, "Anand moved this thread to #design", system_message={"type": "MOVED"}),
            _comment(2, "Still here."),
        ],
        CHANNEL,
    )
    assert "moved this thread" not in text
    assert "\n-\n" not in text
    assert text.endswith("Still here.")


def test_closed_thread_is_marked_and_attachments_are_named():
    text = render_thread(
        _thread(closed=True, attachments=[{"title": "mockup.png", "url": "https://x/m.png"}]),
        [_comment(0, "", attachments=[{"file_name": "notes.pdf"}])],
        CHANNEL,
    )
    assert "Status: closed" in text
    assert "Attachment: mockup.png (https://x/m.png)" in text
    assert "Attachment: notes.pdf" in text


def test_thread_inbox_state_does_not_change_the_text():
    # is_archived is the user's Inbox "done" flag, not an archive.
    assert render_thread(_thread(is_archived=True), [], CHANNEL) == render_thread(
        _thread(is_archived=False), [], CHANNEL
    )


def test_rendering_is_deterministic():
    comments = [_comment(0, "One."), _comment(1, "Two.")]
    assert render_thread(_thread(), comments, CHANNEL) == render_thread(
        _thread(), list(reversed(comments)), CHANNEL
    )


def test_channel_text():
    assert render_channel(CHANNEL, "Anand Test") == (
        "Twist channel: #launch\n"
        "Workspace: Anand Test\n"
        "Description: Launch planning\n"
        "Visibility: public"
    )
    private = render_channel({"name": "admin", "public": False, "archived": True})
    assert "Visibility: private" in private
    assert "Status: archived" in private
