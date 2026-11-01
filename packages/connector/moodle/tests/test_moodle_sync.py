"""The sync walk, driven with a plain dict as dlt resource state."""

import pytest
from fake_moodle import FakeMoodle

from cognee_community_connector_moodle.moodle import (
    KINDS,
    MoodleAccessError,
    MoodleAPIError,
    MoodleAuthError,
    _iter_rows,
)

ROW_COLUMNS = {"id", "title", "content", "url", "cognee_node_set", "_deleted"}
ALL = KINDS
ADA, BEN = (11, "Ada Lovelace"), (22, "Ben Turing")


def _sync(fake: FakeMoodle, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, state, stats, **kwargs))
    return rows, stats


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if not row.get("_deleted")}


def _tombstones(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


def _course() -> FakeMoodle:
    """One course: a section with a page, a book, an assignment, a glossary, news and a forum."""
    fake = FakeMoodle()
    fake.add_course(7, "Algorithms", summary="<p>Sorting and searching.</p>")
    fake.add_section(7, "Week 1", "<p>Basics.</p>")
    fake.add_page(7, "Sorting", "<p>Merge sort divides and conquers.</p>", section=1)
    fake.add_book(
        7,
        "Notes",
        [("Heaps", "<p>A heap is a tree.</p>"), ("Graphs", "<p>Nodes and edges.</p>")],
        section=1,
    )
    fake.add_assignment(7, "HW1", "<p>Implement merge sort.</p>", section=1, duedate=1_790_762_400)
    fake.add_glossary(
        7, "Terms", [("Heap", "<p>A tree.</p>"), ("Stable", "<p>Keeps order.</p>")], section=1
    )
    news = fake.add_forum(7, "Announcements", forum_type="news")
    fake.add_discussion(7, news["cmid"], ADA, "Midterm format", "<p>Open book.</p>")
    forum = fake.add_forum(7, "Q&A", section=1)
    fake.add_discussion(7, forum["cmid"], BEN, "Question 2", "<p>Is it about stability?</p>")
    return fake


def _cmid(fake: FakeMoodle, store: str, name: str) -> int:
    key = "cmid" if store in ("assignments", "forums") else "coursemodule"
    return next(r[key] for r in fake.courses[7][store].values() if r["name"] == name)


def test_first_sync_emits_the_course_its_sections_and_every_default_kind():
    fake = _course()
    rows, stats = _sync(fake, {})

    kinds = {row["id"].split(":")[0] for row in rows}
    assert kinds == {
        "course",
        "section",
        "page",
        "book",
        "chapter",
        "assignment",
        "glossary",
        "entry",
        "announcement",
    }
    assert "discussion" not in kinds and "forum" not in kinds  # forums are opt-in
    assert all(set(row) == ROW_COLUMNS for row in rows)
    assert all(row["cognee_node_set"] == ["course:7"] for row in rows)
    assert all(row["title"] and row["content"] and row["_deleted"] is False for row in rows)
    assert stats["failed"] == 0 and stats["emitted"] == stats["scanned"] == len(rows)
    assert len(fake.downloads) == 2  # one per book chapter


def test_forums_are_rendered_when_asked_for_with_anonymous_authors():
    fake = _course()
    rows, _ = _sync(fake, {}, include=ALL)
    kinds = {row["id"].split(":")[0] for row in rows}
    assert {"forum", "discussion"} <= kinds
    content = next(r["content"] for r in rows if r["id"].startswith("discussion:"))
    assert "Participant 1" in content
    assert "Ben" not in content


def test_items_carry_their_section_and_the_section_lists_them():
    rows, _ = _sync(_course(), {})
    by_id = {row["id"]: row for row in rows}
    page = next(r for r in rows if r["id"].startswith("page:"))
    assert "Section: Week 1" in page["content"].splitlines()
    section = next(r for r in rows if r["title"] == "Section: Week 1")
    assert "- Pages: Sorting" in section["content"]
    assert "- Assignments: HW1" in section["content"]
    assert by_id["course:7"]["content"].splitlines()[-2:] == ["- General", "- Week 1"]


def test_an_unchanged_resync_emits_nothing_and_reads_no_bodies():
    fake, state = _course(), {}
    _sync(fake, state)
    downloads, posts = len(fake.downloads), fake.count("mod_forum_get_discussion_posts")

    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["scanned"] == stats["skipped"]
    assert len(fake.downloads) == downloads  # chapter stamps matched: no download
    assert fake.count("mod_forum_get_discussion_posts") == posts


def test_an_edited_page_is_re_emitted_exactly_once():
    fake, state = _course(), {}
    _sync(fake, state)

    page = fake.courses[7]["pages"][_cmid(fake, "pages", "Sorting")]
    page["content"] = "<p>Merge sort, now with a proof.</p>"
    page["timemodified"] = fake._stamp()
    rows, _ = _sync(fake, state)
    assert _ids(rows) == {f"page:7:{page['coursemodule']}"}
    assert "now with a proof" in rows[0]["content"]

    rows, _ = _sync(fake, state)
    assert rows == []


def test_a_bumped_stamp_with_identical_text_emits_no_document():
    fake, state = _course(), {}
    _sync(fake, state)

    fake.courses[7]["pages"][_cmid(fake, "pages", "Sorting")]["timemodified"] = fake._stamp()
    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["emitted"] == 0


def test_hidden_and_deleted_modules_are_tombstoned_with_their_children():
    fake, state = _course(), {}
    _sync(fake, state)
    book_cmid = _cmid(fake, "books", "Notes")
    glossary_cmid = _cmid(fake, "glossaries", "Terms")

    fake.hide(7, book_cmid)
    fake.remove(7, glossary_cmid)
    rows, stats = _sync(fake, state)

    gone = _tombstones(rows)
    assert {r for r in gone if r.startswith("book:") or r.startswith("chapter:")} == {
        f"book:7:{book_cmid}",
        *{f"chapter:7:{book_cmid}:{c}" for c in fake.courses[7]["books"][book_cmid]["_chapters"]},
    }
    assert sum(1 for r in gone if r.startswith("entry:") or r.startswith("glossary:")) == 3
    assert stats["deleted"] == len(gone)
    # the section lost two modules, so it re-renders
    assert _ids(rows) == {next(r["id"] for r in rows if r["id"].startswith("section:"))}


def test_a_deleted_glossary_entry_and_a_new_one_are_both_handled():
    fake, state = _course(), {}
    _sync(fake, state)
    cmid = _cmid(fake, "glossaries", "Terms")
    glossary = fake.courses[7]["glossaries"][cmid]
    old = next(e for e in glossary["_entries"].values() if e["concept"] == "Heap")
    del glossary["_entries"][old["id"]]
    new = fake.add_entry(7, cmid, "Queue", "<p>FIFO.</p>")

    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == {f"entry:7:{cmid}:{old['id']}"}
    assert _ids(rows) == {f"entry:7:{cmid}:{new['id']}"}


def test_an_unapproved_entry_is_left_out():
    fake, state = _course(), {}
    cmid = _cmid(fake, "glossaries", "Terms")
    fake.add_entry(7, cmid, "Draft", "<p>Pending.</p>", approved=False)

    rows, _ = _sync(fake, state)

    assert not any("Draft" in r["title"] for r in rows)


def test_a_new_reply_re_emits_only_that_discussion():
    fake, state = _course(), {}
    _sync(fake, state, include=ALL)
    forum_cmid = _cmid(fake, "forums", "Q&A")
    discussion_id = next(iter(fake.courses[7]["forums"][forum_cmid]["_discussions"]))

    fake.reply(7, forum_cmid, discussion_id, ADA, "<p>Yes, stability.</p>")
    rows, _ = _sync(fake, state, include=ALL)

    assert _ids(rows) == {f"discussion:7:{discussion_id}"}
    assert "Yes, stability." in rows[0]["content"]


def test_announcements_come_from_the_news_forum_only():
    rows, _ = _sync(_course(), {})
    announcements = [r for r in rows if r["id"].startswith("announcement:")]
    assert len(announcements) == 1
    assert announcements[0]["title"] == "Midterm format"
    assert not any(r["id"].startswith("forum:") for r in rows)  # the news forum has no forum row


def test_a_course_removed_from_the_selection_leaves_memory():
    fake, state = _course(), {}
    fake.add_course(8, "Databases")
    fake.add_page(8, "Joins", "<p>Inner and outer.</p>")
    _sync(fake, state, course_ids=[7, 8])

    rows, _ = _sync(fake, state, course_ids=[7])

    assert {r.split(":")[1] for r in _tombstones(rows)} == {"8"}
    assert len(_tombstones(rows)) == 3  # course, section, page
    assert state["courses"] == ["7"]


def test_a_deselected_kind_leaves_memory():
    fake, state = _course(), {}
    _sync(fake, state)

    rows, _ = _sync(fake, state, include=("pages",))

    prefixes = {r.split(":")[0] for r in _tombstones(rows)}
    assert prefixes == {"book", "chapter", "assignment", "glossary", "entry", "announcement"}
    assert _ids(rows) == set()


def test_a_cut_run_keeps_its_progress_and_never_tombstones():
    fake, state = _course(), {}
    _sync(fake, state)
    fake.remove(7, _cmid(fake, "pages", "Sorting"))

    fake.fail_after = 4  # site info, courses, contents and the page listing pass, then boom
    fake.fail_with = MoodleAPIError("Moodle request failed for x: HTTP 503")
    with pytest.raises(MoodleAPIError):
        _sync(fake, state)
    assert any(r.startswith("page:") for r in state["items"])

    fake.fail_after = None
    rows, stats = _sync(fake, state)
    assert len(_tombstones(rows)) == 1
    assert next(iter(_tombstones(rows))).startswith("page:")
    assert stats["failed"] == 0


def test_a_request_budget_cuts_the_run_and_the_next_run_finishes():
    fake, state = _course(), {}

    rows, stats = _sync(fake, state, max_requests=6)
    assert stats["failed_budget"] == 1
    assert _tombstones(rows) == set()
    partial = _ids(rows)
    assert partial

    rows, stats = _sync(fake, state)
    assert stats["failed"] == 0
    assert len(partial | _ids(rows)) >= 12


def test_a_failing_chapter_download_keeps_that_chapter_and_the_run_moves_on():
    fake, state = _course(), {}
    book_cmid = _cmid(fake, "books", "Notes")
    chapters = list(fake.courses[7]["books"][book_cmid]["_chapters"].values())
    fake.unreadable_files.add(chapters[0]["fileurl"])

    rows, stats = _sync(fake, state)

    assert f"chapter:7:{book_cmid}:{chapters[0]['id']}" not in _ids(rows)
    assert f"chapter:7:{book_cmid}:{chapters[1]['id']}" in _ids(rows)
    assert stats["failed"] == 1
    assert _tombstones(rows) == set()

    fake.unreadable_files.clear()
    rows, stats = _sync(fake, state)
    assert _ids(rows) == {f"chapter:7:{book_cmid}:{chapters[0]['id']}"}
    assert stats["failed"] == 0


def test_a_discussion_whose_posts_cannot_be_read_is_skipped_not_fatal():
    fake, state = _course(), {}
    fake.fail_functions["mod_forum_get_discussion_posts"] = MoodleAccessError("nopermissions")

    rows, stats = _sync(fake, state)

    assert not any(r.startswith("announcement:") for r in _ids(rows))
    assert any(r.startswith("page:") for r in _ids(rows))
    assert stats["failed"] == 1


def test_a_missing_function_on_the_service_is_a_clear_error_before_any_read():
    fake = _course()
    fake.functions.remove("mod_book_get_books_by_courses")

    with pytest.raises(MoodleAccessError, match="mod_book_get_books_by_courses"):
        _sync(fake, {})
    assert fake.count("core_course_get_contents") == 0

    rows, _ = _sync(fake, {}, include=("pages",))  # books not selected: no complaint
    assert any(r.startswith("page:") for r in _ids(rows))


def test_a_course_the_user_is_not_enrolled_in_is_skipped_and_nothing_is_deleted():
    fake, state = _course(), {}
    fake.add_course(8, "Databases")
    fake.add_page(8, "Joins", "<p>Inner and outer.</p>")
    _sync(fake, state, course_ids=[7, 8])

    del fake.courses[8]
    rows, stats = _sync(fake, state, course_ids=[7, 8])

    assert _tombstones(rows) == set()
    assert stats["failed"] == 1
    assert any(r.startswith("page:8:") for r in state["items"])
    assert state["courses"] == ["7", "8"]


def test_without_course_ids_every_visible_enrolled_course_is_synced():
    fake = _course()
    fake.add_course(8, "Databases")
    fake.add_course(9, "Old course", visible=0)

    rows, _ = _sync(fake, {}, include=("pages",))

    assert {"course:7", "course:8"} <= _ids(rows)
    assert "course:9" not in _ids(rows)


def test_an_auth_error_before_any_row_raises():
    fake = _course()
    fake.fail_after = 0
    fake.fail_with = MoodleAuthError("Moodle rejected the web service token (invalidtoken)")

    with pytest.raises(MoodleAuthError):
        _sync(fake, {})


def test_check_active_runs_before_every_request_and_can_stop_the_sync():
    fake, checks = _course(), []

    def check_active():
        checks.append(len(fake.calls))
        if len(checks) > 3:
            raise PermissionError("access revoked")

    with pytest.raises(PermissionError):
        _sync(fake, {}, check_active=check_active)

    assert checks == [0, 1, 2, 3]
    assert len(fake.calls) == 3


def test_grades_submissions_and_participants_are_never_requested():
    fake = _course()
    _sync(fake, {}, include=ALL)
    # the fake itself asserts no forbidden function was called; list what was
    assert {f for f, _ in fake.calls} == {
        "core_webservice_get_site_info",
        "core_enrol_get_users_courses",
        "core_course_get_contents",
        "mod_page_get_pages_by_courses",
        "mod_book_get_books_by_courses",
        "mod_assign_get_assignments",
        "mod_glossary_get_glossaries_by_courses",
        "mod_glossary_get_entries_by_letter",
        "mod_forum_get_forums_by_courses",
        "mod_forum_get_forum_discussions",
        "mod_forum_get_discussion_posts",
    }
