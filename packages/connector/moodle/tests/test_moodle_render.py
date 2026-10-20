"""Rendering: Moodle HTML to text, and the document each kind of item becomes."""

import random

from cognee_community_connector_moodle.moodle import (
    _clean_html,
    _when,
    render_assignment,
    render_book,
    render_chapter,
    render_course,
    render_discussion,
    render_entry,
    render_page,
    render_section,
)

BASE = "https://moodle.example.edu"
COURSE = {
    "id": 7,
    "fullname": "Algorithms",
    "shortname": "CS201",
    "summary": "<p>Sorting and searching.</p>",
}
ROW_COLUMNS = {"id", "title", "content", "url", "cognee_node_set", "_deleted"}
# 2026-10-01 10:00:00 UTC
T0 = 1_790_848_800


def test_timestamps_render_as_utc_dates_and_zero_is_blank():
    assert _when(T0) == "2026-10-01 10:00"
    assert _when(0) == ""
    assert _when(None) == ""
    assert _when("x") == ""


def test_clean_html_keeps_structure_drops_links_and_placeholders():
    raw = (
        '<h2>Week 3</h2><p>Read <b>chapter&nbsp;4</b> and <a href="https://moodle.example.edu/'
        'webservice/pluginfile.php/9/mod_page/content/1/slides.pdf?token=abc">the slides</a>.</p>'
        '<ul><li>Sorting</li><li>Searching</li></ul><img src="@@PLUGINFILE@@/x.png" alt="Flow">'
        "<p>Line one<br>Line two</p><script>alert(1)</script>"
    )
    text = _clean_html(raw)
    assert text == (
        "Week 3\nRead chapter 4 and the slides.\n\n- Sorting\n- Searching\n\n"
        "[image: Flow]\nLine one\nLine two"
    )
    assert "pluginfile" not in text
    assert "token=" not in text
    assert "@@PLUGINFILE@@" not in text
    assert _clean_html(None) == ""


def test_every_renderer_produces_the_frozen_row_shape():
    rows = [
        render_course(COURSE, [{"name": "Week 1"}], BASE),
        render_section(COURSE, {"id": 3, "name": "Week 1", "section": 1}, [], BASE),
        render_page(
            COURSE, {"coursemodule": 11, "name": "Sorting", "content": "<p>b</p>"}, "Week 1", BASE
        ),
        render_book(COURSE, {"coursemodule": 12, "name": "Notes"}, ["Intro"], "Week 1", BASE),
        render_chapter(
            COURSE,
            {"coursemodule": 12, "name": "Notes"},
            {"id": 5, "title": "Intro"},
            "<p>c</p>",
            BASE,
        ),
        render_assignment(COURSE, {"cmid": 13, "name": "HW1"}, "Week 1", BASE),
        render_entry(
            COURSE, {"coursemodule": 14, "name": "Terms"}, {"id": 8, "concept": "Heap"}, BASE
        ),
        render_discussion(
            COURSE, {"cmid": 15, "name": "News"}, {"discussion": 21, "name": "Exam"}, [], BASE
        ),
    ]
    for row in rows:
        assert set(row) == ROW_COLUMNS
        assert row["_deleted"] is False
        assert row["cognee_node_set"] == ["course:7"]
        assert row["title"] and row["content"]
        assert row["url"].startswith(BASE)
    assert [row["id"] for row in rows] == [
        "course:7",
        "section:7:3",
        "page:7:11",
        "book:7:12",
        "chapter:7:12:5",
        "assignment:7:13",
        "entry:7:14:8",
        "discussion:7:21",
    ]


def test_the_course_document_lists_summary_and_sections():
    row = render_course(
        {**COURSE, "startdate": T0}, [{"name": "General"}, {"name": "Week 1"}], BASE
    )
    assert row["title"] == "Algorithms"
    assert row["content"].splitlines() == [
        "Short name: CS201",
        "Starts: 2026-10-01",
        "",
        "Sorting and searching.",
        "",
        "Sections:",
        "- General",
        "- Week 1",
    ]


def test_a_section_lists_its_modules_and_inlines_labels():
    modules = [
        {"name": "Read this first", "modname": "label", "description": "<p>Read this first.</p>"},
        {"name": "Sorting", "modname": "page", "modplural": "Pages"},
        {"name": "HW1", "modname": "assign", "modplural": "Assignments"},
    ]
    row = render_section(
        COURSE,
        {"id": 3, "name": "Week 1", "section": 1, "summary": "<p>Basics.</p>"},
        modules,
        BASE,
    )
    assert row["title"] == "Section: Week 1"
    assert row["content"].splitlines() == [
        "Course: Algorithms",
        "",
        "Basics.",
        "",
        "Read this first.",
        "- Pages: Sorting",
        "- Assignments: HW1",
    ]
    assert row["url"] == f"{BASE}/course/view.php?id=7&section=1"


def test_a_page_names_its_course_and_section_then_its_text():
    page = {
        "coursemodule": 11,
        "name": "Sorting",
        "intro": "<p>Intro.</p>",
        "content": "<h3>Merge sort</h3><p>Divide.</p>",
        "timemodified": T0,
    }
    row = render_page(COURSE, page, "Week 1", BASE)
    assert row["content"].splitlines() == [
        "Course: Algorithms",
        "Section: Week 1",
        "",
        "Intro.",
        "",
        "Merge sort",
        "Divide.",
    ]
    assert row["url"] == f"{BASE}/mod/page/view.php?id=11"
    assert "2026" not in row["content"]  # nothing volatile in the document


def test_html_encoded_names_are_unescaped_in_titles_and_text():
    # Moodle web services HTML-encode every name and title (external_format_string).
    page = {"coursemodule": 11, "name": "Q&amp;A &quot;week 1&quot;", "content": "<p>x</p>"}
    assert render_page(COURSE, page, "Week 1", BASE)["title"] == 'Q&A "week 1"'
    section = {"id": 3, "name": "Tom &amp; Jerry", "section": 1, "summary": ""}
    row = render_section(COURSE, section, [], BASE)
    assert row["title"] == "Section: Tom & Jerry"
    assert row["url"].endswith("/course/view.php?id=7&section=1")  # urls are never unescaped


def test_an_assignment_renders_dates_and_instructions():
    assignment = {
        "cmid": 13,
        "name": "HW1",
        "intro": "<p>Implement merge sort.</p>",
        "duedate": T0,
        "allowsubmissionsfromdate": 0,
        "cutoffdate": T0 + 86400,
    }
    row = render_assignment(COURSE, assignment, "Week 1", BASE)
    assert row["content"].splitlines() == [
        "Course: Algorithms",
        "Section: Week 1",
        "Due: 2026-10-01 10:00",
        "Cut-off: 2026-10-02 10:00",
        "",
        "Implement merge sort.",
    ]


def test_a_chapter_carries_its_book_and_text():
    row = render_chapter(
        COURSE,
        {"coursemodule": 12, "name": "Notes"},
        {"id": 5, "title": "Heaps"},
        "<p>A heap is a tree.</p>",
        BASE,
    )
    assert row["title"] == "Notes: Heaps"
    assert row["content"].splitlines() == [
        "Course: Algorithms",
        "Book: Notes",
        "",
        "A heap is a tree.",
    ]
    assert row["url"] == f"{BASE}/mod/book/view.php?id=12&chapterid=5"


def _posts():
    return [
        {
            "id": 1,
            "message": "<p>Is question 2 about stability?</p>",
            "author": {"id": 22, "fullname": "Ben Turing"},
            "hasparent": False,
            "parentid": None,
            "timecreated": T0,
        },
        {
            "id": 2,
            "message": "<p>Yes.</p><p>See the slides.</p>",
            "author": {"id": 11, "fullname": "Ada Lovelace"},
            "hasparent": True,
            "parentid": 1,
            "timecreated": T0 + 60,
        },
        {
            "id": 3,
            "message": "",
            "author": {"id": 22, "fullname": "Ben Turing"},
            "hasparent": True,
            "parentid": 2,
            "timecreated": T0 + 120,
            "isdeleted": True,
        },
        {
            "id": 4,
            "message": "<p>Thanks</p>",
            "author": {"id": 33, "fullname": "Cara Hopper"},
            "hasparent": True,
            "parentid": 1,
            "timecreated": T0 + 180,
        },
    ]


def test_a_discussion_thread_is_indented_and_anonymised_by_default():
    discussion = {"discussion": 21, "name": "Question 2", "created": T0, "numreplies": 3}
    row = render_discussion(COURSE, {"cmid": 15, "name": "Q&A"}, discussion, _posts(), BASE)
    assert row["content"].splitlines() == [
        "Course: Algorithms",
        "Forum: Q&A",
        "Posted: 2026-10-01",
        "Replies: 3",
        "",
        "Participant 1 (2026-10-01): Is question 2 about stability?",
        "  Participant 2 (2026-10-01): Yes.",
        "    See the slides.",
        "    [deleted]",
        "  Participant 3 (2026-10-01): Thanks",
    ]
    assert "Ada" not in row["content"]
    assert row["url"] == f"{BASE}/mod/forum/discuss.php?d=21"


def test_a_discussion_names_authors_when_anonymisation_is_off():
    row = render_discussion(
        COURSE,
        {"cmid": 15, "name": "Q&A"},
        {"discussion": 21, "name": "Q"},
        _posts(),
        BASE,
        anonymize=False,
    )
    lines = row["content"].splitlines()
    assert "Ben Turing (2026-10-01): Is question 2 about stability?" in lines
    assert "  Ada Lovelace (2026-10-01): Yes." in lines


def test_an_announcement_uses_the_announcement_prefix():
    row = render_discussion(
        COURSE,
        {"cmid": 15, "name": "Announcements"},
        {"discussion": 21, "name": "Exam"},
        _posts()[:1],
        BASE,
        kind="announcement",
    )
    assert row["id"] == "announcement:7:21"


def test_a_long_thread_is_cut_and_says_so():
    posts = [
        {
            "id": n,
            "message": f"<p>m{n}</p>",
            "author": {"id": 1},
            "hasparent": False,
            "timecreated": n,
        }
        for n in range(1, 601)
    ]
    row = render_discussion(
        COURSE, {"cmid": 15, "name": "Q"}, {"discussion": 21, "name": "Q"}, posts, BASE
    )
    lines = row["content"].splitlines()
    assert lines[-1] == "(thread cut at 500 posts)"
    assert sum(1 for line in lines if line.startswith("Participant 1")) == 500


def test_a_thread_renders_the_same_whatever_order_moodle_sends():
    expected = render_discussion(
        COURSE, {"cmid": 15, "name": "Q"}, {"discussion": 21, "name": "Q"}, _posts(), BASE
    )
    for seed in range(10):
        shuffled = random.Random(seed).sample(_posts(), 4)
        assert (
            render_discussion(
                COURSE, {"cmid": 15, "name": "Q"}, {"discussion": 21, "name": "Q"}, shuffled, BASE
            )
            == expected
        )
