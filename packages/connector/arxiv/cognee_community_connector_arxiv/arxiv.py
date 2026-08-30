"""arXiv connector for cognee — a ``dlt`` source that turns a paper feed into memory.

Pull arXiv paper metadata and abstracts into cognee, incrementally and with
forget-on-deletion — "ask my reading list".  Like the sibling Confluence and
Gmail connectors this builds entirely on the existing DLT ingestion subsystem;
the source produced here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_arxiv import arxiv_source

    await cognee.remember(
        arxiv_source(categories=["cs.AI"]),
        dataset_name="papers",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by arXiv id
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — none.  The arXiv Atom API at ``https://export.arxiv.org/api/query``
  is public and read-only; the connector only issues ``GET`` requests.
* **Primary key** — the *version-stripped* arXiv id (``2608.09617``, not
  ``2608.09617v2``).  arXiv mints a new version suffix on every revision, so
  keying on the raw id would ingest ``v2`` as a second paper and leave ``v1``
  behind forever.  The version is kept as its own ``version`` column, and
  combined with ``write_disposition="merge"`` a revision upserts in place.
* **Incremental cursor** — the paper's submission timestamp (Atom
  ``published``), narrowed server-side with an arXiv
  ``submittedDate:[<cursor> TO 999912312359]`` range and walked in ascending
  order.  The cursor is persisted in dlt's per-resource state, so re-running
  ``remember`` resumes where it left off and re-embeds only the delta.
* **Forget-on-delete** — arXiv has no deletion feed, so each run sweeps the ids
  currently matching the configured scope and compares them against the ids
  seen on the previous run (also kept in resource state).  Papers that vanished
  are emitted with the ``_deleted`` hard-delete marker; dlt removes those rows
  on ``merge`` and cognee's existing ``orphan_cleanup`` then purges them from
  the graph + vector + relational stores.  No parallel cleanup path.
* **Completeness** — the sweep does double duty.  Because it lists the scope
  *without* a date filter, it also catches papers that entered the scope with an
  old submission date (cross-listed into a tracked category, or matching an
  author after the fact), which the server-side ``submittedDate`` window in the
  incremental pass structurally cannot see.
* **Rate limit** — arXiv asks for roughly one request every three seconds.  The
  delay is enforced in :func:`_ArxivClient.get` for *every* request rather than
  discovered through 429s, and retries use exponential backoff on top.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows from
   the dlt destination (default 50).  For a real corpus pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

.. important::
   The cursor tracks *submission* date, per the connector's spec.  A revision to
   an already-ingested paper bumps Atom ``updated`` but not ``published``, so it
   is not re-fetched by the incremental pass.  Set ``track_revisions=True`` to
   additionally sweep known papers for bumped ``updated`` stamps — this costs no
   extra requests, because the deletion sweep already retrieves those entries.

.. note::
   Deletion detection needs a sweep of the whole configured scope, and arXiv has
   no ids-only endpoint — a sweep costs one request per ``page_size`` papers, at
   ~3s each.  Keep the scope bounded (specific categories/authors, and the
   ``max_papers`` cap) or pass ``detect_deletions=False`` to skip the sweep —
   which also gives up the completeness guarantee above, since backdated papers
   are only ever seen by the sweep.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError

from cognee.shared.logging_utils import get_logger

logger = get_logger("arxiv_connector")

# Public Atom API. No key, no account, read-only.
_API_URL = "https://export.arxiv.org/api/query"

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"
_OPENSEARCH_NS = "{http://a9.com/-/spec/opensearch/1.1/}"

# arXiv asks for ~1 request every 3 seconds. Enforced ahead of every request.
_MIN_REQUEST_INTERVAL = 3.0

# arXiv rejects page sizes above 2000 and is unhappy well before that.
_MAX_PAGE_SIZE = 1000
_DEFAULT_PAGE_SIZE = 100

# Retry budget for rate-limited / transient responses.
_MAX_RETRIES = 5

# Open-ended upper bound for the submittedDate range: arXiv requires both ends.
_FAR_FUTURE = "999912312359"

# Trailing version suffix on an arXiv id: "2608.09617v2" -> "v2".
_VERSION_RE = re.compile(r"v(\d+)$")
_WS_RE = re.compile(r"\s+")

_DEPS_HINT = (
    'The arXiv connector requires the "arxiv" extra: pip install "cognee[arxiv]" (provides dlt).'
)


# ---------------------------------------------------------------------------
# HTTP client (module-private)
# ---------------------------------------------------------------------------
class _ArxivClient:
    """Rate-limited GET client for the arXiv Atom API.

    Uses ``urllib`` from the standard library so the connector adds no HTTP
    dependency of its own. The 3-second courtesy delay is applied *before* every
    request (not only after a 429), because arXiv throttles by source address and
    a burst gets the whole host blocked rather than one request rejected.
    """

    def __init__(self, min_interval: float = _MIN_REQUEST_INTERVAL) -> None:
        self.min_interval = min_interval
        self._last_request_at: float | None = None

    def _wait_for_slot(self) -> None:
        """Sleep just long enough that requests stay ``min_interval`` apart."""
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, params: dict[str, Any]) -> str:
        """GET the API with ``params``, retrying transient failures. Returns XML."""
        from urllib.parse import urlencode
        from urllib.request import urlopen

        url = f"{_API_URL}?{urlencode(params)}"
        for attempt in range(_MAX_RETRIES):
            self._wait_for_slot()
            try:
                # Fixed https host, no user-supplied scheme.
                with urlopen(url, timeout=60) as response:
                    return response.read().decode("utf-8")
            except (HTTPError, URLError, TimeoutError) as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                    raise
                delay = _retry_after(exc, attempt)
                logger.warning(
                    "arXiv: %s — retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
            finally:
                self._last_request_at = time.monotonic()
        # _MAX_RETRIES is >= 1, so the loop either returns or raises.
        raise RuntimeError("unreachable: arXiv retry loop exhausted without raising")


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / network errors worth retrying."""
    if isinstance(exc, HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    # URLError covers DNS/connection resets; TimeoutError covers read timeouts.
    return isinstance(exc, (URLError, TimeoutError))


def _retry_after(exc: Exception, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    headers = getattr(exc, "headers", None)
    header = None
    if headers is not None:
        header = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt) * _MIN_REQUEST_INTERVAL


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------
def build_search_query(
    categories: list[str] | None,
    authors: list[str] | None,
    extra_query: str | None,
    since: str | None,
) -> str:
    """Compose an arXiv ``search_query`` from the configured scope.

    Categories are OR-ed with each other, authors are OR-ed with each other, and
    the resulting groups are AND-ed together — "any of these categories, written
    by any of these authors". ``since`` becomes a ``submittedDate`` range so the
    incremental window is filtered server-side rather than by discarding rows.
    """
    clauses: list[str] = []

    if categories:
        clauses.append(_or_group(f"cat:{category}" for category in categories))
    if authors:
        # Author names contain spaces, which arXiv needs quoted to keep as one term.
        clauses.append(_or_group(f'au:"{author}"' for author in authors))
    if extra_query:
        clauses.append(f"({extra_query})")
    if since:
        clauses.append(f"submittedDate:[{since} TO {_FAR_FUTURE}]")

    if not clauses:
        raise ValueError(
            "arxiv_source needs a scope: pass categories=[...], authors=[...], "
            "or extra_query=... (an unscoped query would sync all of arXiv)."
        )
    return " AND ".join(clauses)


def _or_group(terms: Iterator[str] | Any) -> str:
    """Join terms with OR, parenthesised when there is more than one."""
    joined = " OR ".join(terms)
    return f"({joined})" if " OR " in joined else joined


def _to_cursor(timestamp: str) -> str:
    """Convert an Atom timestamp to arXiv's ``YYYYMMDDHHMM`` query format.

    ``2026-08-25T00:20:29Z`` -> ``202608250020``. arXiv's range filter has
    minute granularity, so the seconds are dropped; the range is inclusive on
    both ends, which is why the caller re-filters on the exact timestamp.
    """
    digits = re.sub(r"\D", "", timestamp)
    if len(digits) < 12:
        raise ValueError(f"cannot build an arXiv cursor from timestamp {timestamp!r}")
    return digits[:12]


# ---------------------------------------------------------------------------
# Atom parsing
# ---------------------------------------------------------------------------
def split_arxiv_id(entry_id: str) -> tuple[str, int | None]:
    """Split an arXiv entry id URL into a stable id and its version number.

    ``http://arxiv.org/abs/2608.09617v2`` -> ``("2608.09617", 2)``. The stable
    half is the primary key; keying on the versioned id would make every
    revision a new row and orphan the previous version forever.
    """
    raw = entry_id.rsplit("/abs/", 1)[-1].strip()
    match = _VERSION_RE.search(raw)
    if not match:
        return raw, None
    return raw[: match.start()], int(match.group(1))


def _clean(text: str | None) -> str:
    """Collapse the newlines arXiv wraps titles and abstracts at."""
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


def _entry_to_row(entry: ET.Element) -> dict[str, Any]:
    """Flatten an Atom ``entry`` into a document row for cognee.

    ``content`` is what gets cognified, so the abstract is prefixed with the
    title and author list: the entity-extraction pipeline reads the row's text,
    not its columns, and a bare abstract loses who wrote it.
    """
    entry_id = entry.findtext(f"{_ATOM_NS}id") or ""
    paper_id, version = split_arxiv_id(entry_id)

    title = _clean(entry.findtext(f"{_ATOM_NS}title"))
    abstract = _clean(entry.findtext(f"{_ATOM_NS}summary"))
    authors = [
        _clean(name.text)
        for author in entry.findall(f"{_ATOM_NS}author")
        for name in author.findall(f"{_ATOM_NS}name")
        if _clean(name.text)
    ]

    categories = [
        term for category in entry.findall(f"{_ATOM_NS}category") if (term := category.get("term"))
    ]
    primary = entry.find(f"{_ARXIV_NS}primary_category")

    byline = ", ".join(authors)
    content = f"# {title}\n\n{byline}\n\n{abstract}" if byline else f"# {title}\n\n{abstract}"

    return {
        "id": paper_id,
        "version": version,
        "url": f"https://arxiv.org/abs/{paper_id}",
        "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "content": content,
        "categories": categories,
        "primary_category": primary.get("term") if primary is not None else None,
        "published": entry.findtext(f"{_ATOM_NS}published") or "",
        "updated": entry.findtext(f"{_ATOM_NS}updated") or "",
        "doi": entry.findtext(f"{_ARXIV_NS}doi"),
        "journal_ref": entry.findtext(f"{_ARXIV_NS}journal_ref"),
        "comment": _clean(entry.findtext(f"{_ARXIV_NS}comment")),
    }


def _deleted_row(paper_id: str) -> dict[str, Any]:
    """Build a hard-delete marker row for a paper that left the corpus."""
    return {"id": paper_id, "_deleted": True}


def parse_feed(xml_text: str) -> tuple[list[ET.Element], int]:
    """Parse an Atom response into its entries and arXiv's total-results count."""
    root = ET.fromstring(xml_text)
    total_text = root.findtext(f"{_OPENSEARCH_NS}totalResults") or "0"
    try:
        total = int(total_text)
    except ValueError:
        total = 0
    return root.findall(f"{_ATOM_NS}entry"), total


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------
def iter_entries(
    client: _ArxivClient,
    search_query: str,
    *,
    page_size: int = _DEFAULT_PAGE_SIZE,
    max_papers: int | None = None,
) -> Iterator[ET.Element]:
    """Yield Atom entries for ``search_query``, oldest submission first.

    Ascending order is what makes the cursor safe to advance while streaming: a
    run that dies halfway has still only skipped papers strictly older than the
    stamp it recorded. Paging stops on a short page, on the reported total, or
    at ``max_papers``.
    """
    start = 0
    seen = 0
    while True:
        window = page_size
        if max_papers is not None:
            window = min(window, max_papers - seen)
            if window <= 0:
                return

        xml_text = client.get(
            {
                "search_query": search_query,
                "start": start,
                "max_results": window,
                "sortBy": "submittedDate",
                "sortOrder": "ascending",
            }
        )
        entries, total = parse_feed(xml_text)
        if not entries:
            return

        yield from entries
        seen += len(entries)
        start += len(entries)

        # A short page means the result set is exhausted; arXiv also reports the
        # total, which stops us before requesting an empty trailing page.
        if len(entries) < window or start >= total:
            return


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def sync_papers(
    client: _ArxivClient,
    state: dict,
    *,
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    extra_query: str | None = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
    max_papers: int | None = None,
    detect_deletions: bool = True,
    track_revisions: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield papers submitted since the last run, plus hard-delete markers.

    Two passes, and which ones run depends on the configuration:

    * The **incremental pass** narrows ``submittedDate`` to everything at or
      after the stored cursor and emits those papers. On a first run the cursor
      is empty, so this is a full backfill of the scope.
    * The **sweep pass** re-lists the whole scope without a date filter to learn
      which ids still exist; ids known from last run but missing now are emitted
      as deletions. Having every current entry in hand, it also emits papers that
      are new to the corpus but older than the cursor (which the incremental
      pass filters out server-side), and — with ``track_revisions`` — papers
      whose ``updated`` stamp moved.

    ``detect_deletions=False`` skips the sweep entirely, which halves the request
    count for a large scope at the cost of never forgetting withdrawn papers.
    """
    known_ids: set[str] = set(state.get("known_ids", []))
    known_updated: dict[str, str] = dict(state.get("known_updated", {}))
    # Submission stamp per known id. Only needed to tell "this paper is gone"
    # apart from "the capped sweep never reached this paper" (see below).
    known_published: dict[str, str] = dict(state.get("known_published", {}))
    last_submitted: str = state.get("last_submitted", "")
    newest_submitted = last_submitted
    emitted: set[str] = set()
    changed = 0

    # --- Incremental pass -------------------------------------------------
    since = _to_cursor(last_submitted) if last_submitted else None
    incremental_query = build_search_query(categories, authors, extra_query, since)

    for entry in iter_entries(
        client, incremental_query, page_size=page_size, max_papers=max_papers
    ):
        row = _entry_to_row(entry)
        paper_id = row["id"]
        published = row["published"]

        # The range filter has minute granularity and is inclusive, so the last
        # paper of the previous run comes back every time. Skip it only when we
        # already have it — an unknown id at the same stamp is still new to us.
        if paper_id in known_ids and published <= last_submitted:
            continue
        if published > newest_submitted:
            newest_submitted = published

        yield row
        emitted.add(paper_id)
        known_updated[paper_id] = row["updated"]
        known_published[paper_id] = published
        changed += 1

    # --- Sweep pass -------------------------------------------------------
    if not detect_deletions:
        state["last_submitted"] = newest_submitted
        state["known_ids"] = sorted(known_ids | emitted)
        state["known_updated"] = known_updated
        state["known_published"] = known_published
        logger.info("arXiv: %d new/updated paper(s), deletion detection off.", changed)
        return

    current_ids: set[str] = set()
    swept = 0
    # Highest submission stamp the sweep actually reached. The sweep walks in
    # ascending order, so this is the upper edge of the window it can vouch for.
    sweep_high = ""
    sweep_query = build_search_query(categories, authors, extra_query, None)
    for entry in iter_entries(client, sweep_query, page_size=page_size, max_papers=max_papers):
        row = _entry_to_row(entry)
        paper_id = row["id"]
        current_ids.add(paper_id)
        known_published[paper_id] = row["published"]
        swept += 1
        if row["published"] > sweep_high:
            sweep_high = row["published"]

        if paper_id in emitted:
            continue

        # A paper can enter the scope with an old submission date — cross-listed
        # into a tracked category, or an author match appearing after the fact.
        # The incremental pass filters submittedDate server-side, so it cannot
        # see anything below the cursor; the sweep lists the whole scope and
        # already holds the entry, so catching it here costs nothing.
        if paper_id not in known_ids:
            yield row
            emitted.add(paper_id)
            known_updated[paper_id] = row["updated"]
            changed += 1
            continue

        # A revision bumps `updated` but not `published`, so the incremental pass
        # cannot see it either. Same story: the entry is already in hand.
        if not track_revisions:
            continue
        previous_updated = known_updated.get(paper_id)
        if previous_updated and row["updated"] > previous_updated:
            yield row
            emitted.add(paper_id)
            known_updated[paper_id] = row["updated"]
            changed += 1

    # Deletion detection relies on the sweep enumerating every current paper. An
    # empty sweep while papers were previously known almost always means a
    # transient failure (arXiv 503s under load, a network blip, a typo'd category
    # that suddenly matches nothing) rather than a genuine wipe — treating it as
    # "all deleted" would purge the whole dataset and overwrite known_ids with
    # [], making the loss permanent. Skip deletion and preserve state in that case.
    if known_ids and not current_ids:
        logger.warning(
            "arXiv: sweep returned 0 papers but %d were known; skipping deletion "
            "this run to avoid a mass forget-on-delete on a transient sweep.",
            len(known_ids),
        )
        state["last_submitted"] = newest_submitted
        logger.info("arXiv: %d new/updated paper(s), 0 deletion(s).", changed)
        return

    deleted = known_ids - current_ids

    # A sweep stopped by max_papers only proves what exists *inside* the window
    # it covered — everything up to sweep_high. Papers past that edge were never
    # looked at, so an unbounded `known_ids - current_ids` reports them as
    # deleted and purges live memory as soon as the corpus outgrows the cap.
    # Restrict deletion to ids the sweep could actually have seen; an id with no
    # recorded stamp (state written by an older version) is left alone rather
    # than guessed at.
    if max_papers is not None and swept >= max_papers:
        in_window = {
            paper_id
            for paper_id in deleted
            if (seen_at := known_published.get(paper_id)) is not None and seen_at <= sweep_high
        }
        suppressed = len(deleted) - len(in_window)
        if suppressed:
            logger.info(
                "arXiv: %d known paper(s) lie past the max_papers=%d sweep window; "
                "not treating them as deleted.",
                suppressed,
                max_papers,
            )
        deleted = in_window

    for paper_id in sorted(deleted):
        yield _deleted_row(paper_id)
        known_updated.pop(paper_id, None)
        known_published.pop(paper_id, None)

    state["known_ids"] = sorted((known_ids | current_ids | emitted) - deleted)
    state["known_updated"] = known_updated
    state["known_published"] = known_published
    state["last_submitted"] = newest_submitted
    logger.info("arXiv: %d new/updated paper(s), %d deletion(s).", changed, len(deleted))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def arxiv_source(
    *,
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    extra_query: str | None = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
    max_papers: int | None = None,
    detect_deletions: bool = True,
    track_revisions: bool = False,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields arXiv papers for ``remember``.

    Args:
        categories: arXiv category ids to sync, e.g. ``["cs.AI", "cs.LG"]``.
            OR-ed together.
        authors: Author names to sync, e.g. ``["Yoshua Bengio"]``. OR-ed
            together, and AND-ed with ``categories`` when both are given.
        extra_query: Raw arXiv query appended with AND, for anything the two
            arguments above do not cover, e.g. ``'abs:"knowledge graph"'``.
        page_size: Papers per API request (max 1000). Larger pages mean fewer
            round trips, and each round trip costs a 3-second courtesy delay.
        max_papers: Stop after this many papers per pass. ``None`` means the
            whole scope — bound it for a broad category, which can run to
            hundreds of thousands of papers. When set, the sweep only vouches
            for the window it reached, so forget-on-delete applies inside that
            window and papers beyond it are left alone rather than purged.
        detect_deletions: Run the id sweep that powers forget-on-delete. Turn it
            off to halve the request count on a large scope — at the cost of
            never forgetting withdrawn papers, and of missing papers that enter
            the scope with a submission date below the cursor.
        track_revisions: Also re-emit papers whose ``updated`` stamp moved since
            the last run. Requires ``detect_deletions``; costs no extra requests.
        client: Pre-built ``_ArxivClient``. Mainly an injection point for tests;
            when omitted a rate-limited one is built.

    Returns:
        A ``dlt`` resource (``arxiv_papers``) configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column.
        Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_DEPS_HINT) from exc

    if not (categories or authors or extra_query):
        raise ValueError(
            "arxiv_source needs a scope: pass categories=[...], authors=[...], "
            "or extra_query=... (an unscoped query would sync all of arXiv)."
        )
    if page_size < 1 or page_size > _MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}, got {page_size}.")
    if track_revisions and not detect_deletions:
        raise ValueError(
            "track_revisions=True requires detect_deletions=True — revisions are "
            "found by the same sweep that detects deletions."
        )

    @dlt.resource(
        name="arxiv_papers",
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def arxiv_papers():
        http = client or _ArxivClient()
        resource_state = dlt.current.resource_state()
        yield from sync_papers(
            http,
            resource_state,
            categories=categories,
            authors=authors,
            extra_query=extra_query,
            page_size=page_size,
            max_papers=max_papers,
            detect_deletions=detect_deletions,
            track_revisions=track_revisions,
        )

    return arxiv_papers
