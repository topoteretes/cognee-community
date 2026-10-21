"""Moodle connector for cognee: a dlt resource that syncs courses into memory.

It reads the course material the token's user can see (sections, pages, books,
assignments, glossaries, announcements and, when asked for, forum discussions), never
grades, submissions or participant lists, and is meant to be handed to
``cognee.remember``::

    await cognee.remember(
        moodle_source(
            resource_name="moodle_main",
            base_url="https://moodle.example.edu",
            token="<web service token>",
            course_ids=[7],
        ),
        dataset_name="moodle",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
        max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
    )

How a run works
---------------
Every course item becomes one document row keyed ``"<kind>:<course id>:<moodle id>"``.
A run calls Moodle's web service functions per course, compares every item's
``timemodified`` with the stamp kept in dlt's resource state, renders only the items
that moved (reading forum posts and book chapters only then), and emits a row only when
the rendered text really changed. Items that stopped appearing in a complete listing are
emitted as ``_deleted`` tombstones, so hiding or deleting something in Moodle forgets it
in cognee on the next sync. A run that is cut short (request budget, server errors)
keeps the progress it made and never tombstones anything.
"""

import hashlib
import html
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, NODE_SET_COLUMN

logger = get_logger("moodle_connector")

REST_PATH = "/webservice/rest/server.php"
# the kinds of course material this source can sync; forums are opt-in (students write them)
KINDS = ("pages", "books", "assignments", "glossaries", "announcements", "forums")
DEFAULT_INCLUDE = ("pages", "books", "assignments", "glossaries", "announcements")
# requests one run may make; a big site finishes over several runs
DEFAULT_MAX_REQUESTS = 500
# forum discussions and glossary entries are read in pages of this size
PAGE_SIZE = 100
# (connect, read) seconds
_TIMEOUT = (10, 60)
# attempts per request on 5xx and network errors
_RETRIES = 4
# posts rendered per discussion
_THREAD_LIMIT = 500
# moodle's "news" forum type is the course announcements forum
_NEWS_FORUM = "news"

# the web service functions each kind needs; the site info check names the missing ones
_ALWAYS = (
    "core_webservice_get_site_info",
    "core_enrol_get_users_courses",
    "core_course_get_contents",
)
_FUNCTIONS = {
    "pages": ("mod_page_get_pages_by_courses",),
    "books": ("mod_book_get_books_by_courses",),
    "assignments": ("mod_assign_get_assignments",),
    "glossaries": ("mod_glossary_get_glossaries_by_courses", "mod_glossary_get_entries_by_letter"),
    "announcements": (
        "mod_forum_get_forums_by_courses",
        "mod_forum_get_forum_discussions",
        "mod_forum_get_discussion_posts",
    ),
    "forums": (
        "mod_forum_get_forums_by_courses",
        "mod_forum_get_forum_discussions",
        "mod_forum_get_discussion_posts",
    ),
}

_AUTH_CODES = ("invalidtoken", "accessexception", "invalidlogin", "sitemaintenance")
_ACCESS_CODES = (
    "nopermissions",
    "requireloginerror",
    "webservice_access_exception",
    "accessdenied",
    "nopermissiontoviewpage",
    "cannotviewprofile",
    "noviewdiscussionspermission",
    "nopermissiontoviewdiscussion",
)
_GONE_CODES = (
    "invalidrecord",
    "invalidcoursemodule",
    "invalidcourseid",
    "cannotfindcourse",
    "notexist",
    "invalidcourse",
    "coursemisconf",
    "invalidmodule",
    "notfound",
)


class MoodleSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class MoodleAPIError(MoodleSourceError):
    """Moodle answered with an error, or could not be reached."""


class MoodleAuthError(MoodleSourceError):
    """Moodle rejected the web service token."""


class MoodleAccessError(MoodleSourceError):
    """The token's user or service may not call a function or read a record."""


class MoodleNotFoundError(MoodleAPIError):
    """The record was deleted or never existed."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Encode nested parameters the way Moodle's REST server reads them.

    ``{"courseids": [1, 2]}`` becomes ``courseids[0]=1&courseids[1]=2`` and
    ``{"options": {"includenotapproved": 1}}`` becomes ``options[includenotapproved]=1``.
    """
    flat: dict[str, Any] = {}
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            flat.update(_flatten(dict(enumerate(value)), name))
        elif isinstance(value, bool):
            flat[name] = int(value)
        elif value is not None:
            flat[name] = value
    return flat


def _is_local(base: str) -> bool:
    host = base.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host in ("localhost", "127.0.0.1", "[::1]") or host.endswith((".localhost", ".test"))


class MoodleClient:
    """Minimal synchronous client for Moodle's REST web services.

    Every call is a POST with the token in the form body, never in the URL, so it
    cannot end up in a server access log. Tests pass any object with the same ``call``
    and ``download`` methods instead.
    """

    def __init__(self, base_url: str, token: str, *, sleep: Callable[[float], None] = time.sleep):
        key = (token or "").strip()
        # reject early and without echoing the token: requests would put it in its exception
        if not key or not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError("The Moodle web service token is empty or has an invalid format")
        base = (base_url or "").strip().rstrip("/")
        # a plain-http moodle would send the token in clear; only a local test site may
        if not (base.startswith("https://") or (base.startswith("http://") and _is_local(base))):
            raise ValueError(
                "base_url must be the https:// address of your Moodle site "
                "(http:// is accepted for localhost only)"
            )
        self.base_url = base
        self._token = key
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"MoodleClient(base_url={self.base_url!r}, token=<redacted>)"

    def call(self, function: str, **params: Any) -> Any:
        """Call a web service function and return its decoded JSON answer."""
        data = {
            "wstoken": self._token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
            **_flatten(params),
        }
        response = self._post(f"{self.base_url}{REST_PATH}", data, function)
        try:
            payload = response.json()
        except ValueError:
            raise MoodleAPIError(f"Moodle sent an unreadable answer for {function}") from None
        if isinstance(payload, dict) and payload.get("exception"):
            raise _error_for(payload, function)
        return payload

    def download(self, file_url: str) -> str:
        """Read a ``webservice/pluginfile.php`` file as text, sending the token in the body."""
        if not file_url.startswith(self.base_url + "/"):
            raise MoodleAccessError("Moodle offered a file outside the site; it was not read")
        response = self._post(file_url, {"token": self._token}, "pluginfile")
        text = response.text
        if text.lstrip().startswith("{"):
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("exception"):
                raise _error_for(payload, "pluginfile")
        return text

    def _post(self, url: str, data: dict[str, Any], what: str) -> Any:
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = requests.post(url, data=data, timeout=_TIMEOUT)
            except requests.RequestException as exc:
                if last:
                    raise MoodleAPIError(f"Moodle request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue
            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue
            if response.status_code != 200:
                raise MoodleAPIError(
                    f"Moodle request failed for {what}: HTTP {response.status_code}"
                )
            return response
        raise MoodleAPIError("Moodle request failed")  # pragma: no cover - loop always returns


def _error_for(payload: dict[str, Any], function: str) -> MoodleSourceError:
    """Map Moodle's ``{exception, errorcode, message}`` answer to a typed error.

    Only the error code is kept: the message can quote site data, so it is never
    carried into an exception that may be logged.
    """
    code = str(payload.get("errorcode") or payload.get("exception") or "unknown")
    if code in _AUTH_CODES:
        return MoodleAuthError(f"Moodle rejected the web service token ({code})")
    if code in _ACCESS_CODES:
        return MoodleAccessError(f"The Moodle token cannot call {function} ({code})")
    if code in _GONE_CODES or code.startswith("invalid"):
        return MoodleNotFoundError(f"Moodle record not found ({code})")
    return MoodleAPIError(f"Moodle request failed for {function} ({code})")


# ---------------------------------------------------------------------------
# Rendering: deterministic, readable text, nothing volatile, no URLs from bodies
# ---------------------------------------------------------------------------
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
# anchors keep their text and lose their href, whatever it points at: this is the
# whole file-link guard (webservice/pluginfile.php links and tokens never survive)
_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a\s*>", re.I | re.S)
_IMG_RE = re.compile(r"<img\b([^>]*)>", re.I)
_ALT_RE = re.compile(r'\balt\s*=\s*"([^"]*)"', re.I)
_LI_RE = re.compile(r"<li\b[^>]*>", re.I)
_LIST_END_RE = re.compile(r"</(?:ul|ol)\s*>", re.I)
_BLOCK_RE = re.compile(
    r"</(?:p|div|h[1-6]|tr|blockquote|pre|table|section|article)\s*>|<br\s*/?>|<hr\s*/?>",
    re.I,
)
_CELL_RE = re.compile(r"</t[dh]\s*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\r\f\v\xa0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# a stray placeholder moodle did not rewrite
_PLUGINFILE_RE = re.compile(r"@@PLUGINFILE@@\S*")


def _image_alt(match: re.Match) -> str:
    alt = _ALT_RE.search(match.group(1))
    text = alt.group(1).strip() if alt else ""
    return f"\n[image: {text}]\n" if text else ""


def _clean_html(raw: Any) -> str:
    """Moodle HTML to plain text that keeps headings, paragraphs and list items."""
    if not isinstance(raw, str) or not raw:
        return ""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _ANCHOR_RE.sub(r"\1", text)
    text = _IMG_RE.sub(_image_alt, text)
    text = _LI_RE.sub("\n- ", text)
    text = _LIST_END_RE.sub("\n\n", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _CELL_RE.sub(" ", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _PLUGINFILE_RE.sub("", text)
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _text(value: Any) -> str:
    """A Moodle string as plain text: web services HTML-encode names (``Q&amp;A``)."""
    if isinstance(value, list):
        return ", ".join(_text(item) for item in value if _text(item))
    return "" if value is None else html.unescape(str(value)).strip()


def _when(timestamp: Any) -> str:
    """A Moodle unix timestamp as ``YYYY-MM-DD HH:MM`` UTC; zero and None are blank."""
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _date(timestamp: Any) -> str:
    return _when(timestamp)[:10]


def _lines(fields: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in fields if value]


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _row(row_id: str, title: str, parts: list[str], url: Any, course_id: str) -> dict[str, Any]:
    """The frozen row shape every live document has."""
    content = "\n".join(parts).strip()
    return {
        "id": row_id,
        "title": title,
        "content": content or title,
        # not _text(): unescaping would turn "&section=" into "§ion="
        "url": "" if url is None else str(url).strip(),
        # cognee namespaces this under the source tag: "moodle:course:<id>"
        NODE_SET_COLUMN: [f"course:{course_id}"],
        "_deleted": False,
    }


def _course_name(course: dict[str, Any]) -> str:
    return _text(course.get("fullname")) or _text(course.get("shortname")) or str(course.get("id"))


def render_course(course: dict[str, Any], sections: list[dict[str, Any]], base_url: str) -> dict:
    course_id = str(course["id"])
    title = _course_name(course)
    parts = _lines(
        [
            ("Short name", _text(course.get("shortname"))),
            ("Starts", _date(course.get("startdate"))),
            ("Ends", _date(course.get("enddate"))),
        ]
    )
    summary = _clean_html(course.get("summary"))
    if summary:
        parts.extend(["", summary])
    names = [_text(section.get("name")) for section in sections if _text(section.get("name"))]
    if names:
        parts.extend(["", "Sections:", *[f"- {name}" for name in names]])
    return _row(
        f"course:{course_id}", title, parts, f"{base_url}/course/view.php?id={course_id}", course_id
    )


def render_section(
    course: dict[str, Any], section: dict[str, Any], modules: list[dict], base_url: str
):
    course_id = str(course["id"])
    name = _text(section.get("name")) or f"Section {section.get('section', '')}".strip()
    parts = _lines([("Course", _course_name(course))])
    summary = _clean_html(section.get("summary"))
    if summary:
        parts.extend(["", summary])
    listed = [module for module in modules if _text(module.get("name"))]
    if listed:
        parts.append("")
    for module in listed:
        if module.get("modname") == "label":
            parts.append(_clean_html(module.get("description")) or _text(module.get("name")))
        else:
            kind = _text(module.get("modplural") or module.get("modname")) or "Activity"
            parts.append(f"- {kind}: {_text(module.get('name'))}")
    url = f"{base_url}/course/view.php?id={course_id}&section={section.get('section', 0)}"
    return _row(f"section:{course_id}:{section['id']}", f"Section: {name}", parts, url, course_id)


def _module_header(course: dict[str, Any], module: dict[str, Any], section: str) -> list[str]:
    return _lines([("Course", _course_name(course)), ("Section", section)])


def render_page(course: dict[str, Any], page: dict[str, Any], section: str, base_url: str) -> dict:
    course_id = str(course["id"])
    title = _text(page.get("name")) or "Untitled page"
    parts = _module_header(course, page, section)
    intro = _clean_html(page.get("intro"))
    content = _clean_html(page.get("content"))
    for text in (intro, content):
        if text:
            parts.extend(["", text])
    url = f"{base_url}/mod/page/view.php?id={page['coursemodule']}"
    return _row(f"page:{course_id}:{page['coursemodule']}", title, parts, url, course_id)


def render_book(
    course: dict[str, Any], book: dict[str, Any], chapters: list[str], section: str, base_url: str
):
    course_id = str(course["id"])
    title = _text(book.get("name")) or "Untitled book"
    parts = _module_header(course, book, section)
    intro = _clean_html(book.get("intro"))
    if intro:
        parts.extend(["", intro])
    if chapters:
        parts.extend(["", "Chapters:", *[f"- {chapter}" for chapter in chapters]])
    url = f"{base_url}/mod/book/view.php?id={book['coursemodule']}"
    return _row(f"book:{course_id}:{book['coursemodule']}", title, parts, url, course_id)


def render_chapter(
    course: dict[str, Any], book: dict[str, Any], chapter: dict[str, Any], body: str, base_url: str
) -> dict:
    course_id = str(course["id"])
    cmid = book["coursemodule"]
    chapter_title = _text(chapter.get("title")) or "Untitled chapter"
    parts = _lines([("Course", _course_name(course)), ("Book", _text(book.get("name")))])
    text = _clean_html(body)
    if text:
        parts.extend(["", text])
    url = f"{base_url}/mod/book/view.php?id={cmid}&chapterid={chapter['id']}"
    return _row(
        f"chapter:{course_id}:{cmid}:{chapter['id']}",
        f"{_text(book.get('name'))}: {chapter_title}",
        parts,
        url,
        course_id,
    )


def render_assignment(
    course: dict[str, Any], assignment: dict[str, Any], section: str, base_url: str
):
    course_id = str(course["id"])
    title = _text(assignment.get("name")) or "Untitled assignment"
    parts = _module_header(course, assignment, section) + _lines(
        [
            ("Due", _when(assignment.get("duedate"))),
            ("Submissions from", _when(assignment.get("allowsubmissionsfromdate"))),
            ("Cut-off", _when(assignment.get("cutoffdate"))),
        ]
    )
    for field in ("intro", "activity"):
        text = _clean_html(assignment.get(field))
        if text:
            parts.extend(["", text])
    url = f"{base_url}/mod/assign/view.php?id={assignment['cmid']}"
    return _row(f"assignment:{course_id}:{assignment['cmid']}", title, parts, url, course_id)


def render_glossary(course: dict[str, Any], glossary: dict[str, Any], section: str, base_url: str):
    course_id = str(course["id"])
    title = _text(glossary.get("name")) or "Untitled glossary"
    parts = _module_header(course, glossary, section)
    intro = _clean_html(glossary.get("intro"))
    if intro:
        parts.extend(["", intro])
    url = f"{base_url}/mod/glossary/view.php?id={glossary['coursemodule']}"
    return _row(f"glossary:{course_id}:{glossary['coursemodule']}", title, parts, url, course_id)


def render_entry(
    course: dict[str, Any], glossary: dict[str, Any], entry: dict[str, Any], base_url: str
):
    course_id = str(course["id"])
    concept = _text(entry.get("concept")) or "Untitled entry"
    parts = _lines([("Course", _course_name(course)), ("Glossary", _text(glossary.get("name")))])
    definition = _clean_html(entry.get("definition"))
    if definition:
        parts.extend(["", definition])
    url = f"{base_url}/mod/glossary/showentry.php?eid={entry['id']}"
    return _row(
        f"entry:{course_id}:{glossary['coursemodule']}:{entry['id']}",
        concept,
        parts,
        url,
        course_id,
    )


def render_forum(
    course: dict[str, Any], forum: dict[str, Any], section: str, base_url: str
) -> dict:
    course_id = str(course["id"])
    title = _text(forum.get("name")) or "Untitled forum"
    parts = _module_header(course, forum, section)
    intro = _clean_html(forum.get("intro"))
    if intro:
        parts.extend(["", intro])
    url = f"{base_url}/mod/forum/view.php?id={forum['cmid']}"
    return _row(f"forum:{course_id}:{forum['cmid']}", title, parts, url, course_id)


def render_discussion(
    course: dict[str, Any],
    forum: dict[str, Any],
    discussion: dict[str, Any],
    posts: list[dict[str, Any]],
    base_url: str,
    *,
    kind: str = "discussion",
    anonymize: bool = True,
) -> dict:
    """A discussion with its posts as one document; announcements use the same shape."""
    course_id = str(course["id"])
    title = (
        _text(discussion.get("name")) or _text(discussion.get("subject")) or "Untitled discussion"
    )
    parts = _lines(
        [
            ("Course", _course_name(course)),
            ("Forum", _text(forum.get("name"))),
            ("Posted", _date(discussion.get("created"))),
            ("Replies", _text(discussion.get("numreplies"))),
        ]
    )
    thread = _render_thread(posts, anonymize)
    if thread:
        parts.extend(["", *thread])
    url = f"{base_url}/mod/forum/discuss.php?d={discussion['discussion']}"
    return _row(f"{kind}:{course_id}:{discussion['discussion']}", title, parts, url, course_id)


def _render_thread(posts: list[dict[str, Any]], anonymize: bool) -> list[str]:
    """Forum posts as a tree, oldest first, two spaces per reply level.

    With ``anonymize`` authors are numbered by first appearance ("Participant 1"), so
    student names never reach memory. Deleted posts keep their place as "[deleted]" so
    the replies under them still read as replies.
    """
    by_parent: dict[Any, list[dict[str, Any]]] = {}
    ids = {post.get("id") for post in posts if isinstance(post, dict)}
    for post in posts:
        if not isinstance(post, dict):
            continue
        parent = post.get("parentid") if post.get("hasparent", post.get("parentid")) else None
        if parent not in ids:
            parent = None
        by_parent.setdefault(parent, []).append(post)
    for children in by_parent.values():
        children.sort(key=lambda post: (post.get("timecreated") or 0, str(post.get("id"))))
    labels: dict[Any, str] = {}
    lines: list[str] = []
    count = 0

    def label(post: dict[str, Any]) -> str:
        author = post.get("author") if isinstance(post.get("author"), dict) else {}
        user_id = author.get("id")
        if anonymize:
            return labels.setdefault(user_id, f"Participant {len(labels) + 1}")
        return _text(author.get("fullname")) or "Unknown"

    def walk(parent: Any, depth: int) -> None:
        nonlocal count
        for post in by_parent.get(parent, []):
            if count >= _THREAD_LIMIT:
                return
            count += 1
            indent = "  " * depth
            message = _clean_html(post.get("message"))
            if post.get("isdeleted") or not message:
                lines.append(f"{indent}[deleted]")
            else:
                message = message.replace("\n", "\n" + indent + "  ")
                lines.append(f"{indent}{label(post)} ({_date(post.get('timecreated'))}): {message}")
            walk(post.get("id"), depth + 1)

    walk(None, 0)
    if count >= _THREAD_LIMIT:
        lines.append(f"(thread cut at {_THREAD_LIMIT} posts)")
    return lines


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class _CutShortError(Exception):
    """Internal: stop the run here, keeping the state reached so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _course_of(row_id: str) -> str:
    """The course id inside a row id ("page:7:12" and "course:7" both give "7")."""
    parts = row_id.split(":")
    return parts[1] if len(parts) > 1 else ""


def _visible(module: dict[str, Any]) -> bool:
    """Whether the token's user may see this module right now.

    ``visible`` is 0 for modules a teacher hid (students never receive those), and
    ``uservisible`` is False for modules the user is restricted from, so a student's
    memory holds only what that student can open.
    """
    if module.get("visible") in (0, False):
        return False
    return module.get("uservisible") is not False


_KIND_PREFIXES = {
    "page": "pages",
    "book": "books",
    "chapter": "books",
    "assignment": "assignments",
    "glossary": "glossaries",
    "entry": "glossaries",
    "announcement": "announcements",
    "forum": "forums",
    "discussion": "forums",
}


class _Walker:
    """One run over the selected courses, writing progress into ``state``."""

    def __init__(
        self,
        client: Any,
        state: dict,
        stats: dict[str, int],
        *,
        base_url: str,
        course_ids: list[int] | None,
        include: tuple[str, ...],
        anonymize_authors: bool,
        max_requests: int,
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "emitted", "deleted", "failed"):
            stats.setdefault(key, 0)
        self.base_url = base_url
        self.course_ids = course_ids
        self.include = include
        self.anonymize = anonymize_authors
        self.max_requests = max_requests
        self.check_active = check_active
        self.requests = 0
        self.emitted = 0
        # selected courses this run could not read: kept as they are, never swept
        self.unreadable: set[str] = set()
        self.items: dict[str, dict[str, str]] = state.setdefault("items", {})
        self.pending: list[str] = state.setdefault("pending_deletes", [])

    # -- requests ----------------------------------------------------------
    def _call(self, function: str, **params: Any) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.call(function, **params)
        except MoodleAuthError:
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    def _download(self, file_url: str) -> str | None:
        """A file's text; a failure keeps the item as it was and the run goes on."""
        if self.check_active is not None:
            self.check_active()
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.download(file_url)
        except (MoodleAccessError, MoodleAPIError) as exc:
            self._failed("file read", exc)
            return None

    def _detail(self, function: str, **params: Any) -> Any:
        """A single item's read; a failure keeps the item as it was and the run goes on."""
        try:
            return self._call(function, **params)
        except (MoodleAccessError, MoodleAPIError) as exc:
            self._failed(function, exc)
            return None

    def _failed(self, step: str, exc: Exception) -> None:
        self.stats["failed"] += 1
        # the message names the function and error code, never the record or the token
        logger.warning("Moodle %s failed for one item, retried next run: %s", step, exc)

    # -- change detection --------------------------------------------------
    def _consider(
        self,
        row_id: str,
        stamp: str | None,
        seen: set[str],
        build: Callable[[], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Decide whether ``row_id`` needs a new document.

        ``stamp`` is the item's cheap change marker (``timemodified``); when it matches
        the stored one nothing more is read. Otherwise the row is built and emitted only
        when its rendered text changed, so a metadata-only edit costs no re-cognify.
        Kinds without a stamp pass None and are compared on the rendered text alone.
        """
        seen.add(row_id)
        self.stats["scanned"] += 1
        known = self.items.get(row_id)
        if stamp is not None and known and known.get("stamp") == stamp:
            self.stats["skipped"] += 1
            return None
        row = build()
        if row is None:
            return None
        fingerprint = _hash(row)
        self.items[row_id] = {
            "stamp": stamp if stamp is not None else fingerprint,
            "fp": fingerprint,
        }
        if known and known.get("fp") == fingerprint:
            self.stats["skipped"] += 1
            return None
        self.stats["emitted"] += 1
        self.emitted += 1
        return row

    # -- site and courses --------------------------------------------------
    def _site(self) -> dict[str, Any]:
        info = self._call("core_webservice_get_site_info")
        if not isinstance(info, dict) or info.get("userid") is None:
            raise MoodleAPIError("Moodle sent no site info for core_webservice_get_site_info")
        available = {
            function.get("name")
            for function in info.get("functions") or []
            if isinstance(function, dict)
        }
        needed = set(_ALWAYS)
        for kind in self.include:
            needed.update(_FUNCTIONS[kind])
        missing = sorted(needed - available)
        if missing:
            raise MoodleAccessError(
                "The Moodle web service behind this token does not expose "
                + ", ".join(missing)
                + ". Add these functions to the external service, or use the built-in "
                "'Moodle mobile web service'."
            )
        return info

    def _courses(self, user_id: int) -> list[dict[str, Any]]:
        listed = self._call("core_enrol_get_users_courses", userid=user_id)
        if not isinstance(listed, list):
            raise MoodleAPIError("Moodle sent no list for core_enrol_get_users_courses")
        courses = [
            course for course in listed if isinstance(course, dict) and course.get("id") is not None
        ]
        if self.course_ids is None:
            return [course for course in courses if course.get("visible", 1) not in (0, False)]
        by_id = {int(course["id"]): course for course in courses}
        selected = []
        for course_id in self.course_ids:
            course = by_id.get(int(course_id))
            if course is None:
                # not enrolled, or the course is gone: left as it is, remove it from
                # course_ids to forget its documents
                self.unreadable.add(str(int(course_id)))
                self._failed(
                    "course lookup",
                    MoodleNotFoundError("the user is not enrolled in a selected course"),
                )
                continue
            selected.append(course)
        return selected

    # -- one course --------------------------------------------------------
    def _course_rows(self, course: dict[str, Any], seen: set[str]) -> Iterator[dict[str, Any]]:
        course_id = str(course["id"])
        contents = self._call("core_course_get_contents", courseid=int(course_id))
        if not isinstance(contents, list):
            raise MoodleAPIError("Moodle sent no list for core_course_get_contents")
        sections = [
            s
            for s in contents
            if isinstance(s, dict)
            and s.get("uservisible") is not False
            and s.get("visible", 1) not in (0, False)
        ]
        # cmid → section name, and cmid → the module's listing (for book chapters)
        section_of: dict[str, str] = {}
        modules_by_cmid: dict[str, dict[str, Any]] = {}
        for section in sections:
            visible_modules = [
                m for m in section.get("modules") or [] if isinstance(m, dict) and _visible(m)
            ]
            section["_modules"] = visible_modules
            for module in visible_modules:
                section_of[str(module.get("id"))] = _text(section.get("name"))
                modules_by_cmid[str(module.get("id"))] = module

        row = self._consider(
            f"course:{course_id}",
            None,
            seen,
            lambda: render_course(course, sections, self.base_url),
        )
        if row:
            yield row
        for section in sections:
            row = self._consider(
                f"section:{course_id}:{section['id']}",
                None,
                seen,
                lambda section=section: render_section(
                    course, section, section["_modules"], self.base_url
                ),
            )
            if row:
                yield row

        if "pages" in self.include:
            yield from self._pages(course, seen, section_of, modules_by_cmid)
        if "books" in self.include:
            yield from self._books(course, seen, section_of, modules_by_cmid)
        if "assignments" in self.include:
            yield from self._assignments(course, seen, section_of, modules_by_cmid)
        if "glossaries" in self.include:
            yield from self._glossaries(course, seen, section_of, modules_by_cmid)
        if "announcements" in self.include or "forums" in self.include:
            yield from self._forums(course, seen, section_of, modules_by_cmid)

    def _listing(self, function: str, key: str, course_id: str) -> list[dict[str, Any]]:
        answer = self._call(function, courseids=[int(course_id)])
        records = answer.get(key) if isinstance(answer, dict) else None
        if not isinstance(records, list):
            raise MoodleAPIError(f"Moodle sent no {key} for {function}")
        return [record for record in records if isinstance(record, dict)]

    def _pages(self, course, seen, section_of, modules_by_cmid) -> Iterator[dict[str, Any]]:
        course_id = str(course["id"])
        for page in self._listing("mod_page_get_pages_by_courses", "pages", course_id):
            cmid = str(page.get("coursemodule"))
            if cmid not in modules_by_cmid:
                continue  # hidden or restricted: the course contents left it out
            row = self._consider(
                f"page:{course_id}:{cmid}",
                _text(page.get("timemodified")),
                seen,
                lambda page=page, cmid=cmid: render_page(
                    course, page, section_of.get(cmid, ""), self.base_url
                ),
            )
            if row:
                yield row

    def _books(self, course, seen, section_of, modules_by_cmid) -> Iterator[dict[str, Any]]:
        course_id = str(course["id"])
        for book in self._listing("mod_book_get_books_by_courses", "books", course_id):
            cmid = str(book.get("coursemodule"))
            module = modules_by_cmid.get(cmid)
            if module is None:
                continue
            # a book's chapters are the index.html entries of its module contents
            chapters = [
                {
                    "id": content["filepath"].strip("/"),
                    "title": _text(content.get("content")),
                    "fileurl": _text(content.get("fileurl")),
                    "timemodified": content.get("timemodified"),
                }
                for content in module.get("contents") or []
                if isinstance(content, dict)
                and content.get("filename") == "index.html"
                and _text(content.get("filepath")).strip("/").isdigit()
            ]
            titles = [chapter["title"] for chapter in chapters if chapter["title"]]
            row = self._consider(
                f"book:{course_id}:{cmid}",
                f"{_text(book.get('timemodified'))}|{_hash(titles)}",
                seen,
                lambda book=book, cmid=cmid, titles=titles: render_book(
                    course, book, titles, section_of.get(cmid, ""), self.base_url
                ),
            )
            if row:
                yield row
            for chapter in chapters:

                def build(book=book, chapter=chapter) -> dict[str, Any] | None:
                    body = self._download(chapter["fileurl"]) if chapter["fileurl"] else ""
                    if body is None:
                        return None
                    return render_chapter(course, book, chapter, body, self.base_url)

                row = self._consider(
                    f"chapter:{course_id}:{cmid}:{chapter['id']}",
                    f"{_text(chapter['timemodified'])}|{chapter['title']}",
                    seen,
                    build,
                )
                if row:
                    yield row

    def _assignments(self, course, seen, section_of, modules_by_cmid) -> Iterator[dict[str, Any]]:
        course_id = str(course["id"])
        answer = self._call("mod_assign_get_assignments", courseids=[int(course_id)])
        courses = answer.get("courses") if isinstance(answer, dict) else None
        if not isinstance(courses, list):
            raise MoodleAPIError("Moodle sent no courses for mod_assign_get_assignments")
        for listed in courses:
            for assignment in (listed.get("assignments") or []) if isinstance(listed, dict) else []:
                if not isinstance(assignment, dict):
                    continue
                cmid = str(assignment.get("cmid"))
                if cmid not in modules_by_cmid:
                    continue
                row = self._consider(
                    f"assignment:{course_id}:{cmid}",
                    _text(assignment.get("timemodified")),
                    seen,
                    lambda assignment=assignment, cmid=cmid: render_assignment(
                        course, assignment, section_of.get(cmid, ""), self.base_url
                    ),
                )
                if row:
                    yield row

    def _glossaries(self, course, seen, section_of, modules_by_cmid) -> Iterator[dict[str, Any]]:
        course_id = str(course["id"])
        for glossary in self._listing(
            "mod_glossary_get_glossaries_by_courses", "glossaries", course_id
        ):
            cmid = str(glossary.get("coursemodule"))
            if cmid not in modules_by_cmid:
                continue
            row = self._consider(
                f"glossary:{course_id}:{cmid}",
                _text(glossary.get("timemodified")),
                seen,
                lambda glossary=glossary, cmid=cmid: render_glossary(
                    course, glossary, section_of.get(cmid, ""), self.base_url
                ),
            )
            if row:
                yield row
            for entry in self._entries(glossary):
                if entry.get("approved") is False:
                    continue
                row = self._consider(
                    f"entry:{course_id}:{cmid}:{entry['id']}",
                    _text(entry.get("timemodified")),
                    seen,
                    lambda glossary=glossary, entry=entry: render_entry(
                        course, glossary, entry, self.base_url
                    ),
                )
                if row:
                    yield row

    def _entries(self, glossary: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        start = 0
        while True:
            answer = self._call(
                "mod_glossary_get_entries_by_letter",
                id=int(glossary["id"]),
                letter="ALL",
                **{"from": start},
                limit=PAGE_SIZE,
            )
            page = answer.get("entries") if isinstance(answer, dict) else None
            if not isinstance(page, list):
                raise MoodleAPIError(
                    "Moodle sent no entries for mod_glossary_get_entries_by_letter"
                )
            entries.extend(
                entry for entry in page if isinstance(entry, dict) and entry.get("id") is not None
            )
            start += len(page)
            if len(page) < PAGE_SIZE or start >= int(answer.get("count") or 0):
                return entries

    def _forums(self, course, seen, section_of, modules_by_cmid) -> Iterator[dict[str, Any]]:
        course_id = str(course["id"])
        forums = self._call("mod_forum_get_forums_by_courses", courseids=[int(course_id)])
        if not isinstance(forums, list):
            raise MoodleAPIError("Moodle sent no list for mod_forum_get_forums_by_courses")
        for forum in forums:
            if not isinstance(forum, dict):
                continue
            cmid = str(forum.get("cmid"))
            if cmid not in modules_by_cmid:
                continue
            is_news = forum.get("type") == _NEWS_FORUM
            kind = "announcement" if is_news else "discussion"
            if (is_news and "announcements" not in self.include) or (
                not is_news and "forums" not in self.include
            ):
                continue
            if not is_news:
                row = self._consider(
                    f"forum:{course_id}:{cmid}",
                    _text(forum.get("timemodified")),
                    seen,
                    lambda forum=forum, cmid=cmid: render_forum(
                        course, forum, section_of.get(cmid, ""), self.base_url
                    ),
                )
                if row:
                    yield row
            for discussion in self._discussions(forum):

                def build(forum=forum, discussion=discussion, kind=kind) -> dict[str, Any] | None:
                    answer = self._detail(
                        "mod_forum_get_discussion_posts",
                        discussionid=int(discussion["discussion"]),
                        sortby="created",
                        sortdirection="ASC",
                    )
                    posts = answer.get("posts") if isinstance(answer, dict) else None
                    if not isinstance(posts, list):
                        return None
                    return render_discussion(
                        course,
                        forum,
                        discussion,
                        posts,
                        self.base_url,
                        kind=kind,
                        anonymize=self.anonymize,
                    )

                row = self._consider(
                    f"{kind}:{course_id}:{discussion['discussion']}",
                    f"{_text(discussion.get('timemodified'))}|{_text(discussion.get('numreplies'))}",
                    seen,
                    build,
                )
                if row:
                    yield row

    def _discussions(self, forum: dict[str, Any]) -> list[dict[str, Any]]:
        discussions: list[dict[str, Any]] = []
        page = 0
        while True:
            answer = self._call(
                "mod_forum_get_forum_discussions",
                forumid=int(forum["id"]),
                page=page,
                perpage=PAGE_SIZE,
            )
            listed = answer.get("discussions") if isinstance(answer, dict) else None
            if not isinstance(listed, list):
                raise MoodleAPIError(
                    "Moodle sent no discussions for mod_forum_get_forum_discussions"
                )
            discussions.extend(
                d for d in listed if isinstance(d, dict) and d.get("discussion") is not None
            )
            if len(listed) < PAGE_SIZE:
                return discussions
            page += 1

    # -- forget-on-delete --------------------------------------------------
    def _forget(self) -> Iterator[dict[str, Any]]:
        while self.pending:
            row_id = self.pending[0]
            self.items.pop(row_id, None)
            self.stats["deleted"] += 1
            yield {"id": row_id, "_deleted": True}
            self.pending.pop(0)

    def _queue(self, row_ids: list[str]) -> None:
        for row_id in row_ids:
            if row_id not in self.pending:
                self.pending.append(row_id)

    def _sweep(self, course_id: str, seen: set[str]) -> None:
        """Queue every known row of a fully listed course that this run did not see."""
        known = [row_id for row_id in self.items if _course_of(row_id) == course_id]
        self._queue(sorted(row_id for row_id in known if row_id not in seen))

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict[str, Any]]:
        # finish deletions a cut run left behind before reading anything
        yield from self._forget()
        site = self._site()
        courses = self._courses(int(site["userid"]))
        selected = {str(course["id"]) for course in courses}
        # a course removed from the selection leaves memory, like a deselected kind;
        # one this run could not read is neither selected nor removed, so it stays
        previous = set(self.state.get("courses", []))
        for gone in sorted(previous - selected - self.unreadable):
            self._queue(sorted(row_id for row_id in self.items if _course_of(row_id) == gone))
        yield from self._forget()
        for course in courses:
            course_id = str(course["id"])
            seen: set[str] = set()
            yield from self._course_rows(course, seen)
            # every selected kind listed to the end: what was not seen is gone
            self._sweep(course_id, seen)
            yield from self._forget()
        # deselected kinds leave memory too
        kinds_gone = [
            row_id
            for row_id in self.items
            if row_id.split(":")[0] in _KIND_PREFIXES
            and _KIND_PREFIXES[row_id.split(":")[0]] not in self.include
        ]
        self._queue(sorted(kinds_gone))
        yield from self._forget()
        self.state["courses"] = sorted(selected | self.unreadable)


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    base_url: str = "",
    course_ids: list[int] | None = None,
    include: tuple[str, ...] = DEFAULT_INCLUDE,
    anonymize_authors: bool = True,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield the changed rows of the selected courses.

    Pure of dlt, so tests drive it with a plain dict as the state.
    """
    walker = _Walker(
        client,
        state,
        stats,
        base_url=base_url or getattr(client, "base_url", ""),
        course_ids=course_ids,
        include=include,
        anonymize_authors=anonymize_authors,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Moodle sync stopped early (%s); it resumes on the next run.", cut.reason)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def moodle_source(
    *,
    resource_name: str,
    base_url: str | None = None,
    token: str | None = None,
    course_ids: list[int] | None = None,
    include: tuple[str, ...] = DEFAULT_INCLUDE,
    anonymize_authors: bool = True,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource that yields a Moodle user's course documents for ``remember``.

    Args:
        resource_name: Stable name of this connection, one per Moodle account. It scopes
            the sync state and the cleanup of deleted items, so keep it fixed across runs.
            There is no default: two accounts sharing a name would forget each other's
            documents.
        base_url: Address of the Moodle site (``https://``; ``http://localhost`` for a
            local test site). Falls back to ``MOODLE_BASE_URL``.
        token: Web service token (Preferences > Security keys, or one an admin issued).
            Falls back to ``MOODLE_TOKEN``. Sent only in POST bodies, never in a URL.
        course_ids: Only sync these courses. ``None`` syncs every visible course the
            token's user is enrolled in.
        include: Which of ``"pages"``, ``"books"``, ``"assignments"``, ``"glossaries"``,
            ``"announcements"`` and ``"forums"`` to sync. The course overview and its
            sections are always synced. Forums are off by default because students write
            them; announcements (the course news forum) are on.
        anonymize_authors: Render post authors as "Participant 1", "Participant 2".
        service: Pre-built client, mainly an injection point for tests.
        check_active: Optional host authorization checkpoint, called before each request.
        max_requests: Request budget of one run. A bigger site finishes over several runs.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError("Moodle sync requires cognee>=1.6.3 (per-row node sets).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("moodle_source needs a resource_name, one per Moodle account")
    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")
    if course_ids is not None:
        course_ids = [int(course_id) for course_id in course_ids]

    if service is None:
        base = base_url or os.environ.get("MOODLE_BASE_URL") or ""
        key = token or os.environ.get("MOODLE_TOKEN") or ""
        if not base or not key:
            raise ValueError(
                "moodle_source needs base_url and token (or MOODLE_BASE_URL and MOODLE_TOKEN), "
                "or an injected service"
            )
        # built here so a malformed token fails at construction and the closure holds no raw token
        client: Any = MoodleClient(base, key)
    else:
        client = service
    site_url = getattr(client, "base_url", "") or (base_url or "").rstrip("/")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={
            # rows where _deleted is True are removed from the dlt destination on merge,
            # which cognee's orphan_cleanup propagates to the graph and vector stores
            "_deleted": {"data_type": "bool", "hard_delete": True},
            # the node-set list is stored as json, not normalised into a child table
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def moodle_courses():
        stats.clear()
        stats.update(scanned=0, skipped=0, emitted=0, deleted=0, failed=0)
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            base_url=site_url,
            course_ids=course_ids,
            include=include,
            anonymize_authors=anonymize_authors,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = moodle_courses()
    # opt into the document ingestion path: each row is a text document for cognify
    setattr(resource, DOCUMENT_SOURCE_ATTR, "moodle")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
