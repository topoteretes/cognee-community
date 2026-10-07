"""Stack Overflow connector for cognee — a ``dlt`` source that turns tagged
questions into memory.

Pull Stack Overflow questions (with their accepted/top answers) for a set of
tags into cognee, incrementally and with forget-on-deletion. Like the sibling
Confluence connector this builds entirely on the existing DLT ingestion
subsystem; the source produced here is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_stack_overflow import stack_overflow_source

    await cognee.remember(
        stack_overflow_source(tags=["python", "asyncio"]),
        dataset_name="stack_overflow",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by question id
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — an optional Stack Exchange API key (``api_key=`` /
  ``STACK_OVERFLOW_API_KEY``). Keyless access is capped at 300 requests/day;
  a key raises that to 10,000/day. Access is read-only — the connector only
  issues ``GET`` requests.
* **Scope** — ``tags`` is *required*. Stack Overflow's daily quota is small
  and "sync all of Stack Overflow" is not a meaningful target anyway, so
  callers must scope to the tag(s) they care about.
* **Primary key** — the question ``question_id``. Combined with
  ``write_disposition="merge"`` this gives idempotent upserts.
* **Incremental cursor** — each run does a cheap listing sweep (id +
  ``last_activity_date`` only, no bodies) of every question currently
  matching the configured tags; that sweep drives deletion detection, while
  questions newer than the stored cursor have their body and answers fetched
  and emitted. The cursor (``last_when``) and the id set (``known_ids``) are
  persisted in dlt's per-resource state, so re-running ``remember`` resumes
  where it left off and re-embeds only the delta.
* **Forget-on-delete** — Stack Exchange has no deletion feed, so (mirroring
  the Confluence connector) the listing sweep above doubles as the
  authoritative "currently live" set: questions absent from it (deleted,
  unlisted, or moved out of tag scope) are emitted with the ``_deleted``
  hard-delete marker. dlt removes those rows on ``merge`` and cognee's
  existing ``orphan_cleanup`` then purges them from the graph + vector +
  relational stores.
* **Content** — each question becomes one document: title, tags, the
  question body, then its accepted answer (if any) and the next
  highest-voted answers, up to ``answers_per_question``. HTML bodies are
  converted to plain text with a small, dependency-free converter (no bs4/
  markdownify required) — code blocks and paragraph breaks are preserved,
  inline formatting is flattened.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50). For real tag scopes pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against
   the *whole* synced corpus rather than a truncated window.

.. note::
   Quota is per-day and small without a key (300/day keyless, 10,000/day with
   one). Scoping to specific tags — and letting the incremental cursor skip
   unchanged questions — keeps this connector well inside that budget for a
   normal-sized tag; an extremely high-traffic tag can still exhaust it, in
   which case narrow the tag list or supply an API key.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("stack_overflow_connector")

# dlt resource / staging-table name for Stack Overflow questions.
STACK_OVERFLOW_TABLE_NAME = "stack_overflow_questions"
STACK_OVERFLOW_SOURCE_NAME = "stack_overflow"

_API_BASE = "https://api.stackexchange.com/2.3"
# Stack Exchange caps a semicolon-joined id list at 100 ids per request.
_ID_BATCH_SIZE = 100
_PAGE_SIZE = 100
_MAX_RETRIES = 5
# Warn once quota gets low enough that a normal sync run could exhaust it.
_LOW_QUOTA_THRESHOLD = 50

_EXTRA_HINT = (
    'The Stack Overflow connector requires the "stack-overflow" extra: '
    'pip install "cognee[stack-overflow]" (provides dlt and requests).'
)

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")


# ---------------------------------------------------------------------------
# HTTP / API helpers
# ---------------------------------------------------------------------------
def _make_session() -> Any:
    """Build a plain ``requests`` session for the Stack Exchange API.

    ``requests`` is imported lazily so it stays an optional dependency
    (``pip install "cognee[stack-overflow]"``). No auth headers are needed —
    the API key (if any) is sent as a query parameter per-request.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(_EXTRA_HINT) from exc

    return requests.Session()


def _request(session: Any, path: str, params: dict) -> dict:
    """GET a Stack Exchange API path, retrying transient failures.

    The API reports throttling via a JSON ``backoff`` field (seconds to wait
    before the *next* request) rather than a 429, so a successful response is
    honored by sleeping before returning. 502/503/504 and network errors are
    retried with exponential backoff; other error responses raise.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            response = session.get(f"{_API_BASE}{path}", params=params, timeout=30)
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = 2**attempt
            logger.warning(
                "Stack Overflow: network error (%s) — retrying in %ds (%d/%d).",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue

        if response.status_code == 200:
            data = response.json()
            _warn_if_low_quota(data)
            backoff = data.get("backoff")
            if backoff:
                logger.info("Stack Overflow: API requested a %ss backoff.", backoff)
                time.sleep(backoff)
            return data

        if response.status_code in (502, 503, 504) and attempt < _MAX_RETRIES - 1:
            delay = 2**attempt
            logger.warning(
                "Stack Overflow: HTTP %d — retrying in %ds (%d/%d).",
                response.status_code,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue

        try:
            error = response.json()
        except ValueError:
            error = {}
        raise RuntimeError(
            f"Stack Overflow API error (HTTP {response.status_code}): "
            f"{error.get('error_message', response.text)}"
        )

    raise RuntimeError("Stack Overflow API: exhausted retries.")  # pragma: no cover


def _warn_if_low_quota(data: dict) -> None:
    remaining = data.get("quota_remaining")
    if isinstance(remaining, int) and remaining < _LOW_QUOTA_THRESHOLD:
        logger.warning(
            "Stack Overflow: quota_remaining is low (%d). Narrow the tag list or "
            "supply an API key to raise the daily limit.",
            remaining,
        )


def _paginate(session: Any, path: str, params: dict) -> Iterator[dict]:
    """Yield ``items`` across Stack Exchange's page-number pagination."""
    page = 1
    page_params = dict(params)
    while True:
        page_params["page"] = page
        data = _request(session, path, page_params)
        yield from data.get("items", []) or []
        if not data.get("has_more"):
            return
        page += 1


def _chunks(seq: list, size: int) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _base_params(site: str, api_key: str | None) -> dict:
    params: dict[str, Any] = {"site": site, "pagesize": _PAGE_SIZE}
    if api_key:
        params["key"] = api_key
    return params


# ---------------------------------------------------------------------------
# Stack Exchange API reads
# ---------------------------------------------------------------------------
def _list_question_summaries(
    session: Any, tags: list[str], site: str, api_key: str | None
) -> Iterator[dict]:
    """Cheap listing sweep: every question currently matching ``tags``.

    No ``filter`` is passed, so the default fields (id, title, link, tags,
    last_activity_date, ...) are returned but NOT the question body — this
    keeps the sweep affordable even for tags with many questions, since it
    drives deletion detection and must enumerate every live question.
    """
    params = _base_params(site, api_key)
    params["tagged"] = ";".join(tags)
    params["sort"] = "activity"
    params["order"] = "desc"
    yield from _paginate(session, "/questions", params)


def _fetch_question_bodies(
    session: Any, question_ids: list[int], site: str, api_key: str | None
) -> dict[int, dict]:
    """Fetch full question bodies (``filter=withbody``) for the given ids."""
    questions: dict[int, dict] = {}
    for batch in _chunks(question_ids, _ID_BATCH_SIZE):
        params = _base_params(site, api_key)
        params["filter"] = "withbody"
        id_str = ";".join(str(i) for i in batch)
        for question in _paginate(session, f"/questions/{id_str}", params):
            questions[question["question_id"]] = question
    return questions


def _fetch_answers(
    session: Any, question_ids: list[int], site: str, api_key: str | None
) -> dict[int, list[dict]]:
    """Fetch every answer (``filter=withbody``) for the given question ids."""
    answers_by_question: dict[int, list[dict]] = defaultdict(list)
    for batch in _chunks(question_ids, _ID_BATCH_SIZE):
        params = _base_params(site, api_key)
        params["filter"] = "withbody"
        params["sort"] = "votes"
        params["order"] = "desc"
        id_str = ";".join(str(i) for i in batch)
        for answer in _paginate(session, f"/questions/{id_str}/answers", params):
            answers_by_question[answer["question_id"]].append(answer)
    return answers_by_question


# ---------------------------------------------------------------------------
# Rendering (dependency-free HTML → text)
# ---------------------------------------------------------------------------
def _clean_html(raw: str | None) -> str:
    """Convert a Stack Exchange HTML body to plain text.

    Dependency-free on purpose (no bs4/markdownify): paragraph and line
    breaks, and ``<pre>`` code fences, are preserved as newlines; every other
    tag is stripped and entities unescaped. Inline formatting (bold, links,
    inline ``<code>``) is flattened to plain text — a deliberate simplicity
    trade-off, matching the sibling Confluence connector's HTML handling.
    """
    if not raw:
        return ""
    text = raw
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = re.sub(r"(?i)<pre[^>]*>", "\n```\n", text)
    text = re.sub(r"(?i)</pre>", "\n```\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = _TRAILING_WS_RE.sub("\n", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


def _select_answers(answers: list[dict], accepted_id: int | None, limit: int) -> list[dict]:
    """Order answers accepted-first, then by score descending, and truncate."""
    if not answers:
        return []

    def sort_key(answer: dict) -> tuple[int, int]:
        is_accepted = accepted_id is not None and answer.get("answer_id") == accepted_id
        return (0 if is_accepted else 1, -answer.get("score", 0))

    ordered = sorted(answers, key=sort_key)
    return ordered[:limit] if limit else ordered


def _render_answer(answer: dict, accepted_id: int | None) -> str:
    is_accepted = accepted_id is not None and answer.get("answer_id") == accepted_id
    heading = "Accepted Answer" if is_accepted else f"Answer (score {answer.get('score', 0)})"
    return f"### {heading}\n\n{_clean_html(answer.get('body'))}"


def _question_to_row(question: dict, answers: list[dict]) -> dict[str, Any]:
    """Flatten a question (+ its selected answers) into a document row."""
    tags_line = ", ".join(question.get("tags", []) or [])
    body = _clean_html(question.get("body"))
    accepted_id = question.get("accepted_answer_id")

    sections = [f"Tags: {tags_line}", body] if tags_line else [body]
    if answers:
        sections.append("## Answers")
        sections.extend(_render_answer(answer, accepted_id) for answer in answers)

    return {
        "id": str(question["question_id"]),
        "title": question.get("title") or "",
        "content": "\n\n".join(section for section in sections if section),
        "url": question.get("link"),
        "_deleted": False,
    }


def _deleted_row(question_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a question by id."""
    return {"id": str(question_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_questions(
    session: Any,
    state: dict,
    *,
    tags: list[str],
    site: str,
    api_key: str | None,
    answers_per_question: int,
) -> Iterator[dict[str, Any]]:
    """Yield changed questions (with answers) since the last run, plus
    hard-delete markers.

    One listing sweep enumerates the *current* questions matching ``tags``
    (cheap, no bodies): that set drives deletion detection, while questions
    newer than the stored cursor have their body and answers fetched and
    emitted. The cursor (``last_when``) and the id set (``known_ids``) are
    advanced in ``state`` so the next run is a no-op when nothing changed.
    """
    known_ids: set[str] = set(state.get("known_ids", []))
    last_when: int = state.get("last_when", 0)
    newest_when = last_when
    current_ids: set[str] = set()
    changed_ids: list[int] = []

    for summary in _list_question_summaries(session, tags, site, api_key):
        question_id = summary["question_id"]
        str_id = str(question_id)
        current_ids.add(str_id)

        when = summary.get("last_activity_date", 0)
        # Skip only questions we have already ingested and that have not
        # changed. A question absent from known_ids is fetched regardless of
        # timestamp, so questions new to the corpus (e.g. tied at the cursor
        # boundary) are not lost.
        if str_id in known_ids and when <= last_when:
            continue
        if when > newest_when:
            newest_when = when
        changed_ids.append(question_id)

    # Deletion detection relies on the sweep enumerating every current
    # question. An empty sweep while questions were previously known almost
    # always means a transient/failed listing (network blip, a mistyped tag)
    # rather than a genuine wipe — treating it as "all deleted" would purge
    # the whole dataset and overwrite known_ids with [], making the loss
    # permanent. Skip deletion and preserve state in that case.
    if known_ids and not current_ids:
        logger.warning(
            "Stack Overflow: question sweep returned 0 questions but %d were "
            "known; skipping deletion this run to avoid a mass forget-on-delete "
            "on a transient sweep.",
            len(known_ids),
        )
        state["last_when"] = newest_when
        logger.info("Stack Overflow: 0 changed question(s), 0 deletion(s).")
        return

    changed = 0
    if changed_ids:
        bodies = _fetch_question_bodies(session, changed_ids, site, api_key)
        answers_by_question = _fetch_answers(session, changed_ids, site, api_key)
        for question_id in changed_ids:
            question = bodies.get(question_id)
            if question is None:
                # Vanished between the sweep and the detail fetch — treat as
                # deleted rather than silently dropping it from the corpus.
                current_ids.discard(str(question_id))
                continue
            answers = _select_answers(
                answers_by_question.get(question_id, []),
                question.get("accepted_answer_id"),
                answers_per_question,
            )
            changed += 1
            yield _question_to_row(question, answers)

    deleted = known_ids - current_ids
    for question_id in sorted(deleted):
        yield _deleted_row(question_id)

    state["known_ids"] = sorted(current_ids)
    state["last_when"] = newest_when
    logger.info("Stack Overflow: %d changed question(s), %d deletion(s).", changed, len(deleted))


def _tags_from_env() -> list[str]:
    raw = os.environ.get("STACK_OVERFLOW_TAGS", "")
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def stack_overflow_source(
    tags: list[str] | None = None,
    *,
    site: str = "stackoverflow",
    api_key: str | None = None,
    answers_per_question: int = 3,
    session: Any = None,
):
    """Return a ``dlt`` source that yields Stack Overflow questions as
    markdown documents for ``cognee.remember``.

    Args:
        tags: Required. Restrict ingestion to questions matching any of these
            tags. Falls back to ``STACK_OVERFLOW_TAGS`` (comma-separated).
        site: Stack Exchange site to query (default ``"stackoverflow"``).
        api_key: Stack Exchange API key. Falls back to
            ``STACK_OVERFLOW_API_KEY``; keyless access is capped at 300
            requests/day.
        answers_per_question: Max answers folded into each question's
            document (accepted first, then highest-voted).
        session: Pre-built ``requests.Session`` (mainly a test-injection
            point); when omitted a plain session is built.

    Returns:
        A dlt source suitable for ``cognee.remember(...)`` / ``cognee.add(...)``
        with ``primary_key="id"``, ``write_disposition="merge"``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_tags = tags or _tags_from_env()
    if not resolved_tags:
        raise ValueError(
            "tags is required: pass tags=[...] or set STACK_OVERFLOW_TAGS "
            "(comma-separated). Stack Overflow's daily quota is small "
            "without scoping to specific tags."
        )

    resolved_key = api_key if api_key is not None else os.environ.get("STACK_OVERFLOW_API_KEY")

    @dlt.resource(
        name=STACK_OVERFLOW_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def stack_overflow_questions():
        client = session or _make_session()
        resource_state = dlt.current.resource_state()
        yield from sync_questions(
            client,
            resource_state,
            tags=resolved_tags,
            site=site,
            api_key=resolved_key,
            answers_per_question=answers_per_question,
        )

    resource = stack_overflow_questions()
    # Opt into the document ingestion path (question -> text document ->
    # cognify). resolve_dlt_sources reads this marker; it never imports this
    # connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, STACK_OVERFLOW_SOURCE_NAME)
    return resource
