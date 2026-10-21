"""In-memory stand-in for Moodle's REST web services, shaped like their real answers.

It implements the two methods the connector needs, ``call(function, **params)`` and
``download(file_url)``, exposes the functions a real service lists in
``core_webservice_get_site_info``, and raises the connector's typed errors like the
real client would.
"""

from typing import Any

from cognee_community_connector_moodle.moodle import (
    MoodleAccessError,
    MoodleAPIError,
    MoodleNotFoundError,
)

BASE = "https://moodle.example.edu"
ALL_FUNCTIONS = (
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
)
# functions the connector must never call
FORBIDDEN = ("grade", "submission", "enrolled_users", "user_get", "message")


class FakeMoodle:
    def __init__(self, functions=ALL_FUNCTIONS, user_id: int = 5):
        self.base_url = BASE
        self.user_id = user_id
        self.functions = list(functions)
        self.courses: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[str] = []
        self.fail_functions: dict[str, Exception] = {}
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None
        self.unreadable_files: set[str] = set()
        self._clock = 1_760_000_000
        self._ids = 100

    # -- seeding -----------------------------------------------------------
    def _stamp(self) -> int:
        self._clock += 60
        return self._clock

    def _next_id(self) -> int:
        self._ids += 1
        return self._ids

    def add_course(self, course_id: int, fullname: str, **fields) -> dict[str, Any]:
        self.courses[course_id] = {
            "id": course_id,
            "fullname": fullname,
            "shortname": f"C{course_id}",
            "summary": "",
            "summaryformat": 1,
            "visible": 1,
            "startdate": 0,
            "enddate": 0,
            "sections": [],
            "pages": {},
            "books": {},
            "assignments": {},
            "glossaries": {},
            "forums": {},
            **fields,
        }
        self.add_section(course_id, "General")
        return self.courses[course_id]

    def add_section(self, course_id: int, name: str, summary: str = "", **fields) -> dict:
        course = self.courses[course_id]
        section = {
            "id": self._next_id(),
            "name": name,
            "summary": summary,
            "visible": 1,
            "uservisible": True,
            "section": len(course["sections"]),
            "modules": [],
            **fields,
        }
        course["sections"].append(section)
        return section

    def _add_module(self, course_id: int, section_index: int, modname: str, name: str, **fields):
        cmid = self._next_id()
        module = {
            "id": cmid,
            "name": name,
            "modname": modname,
            "modplural": {
                "page": "Pages",
                "book": "Books",
                "assign": "Assignments",
                "glossary": "Glossaries",
                "forum": "Forums",
                "label": "Text and media areas",
            }.get(modname, modname),
            "instance": self._next_id(),
            "visible": 1,
            "uservisible": True,
            "url": f"{BASE}/mod/{modname}/view.php?id={cmid}",
            "contents": [],
            **fields,
        }
        self.courses[course_id]["sections"][section_index]["modules"].append(module)
        return module

    def add_page(
        self, course_id: int, name: str, content: str, *, section: int = 0, **fields
    ) -> dict:
        module = self._add_module(course_id, section, "page", name)
        page = {
            "id": module["instance"],
            "coursemodule": module["id"],
            "course": course_id,
            "name": name,
            "intro": "",
            "content": content,
            "contentformat": 1,
            "timemodified": self._stamp(),
            **fields,
        }
        self.courses[course_id]["pages"][module["id"]] = page
        return page

    def add_book(
        self, course_id: int, name: str, chapters: list[tuple[str, str]], *, section: int = 0
    ) -> dict:
        module = self._add_module(course_id, section, "book", name)
        book = {
            "id": module["instance"],
            "coursemodule": module["id"],
            "course": course_id,
            "name": name,
            "intro": "",
            "timemodified": self._stamp(),
            "_chapters": {},
        }
        self.courses[course_id]["books"][module["id"]] = book
        for title, body in chapters:
            self.add_chapter(course_id, module["id"], title, body)
        self.courses[course_id]["books"][module["id"]] = book
        return book

    def add_chapter(self, course_id: int, cmid: int, title: str, body: str) -> dict:
        chapter_id = self._next_id()
        module = self._module(course_id, cmid)
        file_url = f"{BASE}/webservice/pluginfile.php/99/mod_book/chapter/{chapter_id}/index.html"
        module["contents"].append(
            {
                "type": "file",
                "filename": "index.html",
                "filepath": f"/{chapter_id}/",
                "filesize": 0,
                "fileurl": file_url,
                "content": title,
                "timemodified": self._stamp(),
                "sortorder": 0,
            }
        )
        chapter = {"id": chapter_id, "title": title, "body": body, "fileurl": file_url}
        self.courses[course_id]["books"].setdefault(cmid, {}).setdefault("_chapters", {})[
            chapter_id
        ] = chapter
        return chapter

    def add_assignment(
        self, course_id: int, name: str, intro: str = "", *, section: int = 0, **fields
    ):
        module = self._add_module(course_id, section, "assign", name)
        assignment = {
            "id": module["instance"],
            "cmid": module["id"],
            "course": course_id,
            "name": name,
            "intro": intro,
            "introformat": 1,
            "duedate": 0,
            "allowsubmissionsfromdate": 0,
            "cutoffdate": 0,
            "timemodified": self._stamp(),
            "nosubmissions": 0,
            **fields,
        }
        self.courses[course_id]["assignments"][module["id"]] = assignment
        return assignment

    def add_glossary(
        self, course_id: int, name: str, entries: list[tuple[str, str]], *, section: int = 0
    ):
        module = self._add_module(course_id, section, "glossary", name)
        glossary = {
            "id": module["instance"],
            "coursemodule": module["id"],
            "course": course_id,
            "name": name,
            "intro": "",
            "timemodified": self._stamp(),
            "_entries": {},
        }
        self.courses[course_id]["glossaries"][module["id"]] = glossary
        for concept, definition in entries:
            self.add_entry(course_id, module["id"], concept, definition)
        return glossary

    def add_entry(self, course_id: int, cmid: int, concept: str, definition: str, **fields) -> dict:
        entry = {
            "id": self._next_id(),
            "concept": concept,
            "definition": definition,
            "definitionformat": 1,
            "timemodified": self._stamp(),
            "approved": True,
            "userfullname": "Ada Lovelace",
            **fields,
        }
        self.courses[course_id]["glossaries"][cmid]["_entries"][entry["id"]] = entry
        return entry

    def add_forum(
        self, course_id: int, name: str, *, forum_type: str = "general", section: int = 0
    ):
        module = self._add_module(course_id, section, "forum", name)
        forum = {
            "id": module["instance"],
            "cmid": module["id"],
            "course": course_id,
            "type": forum_type,
            "name": name,
            "intro": "",
            "timemodified": self._stamp(),
            "_discussions": {},
        }
        self.courses[course_id]["forums"][module["id"]] = forum
        return forum

    def add_discussion(
        self, course_id: int, cmid: int, author: tuple[int, str], subject: str, message: str
    ):
        forum = self.courses[course_id]["forums"][cmid]
        discussion_id = self._next_id()
        post_id = self._next_id()
        when = self._stamp()
        discussion = {
            "id": post_id,
            "discussion": discussion_id,
            "name": subject,
            "subject": subject,
            "message": message,
            "userid": author[0],
            "userfullname": author[1],
            "created": when,
            "modified": when,
            "timemodified": when,
            "numreplies": 0,
            "pinned": False,
            "locked": False,
            "_posts": [
                {
                    "id": post_id,
                    "subject": subject,
                    "message": message,
                    "author": {"id": author[0], "fullname": author[1]},
                    "discussionid": discussion_id,
                    "hasparent": False,
                    "parentid": None,
                    "timecreated": when,
                    "timemodified": when,
                    "isdeleted": False,
                }
            ],
        }
        forum["_discussions"][discussion_id] = discussion
        return discussion

    def reply(
        self,
        course_id: int,
        cmid: int,
        discussion_id: int,
        author: tuple[int, str],
        message: str,
        parent=None,
        **fields,
    ):
        discussion = self.courses[course_id]["forums"][cmid]["_discussions"][discussion_id]
        when = self._stamp()
        post = {
            "id": self._next_id(),
            "subject": f"Re: {discussion['subject']}",
            "message": message,
            "author": {"id": author[0], "fullname": author[1]},
            "discussionid": discussion_id,
            "hasparent": True,
            "parentid": parent or discussion["_posts"][0]["id"],
            "timecreated": when,
            "timemodified": when,
            "isdeleted": False,
            **fields,
        }
        discussion["_posts"].append(post)
        discussion["numreplies"] += 1
        discussion["timemodified"] = when
        return post

    def _module(self, course_id: int, cmid: int) -> dict[str, Any]:
        for section in self.courses[course_id]["sections"]:
            for module in section["modules"]:
                if module["id"] == cmid:
                    return module
        raise KeyError(cmid)

    def hide(self, course_id: int, cmid: int) -> None:
        self._module(course_id, cmid)["visible"] = 0

    def remove(self, course_id: int, cmid: int) -> None:
        course = self.courses[course_id]
        for section in course["sections"]:
            section["modules"] = [m for m in section["modules"] if m["id"] != cmid]
        for store in ("pages", "books", "assignments", "glossaries", "forums"):
            course[store].pop(cmid, None)

    # -- the api -----------------------------------------------------------
    def count(self, function: str) -> int:
        return sum(1 for called, _ in self.calls if called == function)

    def call(self, function: str, **params: Any) -> Any:
        self.calls.append((function, dict(params)))
        for forbidden in FORBIDDEN:
            assert forbidden not in function, f"the connector must never call {function}"
        if function in self.fail_functions:
            raise self.fail_functions[function]
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise self.fail_with
        if function not in self.functions:
            raise MoodleAccessError(f"The Moodle token cannot call {function} (accessexception)")
        handler = getattr(self, f"_{function}")
        return handler(**params)

    def download(self, file_url: str) -> str:
        self.downloads.append(file_url)
        if file_url in self.unreadable_files:
            raise MoodleAPIError("Moodle request failed for pluginfile: HTTP 500")
        for course in self.courses.values():
            for book in course["books"].values():
                for chapter in book.get("_chapters", {}).values():
                    if chapter["fileurl"] == file_url:
                        return chapter["body"]
        raise MoodleNotFoundError("Moodle record not found (invalidrecord)")

    # -- functions ---------------------------------------------------------
    def _core_webservice_get_site_info(self):
        return {
            "sitename": "Fake Moodle",
            "username": "student",
            "userid": self.user_id,
            "siteurl": BASE,
            "release": "5.0",
            "functions": [{"name": name, "version": "2026010100"} for name in self.functions],
            "downloadfiles": 1,
        }

    def _core_enrol_get_users_courses(self, userid):
        assert userid == self.user_id
        return [self._public_course(course) for course in self.courses.values()]

    def _public_course(self, course):
        return {
            k: v
            for k, v in course.items()
            if k not in ("sections", "pages", "books", "assignments", "glossaries", "forums")
        }

    def _course(self, course_id):
        course = self.courses.get(int(course_id))
        if course is None:
            raise MoodleNotFoundError("Moodle record not found (invalidrecord)")
        return course

    def _core_course_get_contents(self, courseid, options=None):
        course = self._course(courseid)
        sections = []
        for section in course["sections"]:
            # students never receive hidden modules; a teacher (user 1) gets them flagged
            modules = [dict(m) for m in section["modules"] if m["visible"] or self.user_id == 1]
            sections.append(
                {**{k: v for k, v in section.items() if k != "modules"}, "modules": modules}
            )
        return sections

    def _visible_cmids(self, course):
        return {
            m["id"]
            for s in course["sections"]
            for m in s["modules"]
            if m["visible"] or self.user_id == 1
        }

    def _by_courses(self, courseids, store, key, visible_key):
        records = []
        for course_id in courseids:
            course = self._course(course_id)
            cmids = self._visible_cmids(course)
            for record in course[store].values():
                if record[visible_key] in cmids:
                    records.append({k: v for k, v in record.items() if not k.startswith("_")})
        return {key: records, "warnings": []}

    def _mod_page_get_pages_by_courses(self, courseids):
        return self._by_courses(courseids, "pages", "pages", "coursemodule")

    def _mod_book_get_books_by_courses(self, courseids):
        return self._by_courses(courseids, "books", "books", "coursemodule")

    def _mod_glossary_get_glossaries_by_courses(self, courseids):
        return self._by_courses(courseids, "glossaries", "glossaries", "coursemodule")

    def _mod_forum_get_forums_by_courses(self, courseids):
        return self._by_courses(courseids, "forums", "forums", "cmid")["forums"]

    def _mod_assign_get_assignments(self, courseids, **_):
        courses = []
        for course_id in courseids:
            course = self._course(course_id)
            cmids = self._visible_cmids(course)
            courses.append(
                {
                    "id": course["id"],
                    "fullname": course["fullname"],
                    "shortname": course["shortname"],
                    "timemodified": 0,
                    "assignments": [
                        dict(a) for a in course["assignments"].values() if a["cmid"] in cmids
                    ],
                }
            )
        return {"courses": courses, "warnings": []}

    def _mod_glossary_get_entries_by_letter(self, id, letter, limit, **params):
        start = params.get("from", 0)
        for course in self.courses.values():
            for glossary in course["glossaries"].values():
                if glossary["id"] == id:
                    entries = sorted(glossary["_entries"].values(), key=lambda e: e["id"])
                    return {
                        "count": len(entries),
                        "entries": entries[start : start + limit],
                        "warnings": [],
                    }
        raise MoodleNotFoundError("Moodle record not found (invalidrecord)")

    def _mod_forum_get_forum_discussions(self, forumid, page=0, perpage=0, **_):
        for course in self.courses.values():
            for forum in course["forums"].values():
                if forum["id"] == forumid:
                    discussions = sorted(
                        forum["_discussions"].values(), key=lambda d: d["discussion"]
                    )
                    public = [
                        {k: v for k, v in d.items() if not k.startswith("_")} for d in discussions
                    ]
                    if perpage:
                        public = public[page * perpage : (page + 1) * perpage]
                    return {"discussions": public, "warnings": []}
        raise MoodleNotFoundError("Moodle record not found (invalidrecord)")

    def _mod_forum_get_discussion_posts(self, discussionid, **_):
        for course in self.courses.values():
            for forum in course["forums"].values():
                discussion = forum["_discussions"].get(discussionid)
                if discussion is not None:
                    return {
                        "posts": [dict(post) for post in discussion["_posts"]],
                        "forumid": forum["id"],
                        "courseid": course["id"],
                        "warnings": [],
                    }
        raise MoodleNotFoundError("Moodle record not found (invalidrecord)")
