"""Unit tests for ENML content conversion and row building."""

from types import SimpleNamespace

from evernote.edam.notestore.ttypes import SyncChunk

from cognee_community_connector_evernote.evernote import (
    _sync_records,
    enml_to_text,
    note_to_row,
)


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


def test_enml_malformed_unclosed_comment_does_not_raise():
    text = enml_to_text("<!-- unclosed comment <div>Visible</div>")
    assert isinstance(text, str)


def test_enml_malformed_unclosed_cdata_does_not_raise():
    text = enml_to_text("<![CDATA[ unclosed cdata <div>Visible</div>")
    assert isinstance(text, str)


def test_enml_malformed_bogus_declaration_does_not_raise():
    text = enml_to_text("<!BOGUS DECLARATION><div>Visible</div>")
    assert isinstance(text, str)
    assert "Visible" in text


def test_enml_malformed_bad_entity_does_not_raise():
    text = enml_to_text("<en-note><div>&bad_entity; &amp; &unknown</div></en-note>")
    assert isinstance(text, str)
    assert "&" in text


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
        created=1609459200000,
        updated=1612137600000,
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
        "_deleted",
    }
    assert set(row.keys()) == expected_keys
    assert row["id"] == "note-guid-001"
    assert row["title"] == "My Sample Note"
    assert (
        row["content"]
        == "Notebook: Work Notebook\nTags: Cognee, Productivity\n\nSample note plain text"
    )
    assert row["_deleted"] is False


def test_note_to_row_no_notebook_line_when_unknown():
    note = SimpleNamespace(
        guid="note-002",
        title="No Notebook Test",
        notebookGuid="unknown-nb-guid",
        tagGuids=["tag-01"],
    )
    row = note_to_row(
        note=note,
        content_text="Body content",
        notebook_names={},
        tag_names={"tag-01": "TagOne"},
    )
    assert row["content"] == "Tags: TagOne\n\nBody content"
    assert "Notebook:" not in row["content"]


def test_note_to_row_no_tags_line_when_empty_or_unknown():
    note = SimpleNamespace(
        guid="note-003",
        title="No Tags Test",
        notebookGuid="nb-01",
        tagGuids=["unknown-tag"],
    )
    row = note_to_row(
        note=note,
        content_text="Body content",
        notebook_names={"nb-01": "Personal"},
        tag_names={},
    )
    assert row["content"] == "Notebook: Personal\n\nBody content"
    assert "Tags:" not in row["content"]


def test_note_to_row_neither_header_has_no_leading_blank_line():
    note = SimpleNamespace(
        guid="note-004",
        title="Plain Body Test",
        notebookGuid=None,
        tagGuids=[],
    )
    row = note_to_row(
        note=note,
        content_text="Just the body text",
        notebook_names=None,
        tag_names=None,
    )
    assert row["content"] == "Just the body text"
    assert not row["content"].startswith("\n")


def test_note_to_row_tags_sorted_case_insensitively():
    note = SimpleNamespace(
        guid="note-005",
        title="Sorted Tags Test",
        notebookGuid=None,
        tagGuids=["t1", "t2", "t3"],
    )
    tag_names = {"t1": "zebra", "t2": "Apple", "t3": "banana"}
    row = note_to_row(
        note=note,
        content_text="Body",
        notebook_names=None,
        tag_names=tag_names,
    )
    assert row["content"] == "Tags: Apple, banana, zebra\n\nBody"


def test_tag_only_edit_changes_content():
    note1 = SimpleNamespace(guid="n1", title="Title", notebookGuid=None, tagGuids=["t1"])
    note2 = SimpleNamespace(guid="n1", title="Title", notebookGuid=None, tagGuids=["t2"])
    tag_names = {"t1": "TagA", "t2": "TagB"}

    row1 = note_to_row(note1, "Body text", notebook_names=None, tag_names=tag_names)
    row2 = note_to_row(note2, "Body text", notebook_names=None, tag_names=tag_names)

    assert row1["content"] != row2["content"]
    assert "Tags: TagA" in row1["content"]
    assert "Tags: TagB" in row2["content"]


def test_empty_body_still_tombstones_even_with_notebook_and_tags():
    class FakeEmptyBodyClient:
        def get_user_id(self):
            return 12345

        def get_sync_state(self):
            return SimpleNamespace(currentTime=1000, updateCount=10, fullSyncBefore=0)

        def get_filtered_sync_chunk(self, after_usn, max_entries, filter_obj):
            note = SimpleNamespace(
                guid="empty-note",
                title="",
                notebookGuid="nb-1",
                tagGuids=["tag-1"],
                active=True,
            )
            nb = SimpleNamespace(guid="nb-1", name="Work")
            tag = SimpleNamespace(guid="tag-1", name="Urgent")
            chunk = SyncChunk(
                chunkHighUSN=10,
                updateCount=10,
                notebooks=[nb],
                tags=[tag],
                notes=[note],
            )
            return chunk

        def get_note_content(self, guid):
            return "<en-note></en-note>"

    client = FakeEmptyBodyClient()
    state = {}
    rows = list(_sync_records(client, state))
    tombstones = [r for r in rows if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "empty-note"


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
