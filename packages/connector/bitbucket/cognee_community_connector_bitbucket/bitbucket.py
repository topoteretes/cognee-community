"""DLT source for Bitbucket Cloud pull requests and PR comments.

Fetches pull requests and their comments from one or more Bitbucket Cloud
repositories and yields them as a single dlt resource for cognee's ingestion
pipeline.

Like the Notion connector, pull requests and comments are ingested as *normal
documents*: the source declares ``cognee_document_source = "bitbucket"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] =
"bitbucket"`` (not ``"dlt"``) and routes it through the standard cognify
entity-extraction pipeline — the right treatment for prose (PR descriptions,
review comments) — instead of the deterministic dlt-row schema-context path.

Sync model: incremental with a periodic full reconciliation pass, mirroring
the Google Drive and Confluence connectors.

* **Incremental pass** (the common case): pull requests are listed sorted
  newest-updated-first and only those at or after the stored per-repo cursor
  are re-yielded; comments are re-checked for every pull request touched
  this run plus every pull request still known to be ``OPEN`` (a comment can
  be added, edited, or deleted without necessarily bumping its parent pull
  request's own ``updated_on`` — this is not confirmed either way against a
  live workspace, so the connector makes the conservative assumption).
* **Full pass** (on the first run, or every ``full_sync_every`` runs):
  every pull request in the configured states and every one of their
  comments is re-listed and diffed against what was stored, so anything
  that fell out of scope between incremental passes — a pull request
  excluded by a narrowed ``pr_states``, a repository removed from
  ``repo_slugs`` or the workspace, or a comment deleted on a long-closed
  pull request that an incremental pass would not have re-checked — is
  still eventually forgotten.

Bitbucket pull requests can never be deleted (confirmed against the
Bitbucket Cloud REST API — there is no DELETE on a pull request resource),
so a pull request row only disappears if it falls outside the configured
``pr_states`` or its repository drops out of scope; both are only detected
on a full pass. Comments *can* be deleted; a comment that is now flagged
``deleted`` or has simply vanished from the listing is tombstoned, exactly
the same whichever way it disappeared, so it is forgotten from the graph and
vector stores by cognee's existing ``orphan_cleanup`` on the next sync.

``write_disposition="merge"`` (declared on the dlt resource below) is what
makes an unchanged row's upsert a no-op and what makes a row with
``_deleted=True`` actually get removed from the dlt destination — but it
is NOT automatically what `cognee.remember()`/`cognee.add()` use: that
"replace"-by-default routing is controlled by a kwarg the *caller* passes
to ``remember()``/``add()``, not by anything declared on the dlt resource
itself (confirmed in the installed ``cognee`` package: ``resolve_dlt_sources``
resolves ``write_disposition`` from its own ``**kwargs``, defaulting to
``"replace"`` when the caller didn't pass one — see
``cognee/tasks/ingestion/resolve_dlt_sources.py``). Every caller MUST
therefore pass ``write_disposition="merge"`` and ``primary_key="id"``
explicitly, exactly like the Google Drive and Confluence connectors'
documented usage.

Listing, pagination, rendering, and the sync state machine are written as
small, independently testable pure functions that take a session and plain
data (never dlt or module state directly), mirroring the Google Drive
connector's ``_iter_rows(service, config, state)`` shape.

Bitbucket's native Wiki feature has been fully removed by Atlassian
(discontinued cloud-wide); there is nothing left to connect to, so it is not
part of this connector at all.
"""

import os
import random
import time
from datetime import datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("bitbucket_connector")

# dlt resource / staging-table name for Bitbucket documents (PRs + comments
# share one table — both rows follow the same {id, title, content, url} shape).
BITBUCKET_TABLE_NAME = "bitbucket_documents"
BITBUCKET_SOURCE_NAME = "bitbucket"

_API_BASE = "https://api.bitbucket.org/2.0"
_ALL_PR_STATES = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")
_DEFAULT_FULL_SYNC_EVERY = 10

# Retry budget for rate-limited / transient Bitbucket API responses.
_MAX_RETRIES = 5
_PAGE_LEN = 100

_EXTRA_HINT = (
    'The Bitbucket connector requires the "bitbucket" extra: pip install "cognee[bitbucket]" '
    "(provides dlt and requests)."
)

_SCOPES_HINT = (
    "Check that the configured credential is valid and has not expired or been revoked. "
    "A personal API token needs the read:workspace:bitbucket, read:repository:bitbucket, and "
    "read:pullrequest:bitbucket scopes; an OAuth or access token needs account, repository, "
    "and pullrequest."
)


def bitbucket_source(
    workspace: str,
    *,
    repo_slugs: list[str] | None = None,
    email: str | None = None,
    api_token: str | None = None,
    access_token: str | None = None,
    pr_states: tuple[str, ...] = _ALL_PR_STATES,
    full_sync_every: int = _DEFAULT_FULL_SYNC_EVERY,
    session: Any = None,
):
    """Create a dlt source that yields Bitbucket pull requests and comments.

    Args:
        workspace: Bitbucket Cloud workspace id (slug), e.g. ``"my-team"``.
        repo_slugs: Restrict ingestion to these repository slugs. When
            omitted, every repository visible in the workspace is synced.
        email: Atlassian account email, paired with ``api_token`` for Basic
            auth. Falls back to ``BITBUCKET_EMAIL``.
        api_token: Bitbucket API token with scopes. Paired with ``email`` for
            Basic auth, or used alone as a bearer token. Falls back to
            ``BITBUCKET_API_TOKEN``.
        access_token: Any bearer token — an OAuth 2.0 access token, or a
            repository/workspace access token. Falls back to
            ``BITBUCKET_ACCESS_TOKEN``. Mutually exclusive with ``api_token``.
        pr_states: Pull request states to include. Defaults to all four
            states Bitbucket supports; the API itself defaults to open-only,
            so this is always sent explicitly.
        full_sync_every: Run a full reconciliation pass every this many runs
            (1 means every run is a full pass). Between full passes, forget
            -on-delete for closed pull requests' comments and for pull
            requests/repos that dropped out of scope is delayed until the
            next full pass; see the module docstring.
        session: Pre-built ``requests`` session (mainly a test-injection
            point); when omitted one is built from the credentials above.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    if not workspace:
        raise ValueError("bitbucket_source requires a non-empty workspace.")
    if repo_slugs is not None and (not repo_slugs or any(not slug for slug in repo_slugs)):
        raise ValueError("repo_slugs, if given, must be a non-empty list of non-empty slugs.")
    if not pr_states:
        raise ValueError("pr_states must not be empty.")
    invalid_states = sorted(set(pr_states) - set(_ALL_PR_STATES))
    if invalid_states:
        raise ValueError(
            f"Invalid pr_states {invalid_states}; must be a subset of {_ALL_PR_STATES}."
        )
    if full_sync_every < 1:
        raise ValueError("full_sync_every must be >= 1 (1 means every run is a full pass).")

    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if session is None:
        session = _make_session(email=email, api_token=api_token, access_token=access_token)

    @dlt.resource(
        name=BITBUCKET_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # `_deleted` is a boolean hard-delete marker: rows where it is True
        # are removed from the dlt destination on merge, which propagates
        # the deletion through cognee's orphan_cleanup. Callers must pass
        # write_disposition="merge" explicitly to remember()/add() too (see
        # the module docstring) -- this decorator value alone is not enough.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def bitbucket_documents():
        yield from _iter_rows(
            session, workspace, repo_slugs, pr_states, full_sync_every, dlt.current.resource_state()
        )

    @dlt.source(name=BITBUCKET_SOURCE_NAME)
    def _bitbucket():
        return bitbucket_documents

    source = _bitbucket()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, BITBUCKET_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Auth / session
# ---------------------------------------------------------------------------


def _resolve_credentials(
    email: str | None, api_token: str | None, access_token: str | None
) -> tuple[str, Any]:
    """Resolve the credential to use, params first then the matching env var.

    Returns ``("bearer", token)`` or ``("basic", (email, api_token))``.
    Raises ``ValueError`` (never containing the credential value) when the
    combination given is ambiguous or incomplete.
    """
    email = email or os.environ.get("BITBUCKET_EMAIL")
    api_token = api_token or os.environ.get("BITBUCKET_API_TOKEN")
    access_token = access_token or os.environ.get("BITBUCKET_ACCESS_TOKEN")

    if access_token and api_token:
        raise ValueError(
            "bitbucket_source: pass either access_token= or api_token= (optionally with "
            "email= for Basic auth), not both."
        )
    if access_token:
        return "bearer", access_token
    if email and api_token:
        return "basic", (email, api_token)
    if api_token:
        return "bearer", api_token
    raise ValueError(
        "bitbucket_source requires credentials: set access_token= (or BITBUCKET_ACCESS_TOKEN) "
        "for a bearer token, or email= + api_token= (or BITBUCKET_EMAIL + BITBUCKET_API_TOKEN) "
        "for Basic auth, or api_token= alone (or BITBUCKET_API_TOKEN) to use it as a bearer "
        "token."
    )


def _make_session(email: str | None, api_token: str | None, access_token: str | None) -> Any:
    """Build a ``requests`` session authenticated against the Bitbucket Cloud API.

    ``requests`` is imported lazily so it stays an optional dependency
    (``pip install "cognee[bitbucket]"``).
    """
    try:
        import requests
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    scheme, credential = _resolve_credentials(email, api_token, access_token)
    session = requests.Session()
    if scheme == "basic":
        session.auth = credential
    else:
        session.headers["Authorization"] = f"Bearer {credential}"
    session.headers["Accept"] = "application/json"
    return session


# ---------------------------------------------------------------------------
# Retrying GET
# ---------------------------------------------------------------------------


def _sleep(seconds: float) -> None:
    """Thin wrapper around ``time.sleep`` so tests can monkeypatch it to run instantly."""
    time.sleep(seconds)


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    """Seconds to wait before retrying: ``Retry-After`` if present, else backoff + jitter.

    Bitbucket does not document a ``Retry-After`` header, so the exponential
    term (with jitter, to avoid a thundering herd on a shared rate-limit
    bucket) is the expected path; honoring ``Retry-After`` is a defensive
    extra in case a proxy or future API version adds it.
    """
    if retry_after is not None:
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            pass
    return (2**attempt) + random.random()


def _get_json(session: Any, url: str, params: Any = None) -> dict:
    """GET a Bitbucket API URL and return parsed JSON, retrying transient failures.

    429 (rate limited) and 502/503/504 (transient server errors) are retried
    with backoff up to ``_MAX_RETRIES`` times. 401/403 raise with a scopes
    hint (never the credential itself); 404 raises naming the resource path
    (not a credentials problem). Anything else is raised via
    ``raise_for_status``. No status is ever swallowed: a failure here must
    abort the whole sync so a partial listing can never be mistaken for
    deletions.
    """
    for attempt in range(_MAX_RETRIES):
        response = session.get(url, params=params)
        status = response.status_code

        if status == 200:
            return response.json()
        if status in (401, 403):
            raise RuntimeError(
                f"Bitbucket API authentication failed (status {status}). {_SCOPES_HINT}"
            )
        if status == 404:
            raise RuntimeError(f"Bitbucket API resource not found: {url}")
        if status not in (429, 502, 503, 504):
            response.raise_for_status()
            raise RuntimeError(f"Unexpected Bitbucket API response from {url}: status {status}.")

        if attempt == _MAX_RETRIES - 1:
            raise RuntimeError(
                f"Bitbucket API request to {url} failed after {_MAX_RETRIES} attempts "
                f"(last status {status})."
            )
        if response.headers.get("X-RateLimit-NearLimit") == "true":
            logger.warning("Bitbucket: close to the rate limit for %s.", url)
        delay = _retry_delay(response.headers.get("Retry-After"), attempt)
        logger.warning(
            "Bitbucket: %s returned %d, retrying in %.1fs (%d/%d).",
            url,
            status,
            delay,
            attempt + 1,
            _MAX_RETRIES,
        )
        _sleep(delay)

    raise RuntimeError(f"Bitbucket API request to {url} did not succeed.")  # pragma: no cover


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _with_default_pagelen(params: Any) -> Any:
    """Add ``pagelen=100`` to params (a dict or a list of (key, value) tuples)."""
    if params is None:
        return [("pagelen", _PAGE_LEN)]
    if isinstance(params, dict):
        merged = dict(params)
        merged.setdefault("pagelen", _PAGE_LEN)
        return merged
    if any(key == "pagelen" for key, _ in params):
        return list(params)
    return [*params, ("pagelen", _PAGE_LEN)]


def _paginate(session: Any, url: str, params: Any = None):
    """Yield ``values`` items across Bitbucket's pagination.

    ``pagelen=100`` is requested on the first call only. Every later call
    follows the ``next`` URL from the previous response exactly as returned —
    Bitbucket's own docs warn that clients must never reconstruct pagination
    URLs themselves, since ``next`` can be an opaque iterator cursor rather
    than a simple page number, and it already carries every query param the
    first call sent. A ``next`` URL that repeats one already visited raises,
    rather than looping forever.
    """
    next_url: str | None = url
    next_params = _with_default_pagelen(params)
    seen_urls: set[str] = set()

    while next_url:
        if next_url in seen_urls:
            raise RuntimeError(f"Bitbucket pagination loop detected at {next_url}.")
        seen_urls.add(next_url)

        data = _get_json(session, next_url, params=next_params)
        yield from data.get("values", []) or []

        next_url = data.get("next")
        next_params = None  # the next URL already carries every query param


# ---------------------------------------------------------------------------
# Listing: repos / PRs
# ---------------------------------------------------------------------------


def _iter_repo_slugs(session: Any, workspace: str, repo_slugs: list[str] | None):
    """Yield the repo slugs to sync: as given, or every repo in the workspace.

    When ``repo_slugs`` is given, slugs are yielded as-is without a separate
    existence check — an invalid or inaccessible slug surfaces naturally as a
    404 (aborting the sync) from the first pull-request listing call made for
    that repo, without spending an extra request just to pre-check it.
    """
    if repo_slugs:
        yield from repo_slugs
        return
    for repo in _paginate(session, f"{_API_BASE}/repositories/{workspace}"):
        slug = repo.get("slug")
        if slug:
            yield slug


def _iter_pull_requests(session: Any, workspace: str, repo_slug: str, pr_states: tuple[str, ...]):
    """Yield every pull request in the given states, in whatever order the API returns.

    Bitbucket's pull-request listing defaults to open-only when no ``state``
    is sent, so every wanted state is sent explicitly as its own repeated
    ``state`` query parameter (Bitbucket's documented way to OR multiple
    values for this field). Used for a full pass (every PR, every state) and
    as the incremental pass's fallback when there is no stored cursor yet.
    """
    url = f"{_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests"
    params = [("state", state) for state in pr_states]
    yield from _paginate(session, url, params=params)


def _list_incremental_prs(
    session: Any, workspace: str, repo_slug: str, pr_states: tuple[str, ...], cursor: str
) -> list[dict]:
    """List pull requests sorted newest-updated-first, stopping once an item
    is strictly older than ``cursor`` (every later item in a correctly
    sorted sequence is older too). Ties with the cursor are included
    (``>=``) — re-yielding a tied PR is a harmless no-op upsert under
    ``write_disposition="merge"``.

    If the sequence is ever found not to be sorted as expected, the early
    stop is permanently disabled for the rest of this call and every
    remaining page is scanned instead of trusting the sort — so a violated
    sort assumption can only cost extra requests, never a missed PR.
    """
    url = f"{_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests"
    params = [("state", state) for state in pr_states] + [("sort", "-updated_on")]
    cursor_dt = _parse_dt(cursor)

    in_scope: list[dict] = []
    previous_dt: datetime | None = None
    trust_sort = True

    for pr in _paginate(session, url, params=params):
        updated_dt = _parse_dt(pr["updated_on"])
        is_first_item = previous_dt is None

        if trust_sort and not is_first_item and updated_dt > previous_dt:
            logger.warning(
                "Bitbucket: pull-request listing for %s/%s was not sorted by -updated_on as "
                "expected; scanning every page instead of stopping early.",
                workspace,
                repo_slug,
            )
            trust_sort = False
        previous_dt = updated_dt

        if updated_dt >= cursor_dt:
            in_scope.append(pr)
        elif trust_sort and not is_first_item:
            # Never break on the very first item examined: with nothing yet
            # to compare it against, an out-of-scope first item is just as
            # likely to be a sort violation (the listing starting with a
            # stale item despite sort=-updated_on) as a genuinely exhausted
            # scope, and only the latter should stop us. Requiring at least
            # one prior item lets a real violation be detected (and the
            # early stop disabled) before we ever act on it.
            break

    return in_scope


# ---------------------------------------------------------------------------
# Comments: fetch + reconcile
# ---------------------------------------------------------------------------


def _fetch_live_comments(session: Any, workspace: str, repo_slug: str, pr_id) -> list[dict]:
    """Fetch and fully paginate a pull request's comments, filtering out
    anything flagged ``deleted`` or left with no text.

    Always makes the API call. Whether to skip it entirely for a PR with no
    comments at all is a decision for the caller (``_sync_repo_comments``),
    since "no comments" can only be trusted when nothing is stored for that
    PR from a prior run either.
    """
    url = f"{_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
    live = []
    for comment in _paginate(session, url):
        if comment.get("deleted"):
            continue
        if not ((comment.get("content") or {}).get("raw") or "").strip():
            continue
        live.append(comment)
    return live


def _sync_repo_comments(
    session: Any,
    workspace: str,
    repo_slug: str,
    prs_to_check: dict[str, dict],
    prior_comment_ids: dict[str, list[str]],
) -> tuple[list[dict], dict[str, list[str]]]:
    """Reconcile comments for a batch of pull requests against ids stored
    for them last run.

    A comment that is now missing from the listing and one that comes back
    flagged ``deleted`` are indistinguishable here on purpose: both are
    already excluded by ``_fetch_live_comments``, so a plain set difference
    against the prior ids catches either deletion signal identically.

    The empty-sweep guard applies across the whole batch, not per PR, and
    only when at least two PRs were actually fetched: if every one of them
    comes back with zero live comments while some were previously known,
    that is treated as one repo-wide probable transient/misconfiguration
    failure (API hiccup, lost access, …) rather than "everyone's comments
    were deleted at once" — nothing is tombstoned and nothing is dropped
    from state this run. A single PR legitimately losing all of its
    comments does NOT trip this: with only one PR fetched there is no
    second, independent data point to corroborate a systemic failure
    against, so the result is trusted as-is (exactly the common, expected
    case of a comment genuinely being deleted). With two or more PRs
    fetched, one of them legitimately going to zero does not trip it
    either, because any other checked PR with a normal comment count keeps
    the batch total above zero.

    ``prs_to_check``: ``{pr_id: pr_dict}``. Returns ``(rows, new_comment_ids)``
    with an entry in ``new_comment_ids`` for every key in ``prs_to_check``.
    """
    live_ids_by_pr: dict[str, list[str]] = {}
    comment_rows_by_pr: dict[str, list[dict]] = {}
    checked_pr_ids: list[str] = []

    for pr_id, pr in prs_to_check.items():
        prior_ids = set(prior_comment_ids.get(pr_id, []))
        if not prior_ids and pr.get("comment_count") == 0:
            live_ids_by_pr[pr_id] = []
            comment_rows_by_pr[pr_id] = []
            continue

        live_comments = _fetch_live_comments(session, workspace, repo_slug, pr.get("id"))
        live_ids_by_pr[pr_id] = sorted({str(c["id"]) for c in live_comments})
        comment_rows_by_pr[pr_id] = [
            _comment_to_row(workspace, repo_slug, pr, c) for c in live_comments
        ]
        checked_pr_ids.append(pr_id)

    total_prior = sum(len(prior_comment_ids.get(pid, [])) for pid in checked_pr_ids)
    total_live = sum(len(live_ids_by_pr[pid]) for pid in checked_pr_ids)

    new_comment_ids: dict[str, list[str]] = {}

    if len(checked_pr_ids) > 1 and total_prior > 0 and total_live == 0:
        logger.warning(
            "Bitbucket: comment sweep for %s/%s returned 0 live comments across %d "
            "re-checked pull request(s) but %d comment(s) were known; skipping comment "
            "forget-on-delete for this repo this run.",
            workspace,
            repo_slug,
            len(checked_pr_ids),
            total_prior,
        )
        for pr_id in prs_to_check:
            new_comment_ids[pr_id] = sorted(prior_comment_ids.get(pr_id, []))
        return [], new_comment_ids

    rows: list[dict] = []
    for pr_id in prs_to_check:
        rows.extend(comment_rows_by_pr.get(pr_id, []))
        live_ids = set(live_ids_by_pr.get(pr_id, []))
        for comment_id in sorted(set(prior_comment_ids.get(pr_id, [])) - live_ids):
            rows.append(_comment_tombstone(workspace, repo_slug, pr_id, comment_id))
        new_comment_ids[pr_id] = sorted(live_ids)

    return rows, new_comment_ids


# ---------------------------------------------------------------------------
# Rendering / row building / stable ids
# ---------------------------------------------------------------------------


def _pr_doc_id(workspace: str, repo_slug: str, pr_id) -> str:
    return f"bitbucket:{workspace}:{repo_slug}:pr:{pr_id}"


def _comment_doc_id(workspace: str, repo_slug: str, pr_id, comment_id) -> str:
    return f"bitbucket:{workspace}:{repo_slug}:pr:{pr_id}:comment:{comment_id}"


def _pr_tombstone(workspace: str, repo_slug: str, pr_id) -> dict:
    return {"id": _pr_doc_id(workspace, repo_slug, pr_id), "_deleted": True}


def _comment_tombstone(workspace: str, repo_slug: str, pr_id, comment_id) -> dict:
    return {"id": _comment_doc_id(workspace, repo_slug, pr_id, comment_id), "_deleted": True}


def _pr_to_row(workspace: str, repo_slug: str, pr: dict) -> dict:
    """Flatten a pull request into a document row.

    Only identity fields plus a fixed, non-volatile metadata header and the
    description are rendered. Fields that change without the PR's substance
    changing (``updated_on``, ``comment_count``, ``task_count``, approval
    state) are deliberately excluded, so a no-op resync keeps the same
    content hash and is not re-ingested or re-cognified.
    """
    pr_id = pr.get("id")
    title = pr.get("title") or ""
    author = (pr.get("author") or {}).get("display_name") or "unknown"
    source_branch = ((pr.get("source") or {}).get("branch") or {}).get("name") or "?"
    destination_branch = ((pr.get("destination") or {}).get("branch") or {}).get("name") or "?"
    reviewer_names = [
        reviewer.get("display_name")
        for reviewer in (pr.get("reviewers") or [])
        if reviewer.get("display_name")
    ]
    reviewers = ", ".join(reviewer_names) if reviewer_names else "none"

    header = (
        f"Repository: {repo_slug}\n"
        f"State: {pr.get('state') or 'UNKNOWN'}\n"
        f"Author: {author}\n"
        f"Branch: {source_branch} -> {destination_branch}\n"
        f"Created: {pr.get('created_on') or 'unknown'}\n"
        f"Reviewers: {reviewers}"
    )
    description = (pr.get("description") or "").strip()
    content = f"{header}\n\n{description}" if description else header

    return {
        "id": _pr_doc_id(workspace, repo_slug, pr_id),
        "title": f"[{repo_slug}#{pr_id}] {title}",
        "content": content,
        "url": ((pr.get("links") or {}).get("html") or {}).get("href") or "",
        "_deleted": False,
    }


def _comment_to_row(workspace: str, repo_slug: str, pr: dict, comment: dict) -> dict:
    """Flatten a pull request comment into a document row.

    The caller has already filtered out deleted and empty comments, so this
    only has to render what is left: who wrote it, a reply note if it is a
    reply to another comment, the file/line it is attached to if it is an
    inline (code-review) comment, then the text.
    """
    pr_id = pr.get("id")
    pr_title = pr.get("title") or ""
    comment_id = comment.get("id")
    author = (comment.get("user") or {}).get("display_name") or "unknown"

    lines = [f"Author: {author}"]
    parent_id = (comment.get("parent") or {}).get("id")
    if parent_id is not None:
        lines.append(f"Reply to comment {parent_id}")
    inline = comment.get("inline") or {}
    path = inline.get("path")
    if path:
        line_no = inline.get("to") if inline.get("to") is not None else inline.get("from")
        lines.append(f"On {path}:{line_no}" if line_no is not None else f"On {path}")

    body = ((comment.get("content") or {}).get("raw") or "").strip()
    content = "\n".join(lines) + "\n\n" + body

    pr_url = ((pr.get("links") or {}).get("html") or {}).get("href") or ""
    comment_url = ((comment.get("links") or {}).get("html") or {}).get("href") or pr_url

    return {
        "id": _comment_doc_id(workspace, repo_slug, pr_id, comment_id),
        "title": f"Comment on [{repo_slug}#{pr_id}] {pr_title}",
        "content": content,
        "url": comment_url,
        "_deleted": False,
    }


def _parse_dt(value: str) -> datetime:
    """Parse a Bitbucket timestamp into a timezone-aware datetime.

    Never compare these as strings: e.g. a missing fractional-seconds
    component, or ``Z`` vs an explicit ``+00:00`` offset, would sort
    incorrectly as text even though they compare correctly as datetimes.
    """
    return datetime.fromisoformat(value)


def _newer(current: str | None, candidate: str | None) -> str | None:
    """Return whichever of two Bitbucket timestamps is later, as the original string.

    ``current`` may be ``None`` (no cursor yet); ``candidate`` is kept as-is
    if it is absent or not later, so the stored cursor is always exactly a
    timestamp Bitbucket itself returned, never a locally constructed one.
    """
    if not candidate:
        return current
    if current is None or _parse_dt(candidate) > _parse_dt(current):
        return candidate
    return current


# ---------------------------------------------------------------------------
# Sync state machine (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------


def _reconcile_vanished_repos(
    state: dict, workspace: str, current_repo_slugs: list[str]
) -> list[dict]:
    """On a full pass, tombstone every stored id for a repo that dropped out
    of the repository listing entirely (deleted, renamed, or access
    revoked), and drop it from ``state``.

    Guarded the same way as the per-repo comment sweep: a workspace listing
    that comes back with zero repos while some were known is treated as a
    probable transient/misconfiguration blip, not "every repo was deleted"
    — state is left untouched in that case. This step makes no further API
    calls, so there is no partial-failure window within it; it only ever
    runs once ``current_repo_slugs`` has already been fetched successfully.
    """
    repos_state = state.setdefault("repos", {})
    known_slugs = set(repos_state)
    current_slugs = set(current_repo_slugs)

    if known_slugs and not current_slugs:
        logger.warning(
            "Bitbucket: repository listing for workspace %s returned 0 repos but %d were "
            "known; skipping repo-level forget-on-delete this run.",
            workspace,
            len(known_slugs),
        )
        return []

    rows: list[dict] = []
    for repo_slug in sorted(known_slugs - current_slugs):
        repo_state = repos_state.pop(repo_slug)
        for pr_id in repo_state.get("pr_ids", []):
            rows.append(_pr_tombstone(workspace, repo_slug, pr_id))
            for comment_id in repo_state.get("comment_ids", {}).get(pr_id, []):
                rows.append(_comment_tombstone(workspace, repo_slug, pr_id, comment_id))
    return rows


def _sync_repo_full(
    session: Any, workspace: str, repo_slug: str, pr_states: tuple[str, ...], prior: dict
) -> tuple[list[dict], dict]:
    """Full reconciliation pass for one repo: list every PR in the configured
    states, re-list every PR's comments, and tombstone anything stored for
    this repo that is no longer present. Returns ``(rows, new_repo_state)``;
    raises -- without yielding a tombstone or returning new state -- on any
    fetch failure, so the caller never commits a partial result.
    """
    prior_pr_ids = set(prior.get("pr_ids", []))
    prior_comment_ids = prior.get("comment_ids", {})

    current_prs = list(_iter_pull_requests(session, workspace, repo_slug, pr_states))
    current_pr_by_id = {str(pr["id"]): pr for pr in current_prs}
    current_pr_ids = set(current_pr_by_id)

    if prior_pr_ids and not current_pr_ids:
        # Empty-sweep guard at the PR level: PRs cannot actually be deleted
        # in Bitbucket, so a full listing coming back empty while some were
        # known is unambiguously a transient/misconfiguration failure, never
        # a genuine mass deletion. Keep everything exactly as it was.
        logger.warning(
            "Bitbucket: full pull-request listing for %s/%s returned 0 pull requests but "
            "%d were known; skipping forget-on-delete for this repo this run.",
            workspace,
            repo_slug,
            len(prior_pr_ids),
        )
        return [], dict(prior)

    pr_rows = [_pr_to_row(workspace, repo_slug, pr) for pr in current_pr_by_id.values()]
    pr_state_by_id = {pid: pr.get("state") for pid, pr in current_pr_by_id.items()}
    newest_cursor = None
    for pr in current_pr_by_id.values():
        newest_cursor = _newer(newest_cursor, pr.get("updated_on"))

    comment_rows, new_comment_ids = _sync_repo_comments(
        session, workspace, repo_slug, current_pr_by_id, prior_comment_ids
    )

    vanished_rows: list[dict] = []
    for pr_id in sorted(prior_pr_ids - current_pr_ids):
        vanished_rows.append(_pr_tombstone(workspace, repo_slug, pr_id))
        for comment_id in prior_comment_ids.get(pr_id, []):
            vanished_rows.append(_comment_tombstone(workspace, repo_slug, pr_id, comment_id))

    new_state = {
        "pr_cursor": newest_cursor,
        "pr_ids": sorted(current_pr_ids),
        "comment_ids": new_comment_ids,
        "pr_state_by_id": pr_state_by_id,
    }
    return pr_rows + comment_rows + vanished_rows, new_state


def _sync_repo_incremental(
    session: Any, workspace: str, repo_slug: str, pr_states: tuple[str, ...], prior: dict
) -> tuple[list[dict], dict]:
    """Incremental pass for one repo: fetch only PRs updated at or after the
    stored cursor, re-check comments for every PR in scope plus every PR
    currently known to be ``OPEN``, and diff against stored comment ids to
    catch deletions. Never tombstones a PR itself -- PRs cannot be deleted
    in Bitbucket, so only a full pass (reacting to a narrowed ``pr_states``)
    ever removes one.
    """
    prior_cursor = prior.get("pr_cursor")
    prior_pr_ids = set(prior.get("pr_ids", []))
    prior_comment_ids = prior.get("comment_ids", {})
    pr_state_by_id = dict(prior.get("pr_state_by_id", {}))

    if prior_cursor is None:
        # No cursor yet for this repo specifically (e.g. it was added to the
        # workspace after the last full pass) -- fall back to a full listing
        # for this repo alone rather than erroring or skipping it.
        in_scope_prs = list(_iter_pull_requests(session, workspace, repo_slug, pr_states))
    else:
        in_scope_prs = _list_incremental_prs(session, workspace, repo_slug, pr_states, prior_cursor)

    in_scope_by_id = {str(pr["id"]): pr for pr in in_scope_prs}
    pr_rows = [_pr_to_row(workspace, repo_slug, pr) for pr in in_scope_by_id.values()]

    newest_cursor = prior_cursor
    for pr_id, pr in in_scope_by_id.items():
        pr_state_by_id[pr_id] = pr.get("state")
        newest_cursor = _newer(newest_cursor, pr.get("updated_on"))

    # Re-check comments for every PR touched this run, plus every PR still
    # known to be OPEN even if it wasn't touched (a comment can arrive
    # without bumping the parent PR's own updated_on -- see module
    # docstring). An OPEN PR not returned by the incremental listing has no
    # fresh data here, so synthesize the minimal shape _sync_repo_comments
    # needs; comment_count is unknown without a refetch, so leave it unset
    # (None), which _sync_repo_comments treats as "do not trust, go fetch".
    open_pr_ids = {pid for pid, pr_state in pr_state_by_id.items() if pr_state == "OPEN"}
    comment_check_ids = set(in_scope_by_id) | open_pr_ids
    prs_to_check = {
        pid: in_scope_by_id.get(pid) or {"id": pid, "comment_count": None}
        for pid in comment_check_ids
    }

    comment_rows, checked_comment_ids = _sync_repo_comments(
        session, workspace, repo_slug, prs_to_check, prior_comment_ids
    )
    new_comment_ids = dict(prior_comment_ids)
    new_comment_ids.update(checked_comment_ids)

    new_state = {
        "pr_cursor": newest_cursor,
        "pr_ids": sorted(prior_pr_ids | set(in_scope_by_id)),
        "comment_ids": new_comment_ids,
        "pr_state_by_id": pr_state_by_id,
    }
    return pr_rows + comment_rows, new_state


def _iter_rows(
    session: Any,
    workspace: str,
    repo_slugs: list[str] | None,
    pr_states: tuple[str, ...],
    full_sync_every: int,
    state: dict,
):
    """Yield one row per in-scope PR/comment, plus tombstones for removed
    PRs/comments/repos.

    Runs a FULL reconciliation pass (every PR, every comment, diffed
    against stored ids) when ``state`` is empty or due by the
    ``full_sync_every`` counter; otherwise an INCREMENTAL pass per repo.

    Decoupled from dlt's resource-state machinery (which needs an active
    pipeline context) so it is directly unit-testable with a fake session
    and a plain dict standing in for dlt's resource state. Each repo's new
    state is written into ``state["repos"][repo_slug]`` only once that
    repo's pass has returned successfully -- an exception partway through
    a repo propagates immediately, before that repo's (or any later repo's)
    state is touched. dlt's own pipeline-state manager additionally rolls
    back the entire state dict to its pre-run value if any exception
    reaches it (``dlt.pipeline.pipeline.Pipeline.managed_state``), so this
    function's own discipline is a second, independent safety layer on top
    of that guarantee rather than the only thing protecting against it.
    """
    prior_repos = dict(state.get("repos", {}))
    runs_since_full = state.get("runs_since_full", 0)
    is_full = not prior_repos or runs_since_full + 1 >= full_sync_every

    current_repo_slugs = list(_iter_repo_slugs(session, workspace, repo_slugs))

    total = 0
    if is_full:
        vanished_rows = _reconcile_vanished_repos(state, workspace, current_repo_slugs)
        yield from vanished_rows
        total += len(vanished_rows)

    for repo_slug in current_repo_slugs:
        prior_repo_state = prior_repos.get(repo_slug, {})
        if is_full:
            rows, new_repo_state = _sync_repo_full(
                session, workspace, repo_slug, pr_states, prior_repo_state
            )
        else:
            rows, new_repo_state = _sync_repo_incremental(
                session, workspace, repo_slug, pr_states, prior_repo_state
            )
        yield from rows
        total += len(rows)
        state.setdefault("repos", {})[repo_slug] = new_repo_state

    state["runs_since_full"] = 0 if is_full else runs_since_full + 1
    logger.info(
        "Bitbucket: %s sync yielded %d document(s) across %d repo(s).",
        "full" if is_full else "incremental",
        total,
        len(current_repo_slugs),
    )
