"""Stack Overflow connector for cognee — a ``dlt`` source that turns Q&A into memory.

Sync Stack Overflow questions (and their accepted/top answers) from a given set
of tags or a specific user's activity into cognee, incrementally and with
forget-on-deletion.  Built entirely on the existing DLT ingestion subsystem;
the resource produced here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_stack_overflow import stack_overflow_source

    await cognee.remember(
        stack_overflow_source(tags=["python", "dlt"]),
        dataset_name="stackoverflow_python",
        primary_key="question_id",
        write_disposition="merge",   # incremental upsert by question id
        max_rows_per_table=0,        # 0 = no row cap
    )

Design
------
* **Auth** — optional Stack Apps API key (``STACKOVERFLOW_API_KEY`` env var).
  Without a key the public API allows 300 requests/day; with a registered key
  the quota rises to 10 000.  No OAuth is required for read-only access.
* **Primary key** — the Stack Overflow ``question_id``.  Combined with
  ``write_disposition="merge"`` this gives idempotent upserts.
* **Incremental cursor** — the ``fromdate`` Unix timestamp of the most-recently
  seen question.  Each run lists questions modified since the cursor so only
  the delta is re-embedded.  The cursor is persisted in dlt's per-resource
  state, so re-running ``remember`` resumes where it left off.
* **Forget-on-delete** — the Stack Exchange API exposes a ``/questions/{ids}``
  endpoint; each sync sweeps the previously-known id set and emits hard-delete
  markers for ids that are no longer returned (deleted or migrated).  dlt
  removes those rows on ``merge``; cognee's ``orphan_cleanup`` then purges
  them from the graph, vector, and relational stores.
* **Content** — question title + body (HTML-stripped) + accepted / top-voted
  answer bodies are concatenated into a single ``content`` field so a plain
  ``remember()`` call routes all text through chunking + LLM graph extraction.
* **Filtering** — restrict by ``tags`` (AND-query), ``user_id`` (questions
  asked by a specific user), or both.  Leave both ``None`` to pull the overall
  "newest" feed (not recommended for large sites).

.. note::
   Stack Exchange API responses are gzip-compressed automatically; ``requests``
   decompresses them transparently when ``Accept-Encoding: gzip`` is sent
   (the default).

.. note::
   The Stack Exchange API paginates with ``page`` / ``pagesize``; max page size
   is 100.  ``has_more`` in the response indicates whether to fetch the next page.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger

logger = get_logger("stack_overflow_connector")

_STACK_API_BASE = "https://api.stackexchange.com/2.3"
_SITE = "stackoverflow"
_PAGE_SIZE = 100

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _make_session() -> Any:
    """Build a ``requests`` session.  Lazily imported so it's optional."""
    try:
        import requests
    except ImportError as exc:
        raise ImportError(
            'The Stack Overflow connector requires "requests". '
            'Install the extra:\n    pip install "cognee[stack-overflow]"'
        ) from exc

    session = requests.Session()
    session.headers.update({"Accept-Encoding": "gzip", "Accept": "application/json"})
    return session


def _api_get(session: Any, path: str, params: dict) -> dict:
    """GET a Stack Exchange API endpoint and return the decoded JSON."""
    url = f"{_STACK_API_BASE}{path}"
    response = session.get(url, params=params)
    response.raise_for_status()
    return response.json()


def _paginate_questions(session: Any, path: str, params: dict) -> Iterator[dict]:
    """Yield every question item across all pages for a Stack Exchange query."""
    page = 1
    while True:
        data = _api_get(session, path, {**params, "page": page, "pagesize": _PAGE_SIZE})
        yield from data.get("items", [])
        if not data.get("has_more", False):
            break
        page += 1
        # Respect backoff / throttle hints from the API.
        backoff = data.get("backoff")
        if backoff:
            logger.warning("Stack Exchange API backoff: sleeping %s seconds", backoff)
            time.sleep(int(backoff))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _strip_html(raw: str | None) -> str:
    """Strip HTML tags, unescape entities, and collapse whitespace."""
    if not raw:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw))).strip()


def _build_content(question: dict, answers: list[dict]) -> str:
    """Concatenate question title, body, and top answer(s) into a single text block."""
    parts: list[str] = []
    title = question.get("title", "")
    if title:
        parts.append(f"Q: {_strip_html(title)}")
    body = _strip_html(question.get("body", ""))
    if body:
        parts.append(body)
    for answer in answers:
        answer_body = _strip_html(answer.get("body", ""))
        if answer_body:
            label = "Accepted answer:" if answer.get("is_accepted") else "Answer:"
            parts.append(f"{label}\n{answer_body}")
    return "\n\n".join(parts)


def _question_to_row(question: dict, answers: list[dict]) -> dict[str, Any]:
    """Flatten a Stack Overflow question (+ answers) into a dlt row."""
    tags = question.get("tags") or []
    return {
        "question_id": question["question_id"],
        "title": _strip_html(question.get("title")),
        "link": question.get("link", ""),
        "tags": ",".join(tags),
        "score": question.get("score", 0),
        "answer_count": question.get("answer_count", 0),
        "is_answered": question.get("is_answered", False),
        "view_count": question.get("view_count", 0),
        "creation_date": question.get("creation_date", 0),
        "last_activity_date": question.get("last_activity_date", 0),
        "owner_display_name": (question.get("owner") or {}).get("display_name", ""),
        "content": _build_content(question, answers),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Answer fetching
# ---------------------------------------------------------------------------
def _fetch_answers(session: Any, question_id: int, api_key: str | None) -> list[dict]:
    """Return the accepted answer (if any) plus up to 2 highest-scored answers."""
    params: dict[str, Any] = {
        "site": _SITE,
        "order": "desc",
        "sort": "votes",
        "filter": "withbody",
    }
    if api_key:
        params["key"] = api_key
    try:
        data = _api_get(session, f"/questions/{question_id}/answers", params)
    except Exception:
        logger.warning("Could not fetch answers for question %s — skipping", question_id)
        return []

    items = data.get("items", [])
    accepted = [a for a in items if a.get("is_accepted")]
    top = [a for a in items if not a.get("is_accepted")][:2]
    return accepted + top


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------
def full_backfill(
    session: Any,
    state: dict,
    tags: list[str] | None,
    user_id: int | None,
    api_key: str | None,
    include_answers: bool,
) -> Iterator[dict]:
    """Yield all matching questions and record the seen-id set + cursor in ``state``."""
    params: dict[str, Any] = {
        "site": _SITE,
        "order": "desc",
        "sort": "activity",
        "filter": "withbody",
    }
    if api_key:
        params["key"] = api_key
    if tags:
        params["tagged"] = ";".join(tags)

    path = f"/users/{user_id}/questions" if user_id else "/questions"

    seen_ids: set[int] = set()
    max_ts = 0

    for question in _paginate_questions(session, path, params):
        qid = question["question_id"]
        seen_ids.add(qid)
        ts = question.get("last_activity_date", 0)
        if ts > max_ts:
            max_ts = ts
        answers = _fetch_answers(session, qid, api_key) if include_answers else []
        yield _question_to_row(question, answers)

    state["seen_ids"] = list(seen_ids)
    if max_ts:
        state["cursor"] = max_ts


def incremental_fetch(
    session: Any,
    state: dict,
    tags: list[str] | None,
    user_id: int | None,
    api_key: str | None,
    include_answers: bool,
) -> Iterator[dict]:
    """Yield questions modified since the cursor and emit delete markers for removed ids."""
    cursor: int = state.get("cursor", 0)
    prev_ids: set[int] = set(state.get("seen_ids", []))

    params: dict[str, Any] = {
        "site": _SITE,
        "order": "desc",
        "sort": "activity",
        "filter": "withbody",
        "fromdate": cursor,
    }
    if api_key:
        params["key"] = api_key
    if tags:
        params["tagged"] = ";".join(tags)

    path = f"/users/{user_id}/questions" if user_id else "/questions"

    seen_ids: set[int] = set(prev_ids)
    max_ts = cursor

    for question in _paginate_questions(session, path, params):
        qid = question["question_id"]
        seen_ids.add(qid)
        ts = question.get("last_activity_date", 0)
        if ts > max_ts:
            max_ts = ts
        answers = _fetch_answers(session, qid, api_key) if include_answers else []
        yield _question_to_row(question, answers)

    # Sweep for deletions: check ids seen before that no longer appear in the API.
    deleted_ids = _find_deleted_ids(session, prev_ids, api_key)
    for qid in deleted_ids:
        seen_ids.discard(qid)
        yield {"question_id": qid, "_deleted": True}

    state["seen_ids"] = list(seen_ids)
    if max_ts > cursor:
        state["cursor"] = max_ts


def _find_deleted_ids(
    session: Any, candidate_ids: set[int], api_key: str | None
) -> list[int]:
    """Return ids from ``candidate_ids`` that the API no longer returns (deleted/migrated)."""
    if not candidate_ids:
        return []

    deleted: list[int] = []
    ids_list = list(candidate_ids)
    # The /questions/{ids} endpoint accepts up to 100 semicolon-joined ids.
    for i in range(0, len(ids_list), 100):
        chunk = ids_list[i : i + 100]
        ids_str = ";".join(str(x) for x in chunk)
        params: dict[str, Any] = {"site": _SITE, "filter": "total"}
        if api_key:
            params["key"] = api_key
        try:
            data = _api_get(session, f"/questions/{ids_str}", params)
        except Exception:
            logger.warning("Could not sweep ids %s for deletions — skipping", ids_str)
            continue
        returned_ids = {item["question_id"] for item in data.get("items", [])}
        deleted.extend(qid for qid in chunk if qid not in returned_ids)

    return deleted


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def stack_overflow_source(
    tags: list[str] | None = None,
    user_id: int | None = None,
    api_key: str | None = None,
    include_answers: bool = True,
) -> Any:
    """Return a dlt resource that syncs Stack Overflow questions into cognee.

    Parameters
    ----------
    tags:
        List of Stack Overflow tags to filter by (AND logic).  E.g.
        ``["python", "pandas"]``.  Leave ``None`` to fetch all questions
        (or all questions for ``user_id`` if provided).
    user_id:
        Numeric Stack Overflow user id.  When set, fetches questions posted
        by that user (optionally filtered by ``tags``).
    api_key:
        Stack Apps API key.  Falls back to the ``STACKOVERFLOW_API_KEY``
        environment variable.  Optional but raises the daily quota from 300
        to 10 000 requests.
    include_answers:
        When ``True`` (default), the accepted answer and up to two top-voted
        answers are fetched and concatenated into the ``content`` field.
        Set to ``False`` to ingest question-only for faster syncs.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Stack Overflow connector requires "dlt". '
            'Install it with: pip install "dlt[sqlalchemy]"'
        ) from exc

    resolved_key = api_key or os.environ.get("STACKOVERFLOW_API_KEY")

    @dlt.resource(
        name="stack_overflow_questions",
        primary_key="question_id",
        write_disposition="merge",
        columns={
            "_deleted": {
                "data_type": "bool",
                "hard_delete": True,
            }
        },
    )
    def _resource() -> Iterator[dict]:
        session = _make_session()
        state = dlt.current.resource_state()

        if state.get("cursor"):
            yield from incremental_fetch(
                session, state, tags, user_id, resolved_key, include_answers
            )
        else:
            yield from full_backfill(
                session, state, tags, user_id, resolved_key, include_answers
            )

    return _resource()
