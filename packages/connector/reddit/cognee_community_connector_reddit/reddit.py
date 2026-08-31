"""Reddit connector for cognee — a ``dlt`` source over the OAuth listing API.

Sync subreddit submissions and their comment trees into cognee, incrementally
via the listing ``before``/``after`` cursor.  Built on the existing DLT
ingestion subsystem like the sibling Notion / Google Drive / Telegram
connectors; the resource produced here is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_reddit import reddit_source

    await cognee.remember(
        reddit_source(subreddits=["r/LocalLLaMA", "cognee"]),
        dataset_name="reddit",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by submission fullname
        max_rows_per_table=0,        # 0 = no row cap (busy subreddits exceed the default 50)
    )

Design
------
* **Auth** — OAuth 2.0 *script app* (``client_id`` + ``client_secret`` +
  ``username`` + ``password``, Basic-auth'd against
  ``/api/v1/access_token``), exactly as the issue asks; a pre-obtained
  ``refresh_token`` is accepted as an alternative grant.  A descriptive
  ``User-Agent`` is mandatory — Reddit rate-limits generic ones hard.  All
  data calls go to ``https://oauth.reddit.com``.  The API is spoken over the
  standard library (``urllib``) — no ``praw``/``asyncpraw`` dependency.
* **Primary key** — the submission *fullname* (``t3_<id>``), which is globally
  unique and is also what the listing cursor and ``/api/info`` speak.
* **Ingest** — one document per submission: title + selftext (or the outbound
  link) plus the rendered comment tree, indented by reply depth.  Subreddit,
  author, timestamp, score, comment count, flair and the truncation flag
  become record metadata (a ``metadata`` JSON column, also folded into the
  text as a ``Submission context`` section so it survives entity extraction —
  which is what turns author→submission and commenter→thread into graph
  edges).
* **Incremental cursor** — the listing ``before``/``after`` cursor named in
  the issue, kept in dlt's per-resource state
  (``state["subreddits"][name]["newest"]``): the first run backfills
  ``/r/<sub>/new`` paging with ``after``, and every later run pages
  ``/r/<sub>/new?before=<newest fullname seen>`` so only submissions posted
  since the last run are fetched.  Alongside it, a per-subreddit map of
  submission fullname → content fingerprint powers edit dedup and
  forget-on-delete.  Because a listing cursor answers "what is new" and never
  "what changed", the ``refresh_limit`` (default 25) most recently ingested
  submissions per subreddit are also re-rendered each run — that is how an
  edited body, a new reply, or a comment deleted inside a live thread is
  noticed.  Their ``t3`` payloads come from the deletion re-check below, so
  the refresh costs one comment-tree call each and nothing more.
* **Comment trees (the issue's explicit trap)** — ``/comments/<id>`` is
  requested with ``depth``/``limit``, and the ``more`` placeholders it leaves
  behind are expanded through ``/api/morechildren`` under a **hard budget**:
  ``comment_depth`` (default 10) bounds nesting and ``max_more_requests``
  (default 10, ``0`` disables expansion) bounds the number of expansion calls
  per submission.  When the budget bites, the document says so in its text and
  the connector logs it — a truncated thread is never silently truncated, and
  one popular thread can never cost thousands of calls.
* **Re-emission gate** — a sha256 fingerprint of the *semantic* material
  (title, selftext, subreddit, author, and the comment tree's authors/bodies/
  shape) is kept per submission.  An unchanged submission is not re-yielded,
  so nothing is re-cognified for free.  Vote score and comment counts are
  deliberately **excluded** from the fingerprint: they churn constantly and
  hashing them would re-ingest the whole corpus on every run.
* **Forget-on-delete** — Reddit pushes no deletion feed, so the connector
  re-checks the submissions it already knows about through
  ``/api/info?id=t3_a,t3_b,...`` (100 ids per call) on every run.  A
  submission that has vanished from ``/api/info``, or comes back with
  ``removed_by_category`` set, or whose author *and* body are both tombstoned
  (``[deleted]``/``[removed]``), is emitted as a ``{"id": ..., "_deleted":
  True}`` hard-delete row; dlt drops it on ``merge`` and cognee's existing
  ``orphan_cleanup`` purges it from the graph, vector and relational stores.
  Because a submission and its comments are one document, forgetting the
  submission forgets its comment tree with it.
* **Honest boundary** — a *comment* deleted inside a still-live submission is
  not a signal Reddit ever pushes.  It is noticed when that submission is next
  re-rendered (the refresh window above, or a later listing sighting): the
  tree no longer contains the comment, the fingerprint differs, and the whole
  document is re-emitted without it.  For a submission that has aged out of
  the refresh window, that means "not until it changes again", not "instantly"
  — the README states this rather than pretending the granularity is finer
  than it is.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("reddit_connector")

# dlt resource / staging-table name for Reddit submissions.
REDDIT_TABLE_NAME = "reddit_submissions"
REDDIT_SOURCE_NAME = "reddit"

# OAuth token endpoint (www) and the data host (oauth) — Reddit separates them.
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_ROOT = "https://oauth.reddit.com"
_WEB_ROOT = "https://www.reddit.com"

# Reddit's listing page size maximum, and the /api/info id-batch maximum.
_LISTING_LIMIT = 100
_INFO_BATCH = 100
# /api/morechildren accepts at most 100 child ids per call.
_MORE_CHILDREN_BATCH = 100

# Retry budget for rate-limited / transient Reddit responses, and the HTTP
# timeout applied to every call.
_MAX_RETRIES = 5
_TIMEOUT = 60
# Upper bound on a single courtesy sleep derived from the rate-limit headers.
_MAX_SLEEP = 60.0

# Bodies/authors Reddit substitutes for removed content.
_TOMBSTONES = frozenset({"[deleted]", "[removed]"})

_TRUNCATION_NOTICE = (
    "_Comment tree truncated by the connector's expansion budget "
    "(comment_depth / max_more_requests); some replies are not included._"
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _RedditConfig:
    """Normalized connector configuration (see :func:`normalize_config`)."""

    subreddits: tuple[str, ...] = ()
    comment_depth: int = 10
    comment_limit: int = 200
    max_more_requests: int = 10
    backfill_limit: int = 200
    refresh_limit: int = 25
    max_listing_pages: int = 10


def normalize_subreddit(name: Any) -> str | None:
    """Normalize one subreddit reference to its bare lowercase name.

    Accepts ``"python"``, ``"r/Python"``, ``"/r/python/"`` and full URLs; the
    result is what Reddit's ``/r/<name>/new`` path wants.  Returns ``None``
    for anything that does not carry a name.
    """
    text = str(name or "").strip()
    if not text:
        return None
    text = text.split("?", 1)[0].split("#", 1)[0]
    if "://" in text:
        text = text.split("://", 1)[1].split("/", 1)[-1]
    parts = [part for part in text.split("/") if part]
    if parts and parts[0].lower() in ("www.reddit.com", "reddit.com", "old.reddit.com"):
        parts = parts[1:]
    if parts and parts[0].lower() == "r":
        parts = parts[1:]
    return parts[0].lower() if parts else None


def normalize_config(
    subreddits: Iterable[str] | None = None,
    *,
    comment_depth: int = 10,
    comment_limit: int = 200,
    max_more_requests: int = 10,
    backfill_limit: int = 200,
    refresh_limit: int = 25,
    max_listing_pages: int = 10,
) -> _RedditConfig:
    """Validate and normalize the knobs the source was built with.

    Subreddit names are de-duplicated while preserving order; the budgets are
    clamped so a caller cannot ask for a negative depth or an unbounded
    ``more``-expansion (``max_more_requests=0`` legitimately disables
    expansion, which is why its floor is 0 and not 1).
    """
    names: list[str] = []
    for entry in subreddits or ():
        name = normalize_subreddit(entry)
        if name and name not in names:
            names.append(name)
    return _RedditConfig(
        subreddits=tuple(names),
        comment_depth=max(1, int(comment_depth)),
        comment_limit=max(1, int(comment_limit)),
        max_more_requests=max(0, int(max_more_requests)),
        backfill_limit=max(1, int(backfill_limit)),
        refresh_limit=max(0, int(refresh_limit)),
        max_listing_pages=max(1, int(max_listing_pages)),
    )


# ---------------------------------------------------------------------------
# Comment tree: parsing, budgeted `more` expansion, rendering
# ---------------------------------------------------------------------------
def _comment_node(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one ``t1`` payload into the connector's internal node shape."""
    return {
        "id": str(data.get("id") or ""),
        "author": str(data.get("author") or ""),
        "body": str(data.get("body") or ""),
        "score": data.get("score"),
        "created_utc": data.get("created_utc"),
        "replies": [],
    }


def _parse_children(
    children: Sequence[Mapping[str, Any]] | None,
    depth: int,
    max_depth: int,
    index: dict[str, tuple[dict[str, Any], int]],
    more_slots: list[tuple[list[dict[str, Any]], dict[str, Any], int]],
) -> tuple[list[dict[str, Any]], bool]:
    """Turn a raw listing ``children`` array into nodes, collecting ``more`` slots.

    ``index`` maps comment id → ``(node, depth)`` so flat ``/api/morechildren``
    results can be grafted back under their real parent.  ``more_slots``
    accumulates ``(sibling_list, more_payload, depth)`` triples for the
    budgeted expansion pass.  Returns the nodes plus whether anything was
    dropped for exceeding ``max_depth``.
    """
    if depth > max_depth:
        return [], bool(children)

    nodes: list[dict[str, Any]] = []
    truncated = False
    for child in children or ():
        kind = child.get("kind")
        data = child.get("data") or {}
        if kind == "more":
            if data.get("children"):
                more_slots.append((nodes, dict(data), depth))
            else:
                # A bare "continue this thread" placeholder carries no ids; it
                # is only reachable by re-fetching the subtree deeper down.
                truncated = True
            continue
        if kind != "t1":
            continue
        node = _comment_node(data)
        if node["id"]:
            index[node["id"]] = (node, depth)
        nodes.append(node)
        replies = data.get("replies")
        if isinstance(replies, Mapping):
            child_nodes, child_truncated = _parse_children(
                (replies.get("data") or {}).get("children"),
                depth + 1,
                max_depth,
                index,
                more_slots,
            )
            node["replies"] = child_nodes
            truncated = truncated or child_truncated
    return nodes, truncated


def _graft_more(
    things: Sequence[Mapping[str, Any]],
    fallback: list[dict[str, Any]],
    fallback_depth: int,
    max_depth: int,
    index: dict[str, tuple[dict[str, Any], int]],
    more_slots: list[tuple[list[dict[str, Any]], dict[str, Any], int]],
) -> bool:
    """Attach a flat ``/api/morechildren`` result back onto the tree.

    ``/api/morechildren`` answers with a flat list of things carrying a
    ``parent_id``; each one is appended under the parent already in ``index``,
    or to ``fallback`` when the parent is the submission itself.  Returns True
    if anything had to be dropped (too deep, or an unresolvable parent).
    """
    truncated = False
    for thing in things or ():
        kind = thing.get("kind")
        data = thing.get("data") or {}
        parent_id = str(data.get("parent_id") or "")
        parent = index.get(parent_id[3:]) if parent_id.startswith("t1_") else None
        if parent is not None:
            target, depth = parent[0]["replies"], parent[1] + 1
        else:
            target, depth = fallback, fallback_depth
        if depth > max_depth:
            truncated = True
            continue
        if kind == "more":
            if data.get("children"):
                more_slots.append((target, dict(data), depth))
            else:
                truncated = True
            continue
        if kind != "t1":
            continue
        node = _comment_node(data)
        if node["id"]:
            index[node["id"]] = (node, depth)
        target.append(node)
    return truncated


def collect_comments(
    fetch_more: Callable[[Sequence[str]], Sequence[Mapping[str, Any]]],
    children: Sequence[Mapping[str, Any]] | None,
    *,
    max_depth: int,
    max_more_requests: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Build a submission's comment tree under a hard expansion budget.

    ``fetch_more(child_ids) -> things`` is the only outside call (the client's
    ``/api/morechildren`` wrapper, or a fake in tests), which keeps this the
    unit-testable heart of the issue's "one thread can be thousands of calls"
    warning.  At most ``max_more_requests`` expansion calls are made and
    nothing deeper than ``max_depth`` is kept.

    Returns ``(nodes, truncated, requests_used)``; ``truncated`` is True as
    soon as *anything* was left out, and the caller surfaces that both in the
    document text and in the log.
    """
    index: dict[str, tuple[dict[str, Any], int]] = {}
    more_slots: list[tuple[list[dict[str, Any]], dict[str, Any], int]] = []
    nodes, truncated = _parse_children(children, 1, max_depth, index, more_slots)

    used = 0
    while more_slots:
        if used >= max_more_requests:
            truncated = True
            break
        target, more, depth = more_slots.pop(0)
        child_ids = [str(child) for child in (more.get("children") or [])]
        if len(child_ids) > _MORE_CHILDREN_BATCH:
            # Reddit caps a single morechildren call; queue the remainder so
            # it is either expanded later or honestly reported as truncated.
            rest = {**more, "children": child_ids[_MORE_CHILDREN_BATCH:]}
            more_slots.append((target, rest, depth))
            child_ids = child_ids[:_MORE_CHILDREN_BATCH]
        used += 1
        things = fetch_more(child_ids) or []
        truncated = _graft_more(things, target, depth, max_depth, index, more_slots) or truncated
    if more_slots:
        truncated = True
    return nodes, truncated, used


def render_comment_lines(nodes: Sequence[Mapping[str, Any]], depth: int = 0) -> list[str]:
    """Render a comment tree as indented markdown bullets (2 spaces per level)."""
    lines: list[str] = []
    for node in nodes or ():
        pad = "  " * depth
        author = f"u/{node['author']}" if node.get("author") else "u/[unknown]"
        score = node.get("score")
        points = f" ({score} points)" if isinstance(score, int) else ""
        body = str(node.get("body") or "").strip() or "[no text]"
        body_lines = body.splitlines()
        lines.append(f"{pad}- **{author}**{points}: {body_lines[0]}")
        lines.extend(f"{pad}  {extra}" for extra in body_lines[1:])
        lines.extend(render_comment_lines(node.get("replies") or (), depth + 1))
    return lines


def _comment_material(nodes: Sequence[Mapping[str, Any]], depth: int = 0) -> Iterator[str]:
    """Fingerprint material for a comment tree: author, body and shape only."""
    for node in nodes or ():
        yield f"{depth}\x01{node.get('author') or ''}\x01{node.get('body') or ''}"
        yield from _comment_material(node.get("replies") or (), depth + 1)


# ---------------------------------------------------------------------------
# Row rendering
# ---------------------------------------------------------------------------
def _iso(timestamp: Any) -> str | None:
    """Render a Reddit unix timestamp as an ISO-8601 UTC string."""
    if not isinstance(timestamp, (int, float)):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _fullname(submission: Mapping[str, Any]) -> str | None:
    """The submission's globally unique ``t3_<id>`` fullname, or None."""
    name = str(submission.get("name") or "").strip()
    if name.startswith("t3_"):
        return name
    ident = str(submission.get("id") or "").strip()
    return f"t3_{ident}" if ident else None


def render_submission(
    submission: Mapping[str, Any],
    comments: Sequence[Mapping[str, Any]] | None = None,
    *,
    truncated: bool = False,
) -> tuple[dict[str, Any], str] | None:
    """Flatten one submission + its comment tree into a document row.

    The row contract is ``{id, title, content, url}`` (what
    ``resolve_dlt_sources`` expects of a document source) plus a structured
    ``metadata`` JSON column.  Subreddit / author / date / score context is
    also rendered into the content as a ``Submission context`` section so
    entity extraction keeps those relationships as graph edges.

    Returns ``(row, fingerprint)``, or ``None`` for a payload with no id.  The
    fingerprint covers only the semantic material — score and comment counts
    are excluded on purpose, because they change on nearly every run and would
    otherwise re-cognify the whole corpus for nothing.
    """
    fullname = _fullname(submission)
    if not fullname:
        return None

    subreddit = normalize_subreddit(submission.get("subreddit")) or ""
    author = str(submission.get("author") or "")
    title = str(submission.get("title") or "").strip() or "(untitled submission)"
    selftext = str(submission.get("selftext") or "").strip()
    is_self = bool(submission.get("is_self", bool(selftext)))
    outbound = str(submission.get("url") or "").strip()
    permalink = str(submission.get("permalink") or "").strip()
    url = (
        f"{_WEB_ROOT}{permalink}"
        if permalink.startswith("/")
        else permalink or (f"{_WEB_ROOT}/comments/{fullname[3:]}")
    )
    flair = submission.get("link_flair_text")

    context = [f"- subreddit: r/{subreddit}" if subreddit else "- subreddit: (unknown)"]
    context.append(f"- author: u/{author}" if author else "- author: u/[unknown]")
    if (posted := _iso(submission.get("created_utc"))) is not None:
        context.append(f"- posted: {posted}")
    score, num_comments = submission.get("score"), submission.get("num_comments")
    if isinstance(score, int):
        context.append(f"- score: {score} points")
    if isinstance(num_comments, int):
        context.append(f"- comments upstream: {num_comments}")
    if flair:
        context.append(f"- flair: {flair}")
    if not is_self and outbound:
        context.append(f"- links out to: {outbound}")

    body = selftext or (f"Link post pointing at {outbound}" if outbound else "(no body text)")
    comment_lines = render_comment_lines(comments or ())
    sections = [f"# {title}", "", body, "", "Submission context:", *context]
    if comment_lines:
        sections += ["", "Comments:", *comment_lines]
    else:
        sections += ["", "Comments: (none ingested)"]
    if truncated:
        sections += ["", _TRUNCATION_NOTICE]

    metadata = {
        "subreddit": subreddit or None,
        "author": author or None,
        "created_utc": _iso(submission.get("created_utc")),
        "score": score if isinstance(score, int) else None,
        "num_comments": num_comments if isinstance(num_comments, int) else None,
        "flair": flair or None,
        "is_self": is_self,
        "permalink": url,
        "comments_truncated": bool(truncated),
    }

    row = {
        "id": fullname,
        "title": title if len(title) <= 200 else title[:197] + "...",
        "content": "\n".join(sections),
        "url": url,
        "metadata": json.dumps(metadata, sort_keys=True, default=str),
        "_deleted": False,
    }

    material = "\x00".join(
        [
            fullname,
            subreddit,
            author,
            title,
            body,
            url,
            str(bool(truncated)),
            *_comment_material(comments or ()),
        ]
    )
    return row, hashlib.sha256(material.encode("utf-8")).hexdigest()


def is_gone(info: Mapping[str, Any] | None) -> bool:
    """True when an ``/api/info`` answer means "this submission is gone".

    Three readings count as gone: the id no longer comes back from
    ``/api/info`` at all (``None``), Reddit flags it with a
    ``removed_by_category`` (moderator/admin/spam removal), or both its author
    and its body have been replaced by ``[deleted]``/``[removed]`` tombstones
    (a self-delete by the author).
    """
    if not info:
        return True
    if info.get("removed_by_category"):
        return True
    author = str(info.get("author") or "").strip().lower()
    body = str(info.get("selftext") or "").strip().lower()
    return author in _TOMBSTONES and body in _TOMBSTONES


# ---------------------------------------------------------------------------
# Sync (pure given config + state + payloads — unit-testable, no network)
# ---------------------------------------------------------------------------
def _empty_subreddit_state() -> dict[str, Any]:
    """Fresh per-subreddit state: listing cursor + fingerprint map."""
    return {"newest": None, "newest_created": None, "submissions": {}}


def sync_submissions(
    config: _RedditConfig,
    state: dict,
    listings: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    info: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed submission rows and tombstones for one sync round.

    Pure over ``(config, state, listings, info)``: no network, no client — the
    caller supplies already-fetched payloads.

    Args:
        config: normalized knobs (only used for logging context here).
        state: dlt per-resource state. Carries
            ``state["subreddits"][name] = {"newest": "t3_...", "submissions":
            {fullname: fingerprint}}`` — ``newest`` is the listing cursor the
            next run passes as ``before=``.
        listings: subreddit → bundles, each ``{"submission": <t3 data>,
            "comments": <nodes>, "truncated": <bool>}``, in any order.
        info: fullname → its ``/api/info`` payload for every previously known
            submission that was re-checked, with ``None`` for ids that no
            longer come back. Ids absent from this mapping are simply not
            re-checked this run.

    Yields:
        Document rows for new/changed submissions, then ``{"id", "_deleted":
        True}`` tombstones for submissions that disappeared upstream.
    """
    subreddits: dict[str, dict[str, Any]] = state.setdefault("subreddits", {})
    changed = deleted = 0

    for subreddit, bundles in (listings or {}).items():
        sub_state = subreddits.setdefault(subreddit, _empty_subreddit_state())
        seen: dict[str, str] = sub_state.setdefault("submissions", {})
        newest_name = sub_state.get("newest")
        stored = sub_state.get("newest_created")
        newest_created = float(stored) if isinstance(stored, (int, float)) else float("-inf")

        for bundle in bundles or ():
            submission = bundle.get("submission") or {}
            rendered = render_submission(
                submission,
                bundle.get("comments") or (),
                truncated=bool(bundle.get("truncated")),
            )
            if rendered is None:
                continue
            row, fingerprint = rendered
            created = submission.get("created_utc")
            created = float(created) if isinstance(created, (int, float)) else 0.0
            if created > newest_created:
                newest_created, newest_name = created, row["id"]
            if seen.get(row["id"]) == fingerprint:
                continue  # nothing changed since the last run — do not re-cognify
            seen[row["id"]] = fingerprint
            changed += 1
            yield row

        if newest_name:
            # The cursor only ever moves forward: the newest submission ever
            # seen is the next run's `before=` anchor. Refreshed older
            # submissions (see `_refresh_bundles`) must never drag it back,
            # which is why the anchor's own timestamp is kept beside it.
            sub_state["newest"] = newest_name
            if newest_created > float("-inf"):
                sub_state["newest_created"] = newest_created

    for fullname, payload in (info or {}).items():
        if not is_gone(payload):
            continue
        known = False
        for sub_state in subreddits.values():
            if fullname in (sub_state.get("submissions") or {}):
                del sub_state["submissions"][fullname]
                known = True
        if not known:
            continue  # never ingested it; nothing to forget
        deleted += 1
        # One row per submission means the tombstone forgets the whole
        # document, comment tree included.
        yield {"id": fullname, "_deleted": True}

    logger.info(
        "Reddit: %d changed submission(s), %d deletion(s) across %d subreddit(s).",
        changed,
        deleted,
        len(listings or {}),
    )


# ---------------------------------------------------------------------------
# Reddit OAuth client (read-only by construction)
# ---------------------------------------------------------------------------
class _RedditClient:
    """Minimal read-only Reddit OAuth client over urllib (no extra dependency).

    Handles the script-app token grant (or a ``refresh_token`` grant),
    transparent re-authentication when the token expires mid-run, the
    ``X-Ratelimit-*`` courtesy sleep, and retries with backoff for
    429/5xx/timeouts.  ``sleep`` is injectable so tests never actually wait.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        user_agent: str,
        username: str | None = None,
        password: str | None = None,
        refresh_token: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._user_agent = user_agent
        self._username = username
        self._password = password
        self._refresh_token = refresh_token
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires = 0.0

    # -- auth ---------------------------------------------------------------
    def _authenticate(self) -> None:
        """Exchange the configured grant for a bearer token."""
        if self._refresh_token:
            form = {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        else:
            form = {
                "grant_type": "password",
                "username": self._username or "",
                "password": self._password or "",
            }
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode(
            "ascii"
        )
        request = urllib.request.Request(
            _TOKEN_URL,
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers={
                "Authorization": f"Basic {basic}",
                "User-Agent": self._user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            # Never echo the request body back: it holds the password.
            raise ValueError(
                f"Reddit rejected the OAuth credentials (HTTP {exc.code}). Check the "
                "script app's client id/secret and that the account owns the app."
            ) from exc
        token = payload.get("access_token")
        if not token:
            raise ValueError(f"Reddit returned no access_token: {payload.get('error', payload)}")
        self._token = str(token)
        expires_in = payload.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0
        # Renew a minute early so a long run never trips over the boundary.
        self._token_expires = time.monotonic() + max(60.0, lifetime - 60.0)

    def _ensure_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expires:
            self._authenticate()
        return self._token or ""

    # -- transport ----------------------------------------------------------
    def _respect_rate_limit(self, headers: Mapping[str, str]) -> None:
        """Sleep out the window when Reddit says we are nearly out of calls."""
        try:
            remaining = float(headers.get("X-Ratelimit-Remaining") or "999")
            reset = float(headers.get("X-Ratelimit-Reset") or "0")
        except (TypeError, ValueError):
            return
        if remaining <= 1.0 and reset > 0:
            delay = min(reset, _MAX_SLEEP)
            logger.warning("Reddit: rate-limit window exhausted — sleeping %.1fs.", delay)
            self._sleep(delay)

    def _request(
        self, path: str, params: Mapping[str, Any] | None = None, *, method: str = "GET"
    ) -> Any:
        """One authenticated API call, retrying transient failures.

        401 triggers exactly one silent re-authentication (the token expired);
        403/404 propagate — they are permanent answers about access or a
        missing thing, and callers that can read a 404 as "gone" do so
        themselves.
        """
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        for attempt in range(_MAX_RETRIES):
            token = self._ensure_token()
            url = f"{_API_ROOT}{path}"
            data = None
            if method == "POST":
                data = query.encode("utf-8")
            elif query:
                url = f"{url}?{query}"
            request = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self._user_agent,
                    **({"Content-Type": "application/x-www-form-urlencoded"} if data else {}),
                },
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                    payload = json.load(response)
                    self._respect_rate_limit(response.headers)
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self._token = None  # expired mid-run — re-auth and retry once
                    continue
                if exc.code not in (429,) and exc.code < 500:
                    raise
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = self._retry_delay(exc, attempt)
                logger.warning(
                    "Reddit: HTTP %s on %s — retrying in %.1fs (%d/%d).",
                    exc.code,
                    path,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                self._sleep(delay)
            except OSError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                self._sleep(float(2**attempt))
        return None  # pragma: no cover — the loop always returns or raises

    @staticmethod
    def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
        """Prefer Reddit's own Retry-After, fall back to exponential backoff."""
        try:
            retry_after = float(exc.headers.get("Retry-After") or 0)
        except (AttributeError, TypeError, ValueError):
            retry_after = 0.0
        return min(max(retry_after, float(2**attempt)), _MAX_SLEEP)

    # -- API surface --------------------------------------------------------
    def subscribed_subreddits(self) -> list[str]:
        """Names of the authenticated user's subscribed subreddits."""
        names: list[str] = []
        after: str | None = None
        for _ in range(_MAX_RETRIES):
            payload = self._request(
                "/subreddits/mine/subscriber", {"limit": _LISTING_LIMIT, "after": after}
            )
            data = (payload or {}).get("data") or {}
            for child in data.get("children") or ():
                name = normalize_subreddit((child.get("data") or {}).get("display_name"))
                if name and name not in names:
                    names.append(name)
            after = data.get("after")
            if not after:
                break
        return names

    def listing(
        self,
        subreddit: str,
        *,
        before: str | None = None,
        after: str | None = None,
        limit: int = _LISTING_LIMIT,
    ) -> dict[str, Any]:
        """One page of ``/r/<subreddit>/new`` — returns the listing ``data``."""
        payload = self._request(
            f"/r/{subreddit}/new",
            {"limit": limit, "before": before, "after": after, "raw_json": 1},
        )
        return (payload or {}).get("data") or {}

    def comments(self, submission_id: str, *, depth: int, limit: int) -> list[Any]:
        """The two-listing ``/comments/<id>`` payload for one submission.

        A 403/404 here is the correct reading of "this thread is not readable
        any more" (private subreddit, deleted thread): the submission is still
        yielded, with no comment tree, and the deletion pass decides its fate.
        """
        try:
            payload = self._request(
                f"/comments/{submission_id}",
                {"depth": depth, "limit": limit, "raw_json": 1, "sort": "top"},
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                logger.warning(
                    "Reddit: comments for %s unreadable (HTTP %s).", submission_id, exc.code
                )
                return []
            raise
        return payload if isinstance(payload, list) else []

    def more_children(
        self, link_fullname: str, children: Sequence[str], *, depth: int
    ) -> list[Any]:
        """Expand one ``more`` node — the flat ``things`` list, or empty."""
        if not children:
            return []
        payload = self._request(
            "/api/morechildren",
            {
                "api_type": "json",
                "link_id": link_fullname,
                "children": ",".join(children),
                "depth": depth,
                "raw_json": 1,
            },
            method="POST",
        )
        return ((payload or {}).get("json") or {}).get("data", {}).get("things") or []

    def info(self, fullnames: Sequence[str]) -> list[dict[str, Any]]:
        """``/api/info`` for up to 100 fullnames — the payloads that still exist."""
        if not fullnames:
            return []
        payload = self._request("/api/info", {"id": ",".join(fullnames), "raw_json": 1})
        children = ((payload or {}).get("data") or {}).get("children") or []
        return [child.get("data") or {} for child in children]


# ---------------------------------------------------------------------------
# Fetch orchestration (client + config + state → payloads for sync_submissions)
# ---------------------------------------------------------------------------
def _collect_listing(client: Any, subreddit: str, anchor: str | None, config: _RedditConfig):
    """Page ``/r/<sub>/new`` — forward from ``anchor`` or backfill from the top.

    With a cursor, ``before=`` walks toward the newest submissions and stops as
    soon as a short page comes back (nothing newer left).  Without one, the
    first run backfills with ``after=`` up to ``backfill_limit`` submissions.
    """
    items: list[dict[str, Any]] = []
    if anchor:
        cursor: str | None = anchor
        for _ in range(config.max_listing_pages):
            data = client.listing(subreddit, before=cursor, limit=_LISTING_LIMIT)
            children = _listing_items(data)
            if not children:
                break
            items.extend(children)
            cursor = str(children[0].get("name") or "") or None
            if len(children) < _LISTING_LIMIT or not cursor:
                break
        return items

    after: str | None = None
    for _ in range(config.max_listing_pages):
        data = client.listing(subreddit, after=after, limit=_LISTING_LIMIT)
        children = _listing_items(data)
        if not children:
            break
        items.extend(children)
        after = data.get("after")
        if not after or len(items) >= config.backfill_limit:
            break
    return items[: config.backfill_limit]


def _listing_items(data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The ``t3`` payloads of a listing page, in the order Reddit returned them."""
    return [
        child.get("data") or {}
        for child in ((data or {}).get("children") or ())
        if child.get("kind") == "t3"
    ]


def _bundle(client: Any, config: _RedditConfig, submission: Mapping[str, Any]) -> dict[str, Any]:
    """Fetch one submission's comment tree under budget and package it up."""
    fullname = _fullname(submission) or ""
    raw = client.comments(
        str(submission.get("id") or ""), depth=config.comment_depth, limit=config.comment_limit
    )
    children = None
    if isinstance(raw, Sequence) and len(raw) > 1 and isinstance(raw[1], Mapping):
        children = (raw[1].get("data") or {}).get("children")

    def fetch_more(child_ids: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        return client.more_children(fullname, child_ids, depth=config.comment_depth)

    nodes, truncated, used = collect_comments(
        fetch_more,
        children,
        max_depth=config.comment_depth,
        max_more_requests=config.max_more_requests,
    )
    if truncated:
        logger.warning(
            "Reddit: comment tree of %s truncated after %d/%d morechildren call(s) "
            "(comment_depth=%d) — the document says so too.",
            fullname,
            used,
            config.max_more_requests,
            config.comment_depth,
        )
    return {"submission": dict(submission), "comments": nodes, "truncated": truncated}


def _recheck_known(client: Any, state: Mapping[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Re-check every known submission through ``/api/info`` (100 ids per call)."""
    known = sorted(
        {
            fullname
            for sub_state in (state.get("subreddits") or {}).values()
            for fullname in (sub_state.get("submissions") or {})
        }
    )
    info: dict[str, dict[str, Any] | None] = {}
    for start in range(0, len(known), _INFO_BATCH):
        batch = known[start : start + _INFO_BATCH]
        alive = {
            str(payload.get("name") or ""): payload
            for payload in (client.info(batch) or ())
            if payload.get("name")
        }
        for fullname in batch:
            info[fullname] = alive.get(fullname)
    return info


def _refresh_bundles(
    client: Any,
    config: _RedditConfig,
    subreddit: str,
    state: Mapping[str, Any],
    info: Mapping[str, Mapping[str, Any] | None],
    already: set[str],
) -> list[dict[str, Any]]:
    """Re-render the most recently ingested submissions of one subreddit.

    The ``before`` cursor answers "what is new", never "what changed" — so a
    body edit, a new reply, or a comment deleted inside a still-live thread
    would otherwise be invisible.  The ``/api/info`` re-check already returns
    the current ``t3`` payload of every known submission for free; this pass
    pairs the newest ``refresh_limit`` of them with a fresh comment tree, and
    the fingerprint gate decides whether anything is actually re-emitted.
    Submissions that ``/api/info`` reports as gone are skipped — the deletion
    pass tombstones them instead.
    """
    sub_state = (state.get("subreddits") or {}).get(subreddit) or {}
    known = [name for name in (sub_state.get("submissions") or {}) if name not in already]
    bundles: list[dict[str, Any]] = []
    for fullname in known[-config.refresh_limit :] if config.refresh_limit else ():
        payload = info.get(fullname)
        if payload is None or is_gone(payload):
            continue
        bundles.append(_bundle(client, config, payload))
    return bundles


def _iter_rows(client: Any, config: _RedditConfig, state: dict) -> Iterator[dict[str, Any]]:
    """Fetch one sync round's payloads and hand them to :func:`sync_submissions`."""
    subreddits = config.subreddits or tuple(
        name for name in (normalize_subreddit(s) for s in client.subscribed_subreddits()) if name
    )
    if not subreddits:
        logger.warning(
            "Reddit: no subreddits configured and the account subscribes to none — nothing to sync."
        )
        return

    # The deletion re-check runs first: its payloads double as the current
    # `t3` data for the refresh window below, so nothing is fetched twice.
    info = _recheck_known(client, state)

    listings: dict[str, list[dict[str, Any]]] = {}
    for subreddit in subreddits:
        sub_state = (state.get("subreddits") or {}).get(subreddit) or {}
        raw = _collect_listing(client, subreddit, sub_state.get("newest"), config)
        bundles = [_bundle(client, config, submission) for submission in raw]
        fresh = {name for name in (_fullname(item) for item in raw) if name}
        bundles += _refresh_bundles(client, config, subreddit, state, info, fresh)
        listings[subreddit] = bundles

    yield from sync_submissions(config, state, listings, info)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def reddit_source(
    subreddits: list[str] | None = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    username: str | None = None,
    password: str | None = None,
    refresh_token: str | None = None,
    user_agent: str | None = None,
    comment_depth: int = 10,
    comment_limit: int = 200,
    max_more_requests: int = 10,
    backfill_limit: int = 200,
    refresh_limit: int = 25,
    client: Any = None,
):
    """Return a ``dlt`` resource yielding one row per Reddit submission for ``remember``.

    Args:
        subreddits: Subreddits to ingest — ``"python"``, ``"r/Python"`` and
            ``"/r/python/"`` all work. Omitted or empty falls back to the
            authenticated user's subscribed subreddits.
        client_id: Script-app client id. Falls back to ``REDDIT_CLIENT_ID``.
        client_secret: Script-app secret. Falls back to ``REDDIT_CLIENT_SECRET``.
        username: Reddit account owning the script app. Falls back to
            ``REDDIT_USERNAME``. Not needed when ``refresh_token`` is given.
        password: That account's password. Falls back to ``REDDIT_PASSWORD``.
            Not needed when ``refresh_token`` is given.
        refresh_token: A pre-obtained refresh token, used instead of the
            username/password grant.
        user_agent: Descriptive User-Agent (Reddit throttles generic ones
            hard). Falls back to ``REDDIT_USER_AGENT``, then to a generated
            ``python:cognee-community-connector-reddit:0.1.0 (by /u/<user>)``.
        comment_depth: Maximum comment nesting kept per submission (default 10).
        comment_limit: ``limit`` passed to ``/comments/<id>`` (default 200).
        max_more_requests: Hard budget of ``/api/morechildren`` calls per
            submission (default 10; ``0`` disables ``more`` expansion). This is
            the knob that keeps one popular thread from costing thousands of
            calls.
        backfill_limit: Submissions per subreddit fetched on the very first
            run, before the incremental cursor exists (default 200).
        refresh_limit: How many of the most recently ingested submissions per
            subreddit are re-rendered each run so edits and new/deleted
            comments are noticed (default 25; ``0`` disables the refresh pass
            and makes the sync new-submissions-only).
        client: Object implementing the client surface used here —
            ``subscribed_subreddits()``, ``listing()``, ``comments()``,
            ``more_children()``, ``info()`` (mainly a test-injection point);
            when omitted an OAuth client is built from the credentials above.

    Returns:
        A ``dlt`` resource (``reddit_submissions``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and a ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Reddit connector requires the dlt extra: pip install "cognee[dlt]".'
        ) from exc

    if client is None:
        resolved_id = client_id or os.getenv("REDDIT_CLIENT_ID")
        resolved_secret = client_secret or os.getenv("REDDIT_CLIENT_SECRET")
        if not resolved_id or not resolved_secret:
            raise ValueError(
                "Reddit client_id and client_secret are required (pass client_id=/"
                "client_secret= or set REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET). Create a "
                "script app at https://www.reddit.com/prefs/apps."
            )
        resolved_user = username or os.getenv("REDDIT_USERNAME")
        resolved_password = password or os.getenv("REDDIT_PASSWORD")
        resolved_refresh = refresh_token or os.getenv("REDDIT_REFRESH_TOKEN")
        if not resolved_refresh and not (resolved_user and resolved_password):
            raise ValueError(
                "Reddit username and password are required for a script app (pass "
                "username=/password= or set REDDIT_USERNAME/REDDIT_PASSWORD), unless you "
                "pass refresh_token=."
            )
        resolved_agent = (
            user_agent
            or os.getenv("REDDIT_USER_AGENT")
            or "python:cognee-community-connector-reddit:0.1.0"
            + (f" (by /u/{resolved_user})" if resolved_user else "")
        )
        client = _RedditClient(
            client_id=resolved_id,
            client_secret=resolved_secret,
            user_agent=resolved_agent,
            username=resolved_user,
            password=resolved_password,
            refresh_token=resolved_refresh,
        )

    config = normalize_config(
        subreddits,
        comment_depth=comment_depth,
        comment_limit=comment_limit,
        max_more_requests=max_more_requests,
        backfill_limit=backfill_limit,
        refresh_limit=refresh_limit,
    )

    @dlt.resource(
        name=REDDIT_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # `_deleted` is a boolean hard-delete marker (matching the Google
        # Drive / Gmail / Telegram connectors): rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def reddit_submissions():
        yield from _iter_rows(client, config, dlt.current.resource_state())

    resource = reddit_submissions()
    # Opt into the document ingestion path: each submission row (id/title/
    # content/url) becomes a text document that flows through normal cognify
    # (LLM graph extraction). resolve_dlt_sources reads this marker; it never
    # imports this connector. Sync stays incremental — hand this to remember()
    # with write_disposition="merge" (before/after cursor + _deleted).
    setattr(resource, DOCUMENT_SOURCE_ATTR, REDDIT_SOURCE_NAME)
    return resource
