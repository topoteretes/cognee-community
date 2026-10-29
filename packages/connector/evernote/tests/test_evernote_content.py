"""Unit tests for ENML content conversion and row building."""

from types import SimpleNamespace

from cognee_community_connector_evernote.evernote import enml_to_text, note_to_row


def test_enml_headings():
    enml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">
<en-note>
<h1>Top Level Title</h1>
<h2>Section Title</h2>
<h3>Subsection Title</h3>
<h4>Fourth Level</h4>
</en-note>"""
    text = enml_to_text(enml)
    assert "# Top Level Title" in text
    assert "## Section Title" in text
    assert "### Subsection Title" in text
    assert "Fourth Level" in text
    # h4 should not have a markdown hash prefix
    assert "#### " not in text


def test_enml_lists():
    enml = """<en-note>
<ul>
  <li>First item</li>
  <li>Second item</li>
</ul>
</en-note>"""
    text = enml_to_text(enml)
    assert "- First item" in text
    assert "- Second item" in text


def test_enml_todos_checked_and_unchecked():
    enml = """<en-note>
<div><en-todo checked="true"/>Completed task</div>
<div><en-todo checked="false"/>Incomplete task</div>
<div><en-todo/>Default task</div>
</en-note>"""
    text = enml_to_text(enml)
    assert "[x] Completed task" in text
    assert "[ ] Incomplete task" in text
    assert "[ ] Default task" in text


def test_enml_entities():
    enml = "<en-note><div>One&nbsp;&amp;&nbsp;Two &lt;Three&gt; &quot;Quotes&quot;</div></en-note>"
    text = enml_to_text(enml)
    assert "&" in text
    assert "<Three>" in text
    assert '"Quotes"' in text
    assert "One" in text and "Two" in text


def test_enml_nested_divs():
    enml = """<en-note>
<div>
  <div>Outer Line</div>
  <div>
    <div>Inner Line A</div>
    <div>Inner Line B</div>
  </div>
</div>
</en-note>"""
    text = enml_to_text(enml)
    assert "Outer Line" in text
    assert "Inner Line A" in text
    assert "Inner Line B" in text
    # Runs of blank lines should be collapsed to at most one
    assert "\n\n\n" not in text


def test_enml_media_attachment():
    enml = """<en-note>
<div>Here is an attachment:</div>
<en-media type="image/png" hash="d41d8cd98f00b204e9800998ecf8427e"/>
</en-note>"""
    text = enml_to_text(enml)
    assert "[attachment: image/png]" in text
    # Binary/hash should not be inlined as content
    assert "d41d8cd98f00b204e9800998ecf8427e" not in text


def test_enml_crypt_is_redacted():
    enml = """<en-note>
<div>Public text</div>
<en-crypt hint="password is pet name">super secret cipher data 12345</en-crypt>
<div>More public text</div>
</en-note>"""
    text = enml_to_text(enml)
    assert "[encrypted content]" in text
    assert "super secret cipher data 12345" not in text
    assert "Public text" in text
    assert "More public text" in text


def test_enml_empty_input():
    assert enml_to_text("") == ""
    assert enml_to_text("   \n\t  ") == ""
    assert enml_to_text(None) == ""


def test_enml_malformed_markup_never_raises():
    malformed = "<<<not valid xml at all <div>open tag without close <b>bold"
    text = enml_to_text(malformed)
    assert isinstance(text, str)
    assert "open tag without close" in text


def test_enml_unicode_text():
    enml = "<en-note><div>日本語のメモ 🚀 &amp; 한국어 🌟</div></en-note>"
    text = enml_to_text(enml)
    assert "日本語のメモ 🚀 & 한국어 🌟" in text


def test_enml_scripts_and_styles_ignored():
    enml = """<en-note>
<style>body { color: red; }</style>
<script>alert('evil');</script>
<div>Visible content</div>
</en-note>"""
    text = enml_to_text(enml)
    assert "Visible content" in text
    assert "color: red" not in text
    assert "evil" not in text


# --- Row building tests ---


def test_note_to_row_keys_and_values():
    note = SimpleNamespace(
        guid="note-guid-001",
        title="My Sample Note",
        notebookGuid="nb-guid-001",
        tagGuids=["tag-01", "tag-02"],
        created=1609459200000,  # 2021-01-01 00:00:00 UTC
        updated=1612137600000,  # 2021-02-01 00:00:00 UTC
    )
    notebook_names = {"nb-guid-001": "Work Notebook"}
    tag_names = {"tag-01": "Productivity", "tag-02": "Cognee"}

    row = note_to_row(
        note=note,
        content_text="Sample note plain text",
        notebook_names=notebook_names,
        tag_names=tag_names,
    )

    expected_keys = {
        "id",
        "title",
        "content",
        "notebook_guid",
        "notebook",
        "tags",
        "created",
        "updated",
        "_deleted",
    }
    assert set(row.keys()) == expected_keys
    assert row["id"] == "note-guid-001"
    assert row["title"] == "My Sample Note"
    assert row["content"] == "Sample note plain text"
    assert row["notebook_guid"] == "nb-guid-001"
    assert row["notebook"] == "Work Notebook"
    assert row["_deleted"] is False


def test_note_to_row_tags_is_comma_separated_string():
    note = SimpleNamespace(
        guid="note-002",
        title="Tags Test",
        notebookGuid="nb-001",
        tagGuids=["tag-a", "tag-b"],
        created=1609459200000,
        updated=1609459200000,
    )
    tag_names = {"tag-a": "Important", "tag-b": "Follow-Up"}

    row = note_to_row(note=note, content_text="", notebook_names={}, tag_names=tag_names)
    assert isinstance(row["tags"], str)
    assert not isinstance(row["tags"], list)
    assert row["tags"] == "Important, Follow-Up"


def test_note_to_row_created_updated_iso_utc():
    note = SimpleNamespace(
        guid="note-003",
        title="Time Test",
        notebookGuid=None,
        tagGuids=None,
        created=1609459200000,  # 2021-01-01T00:00:00+00:00
        updated=1640995200000,  # 2022-01-01T00:00:00+00:00
    )
    row = note_to_row(note=note, content_text="", notebook_names=None, tag_names=None)
    assert row["created"] == "2021-01-01T00:00:00+00:00"
    assert row["updated"] == "2022-01-01T00:00:00+00:00"


def test_note_to_row_degrades_unknown_guids():
    note = SimpleNamespace(
        guid="note-004",
        title="Unknown GUIDs Test",
        notebookGuid="unknown-nb-guid",
        tagGuids=["unknown-tag-guid"],
        created=None,
        updated=None,
    )
    row = note_to_row(note=note, content_text="", notebook_names={}, tag_names={})
    # Unknown notebook GUID degrades to the GUID
    assert row["notebook"] == "unknown-nb-guid"
    # Unknown tag GUID degrades to the GUID
    assert row["tags"] == "unknown-tag-guid"
    assert row["created"] is None
    assert row["updated"] is None


def test_note_to_row_degrades_empty_guids_to_empty_string():
    note = SimpleNamespace(
        guid="note-005",
        title="Missing GUIDs Test",
        notebookGuid=None,
        tagGuids=None,
        created=None,
        updated=None,
    )
    row = note_to_row(note=note, content_text="", notebook_names=None, tag_names=None)
    assert row["notebook"] == ""
    assert row["tags"] == ""


def test_note_to_row_does_not_invent_url():
    note = SimpleNamespace(
        guid="note-006",
        title="URL Test",
        notebookGuid="nb-1",
        tagGuids=[],
        created=1609459200000,
        updated=1609459200000,
    )
    row = note_to_row(note=note, content_text="Text")
    assert "url" not in row
    assert "note_url" not in row
