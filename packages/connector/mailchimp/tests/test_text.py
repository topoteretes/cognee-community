"""Email text and document rendering.

``fixtures/archive_draft.html`` is the real ``archive_html`` of a campaign made
in the current Mailchimp builder on a live account, with the account, audience
and campaign ids and the workspace name replaced. ``fixtures/archive_halted.html``
is the same kind of campaign after Mailchimp halted its send.
"""

from pathlib import Path

from cognee_community_connector_mailchimp.mailchimp import (
    email_text,
    render_campaign,
    render_note,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "archive_draft.html").read_text()
# The real archive_html of a campaign whose send Mailchimp halted, ids replaced.
HALTED = (Path(__file__).parent / "fixtures" / "archive_halted.html").read_text()
BODY = "The new checkout flow ships on 15 March. The payments team owns it."
# The plain_text the live account returned right after sending that campaign.
SENT_PLAIN_TEXT = (
    "View this email in your browser (*|ARCHIVE|*)\nLogo\n\n" + BODY + "\n\n\nLogo\n\n"
    "Copyright (C) *|CURRENT_YEAR|* *|LIST:COMPANY|*. All rights reserved.\n"
    "*|IFNOT:ARCHIVE_PAGE|**|LIST:DESCRIPTION|**|END:IF|*\n\nOur mailing address is:\n"
    "*|IFNOT:ARCHIVE_PAGE|**|LIST_ADDRESS|**|END:IF|*\n\n"
    "Want to change how you receive these emails?\n"
    "You can update your preferences (*|UPDATE_PROFILE|*) or unsubscribe (*|UNSUB|*)\n"
    "http://eepurl.com/example"
)


def test_real_draft_html_gives_only_the_authors_text():
    assert email_text({"archive_html": FIXTURE}) == BODY


def test_real_html_after_sending_gives_the_same_text():
    # Live: sending changes the view link to mailchi.mp/<slug>?e=[UNIQID].
    sent = FIXTURE.replace(
        "https://us22.campaign-archive.com/?e=[UNIQID]&amp;u=0000000000000000000000000&amp;id=bbbbbbbbbb",
        "https://mailchi.mp/640acb2fdd4d/spring-launch-checkout-redesign?e=[UNIQID]",
    ).replace(
        "https://us22.campaign-archive.com/?e=[UNIQID]&u=0000000000000000000000000&id=bbbbbbbbbb",
        "https://mailchi.mp/640acb2fdd4d/spring-launch-checkout-redesign?e=[UNIQID]",
    )
    assert "mailchi.mp/640acb2fdd4d" in sent
    assert email_text({"archive_html": sent, "plain_text": SENT_PLAIN_TEXT}) == BODY


def test_archive_link_without_the_uniqid_marker_is_still_dropped():
    html = (
        '<p><a href="https://mailchi.mp/abc/launch">View this email in your browser</a></p>'
        "<p>Real text.</p>"
    )
    assert email_text({"archive_html": html}, ("", "https://mailchi.mp/abc/launch")) == "Real text."


def test_hidden_preheader_footer_and_leftover_merge_tags_are_removed():
    html = (
        '<span style="display:none">Hidden preview\u034f\u200c\u034f</span>'
        "<p>Hello *|FNAME|*, the launch moved.</p>"
        '<div id="templateFooter"><p>Copyright</p></div>'
    )
    assert email_text({"archive_html": html}) == "Hello , the launch moved."


def test_plain_text_is_only_a_fallback_and_is_cleaned():
    assert email_text({"plain_text": SENT_PLAIN_TEXT}) == BODY
    assert email_text({}) == ""


def test_campaign_document_shows_status_and_audience():
    campaign = {
        "status": "sent",
        "send_time": "2026-10-16T10:17:44+00:00",
        "recipients": {"list_name": "Example Workspace"},
        "settings": {"title": "Spring launch", "subject_line": "Spring launch: checkout"},
    }
    assert render_campaign(campaign, BODY) == (
        "Mailchimp campaign: Spring launch\n"
        "Subject: Spring launch: checkout\n"
        "Audience: Example Workspace\n"
        "Sent on 2026-10-16\n"
        "\n" + BODY
    )
    draft = {**campaign, "status": "save", "send_time": ""}
    assert "Status: draft, not sent" in render_campaign(draft, BODY)
    sending = {**campaign, "status": "sending"}
    assert render_campaign(sending, BODY) == render_campaign(campaign, BODY)


def test_note_names_the_contact_and_hides_the_email_by_default():
    note = {
        "note": "Prefers updates.\n",
        "created_at": "2026-10-16T09:26:16+00:00",
        "created_by": "Anand",
    }
    member = {"full_name": "Dana Lee", "email_address": "dana@example.com"}
    text = render_note(note, member, "Example Workspace")
    assert text == (
        "Mailchimp contact note about Dana Lee\n"
        "Audience: Example Workspace\n"
        "Written by Anand on 2026-10-16\n"
        "\nPrefers updates."
    )
    assert "dana@example.com" not in text
    assert "<dana@example.com>" in render_note(note, member, "", include_contact_email=True)


def test_unusual_spaces_become_plain_spaces():
    html = "<p>Launch\u2007moved\u00a0to\u2009May.</p>"
    assert email_text({"archive_html": html}) == "Launch moved to May."


def test_halted_send_archive_is_plain_text_and_gives_only_the_authors_text():
    # Live: once Mailchimp halted a send, archive_html held the plain text with
    # merge tags resolved and one <br /> per line, so no footer classes to hide.
    assert "<p" not in HALTED
    assert email_text({"archive_html": HALTED}) == "Checkout launch\n\n" + BODY


def test_br_only_archive_keeps_the_authors_angle_brackets():
    html = "Prices &lt; 10 EUR<br />\nUse a < b, not a<=b.<br />\nCopyright (C) 2026 X."
    assert email_text({"archive_html": html}) == "Prices < 10 EUR\nUse a < b, not a<=b."


def test_invisible_formatting_characters_go_but_joiners_stay():
    # Soft hyphens and zero-width spaces come in with text pasted from editors;
    # ZWJ/ZWNJ are part of Hindi conjuncts and emoji sequences.
    hindi = "\u0915\u094d\u200d\u0937"
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    html = f"<p>check\u00adout ships\u200b today\ufeff.</p><p>{hindi} {family}</p>"
    assert email_text({"archive_html": html}) == f"checkout ships today.\n\n{hindi} {family}"


def test_row_title_and_document_title_agree():
    from cognee_community_connector_mailchimp.mailchimp import campaign_title

    campaign = {"settings": {"title": "  Spring launch  "}, "status": "sent"}
    assert campaign_title(campaign) == "Spring launch"
    assert render_campaign(campaign, "").startswith("Mailchimp campaign: Spring launch\n")
    assert campaign_title({"settings": {"title": "   "}}) == "(untitled)"
