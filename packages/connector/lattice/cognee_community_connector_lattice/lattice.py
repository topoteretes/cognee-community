"""DLT source for Lattice goals, feedback and review cycles (full-snapshot sync + forget-on-delete).

Syncs a Lattice workspace into cognee::

    import cognee
    from cognee_community_connector_lattice import lattice_source

    await cognee.remember(
        lattice_source(),  # LATTICE_API_KEY from the environment; goals + review cycles only
        dataset_name="lattice",
    )

Design
------
* **Privacy first** — performance feedback is the most sensitive data Lattice
  holds, so only goals and review-cycle metadata are on by default. Feedback
  needs ``include_feedback=True`` and then covers only feedback shared with the
  whole company unless ``feedback_only_public=False``. Review *responses* need
  the cycle ids in ``review_cycle_ids``. Private and draft goals are skipped.
  Reviewers are never named in a review document, only the direction of the
  review (self, peer, upward, downward). The README explains that the API key
  carries the privileges of the user who created it.
* **Documents** — each goal (with its progress updates), each feedback, each
  review cycle and each person's submitted reviews in a selected cycle becomes
  one short text document that flows through normal cognify. People are named
  from a ``/v1/users`` listing, never emailed or otherwise described.
* **Sync model** — a full snapshot, like the Notion connector. None of the
  goal, feedback or review-cycle listings can filter by ``updatedAt`` (feedback
  has no ``updatedAt`` at all) and the API has no delete feed, so each run
  lists everything that is in scope and ``write_disposition="replace"`` rewrites
  the staging tables with exactly that. A document that is not in the snapshot
  any more (deleted upstream, archived, made private, or taken out of the
  selection) is forgotten by cognee's ``orphan_cleanup``. A document whose text
  did not change keeps its content-hash id, so it is neither re-ingested nor read
  by the LLM again: ``updatedAt``-style change detection by content, not by a
  server filter. A goal's text carries its progress updates, so a new check-in
  changes it even though the goal's own ``updatedAt`` does not move.
* **Errors never delete** — a failed or partial listing (rate limit that does
  not clear, timeout, a cursor that never ends) raises before dlt loads
  anything, so a hiccup is never read as a deletion and the next run starts
  from the same memory.
* **Fixed staging tables** — there is one table per kind (goals, feedback,
  review cycles, reviews) and all four resources always exist, even when a
  kind is switched off. cognee only reconciles tables the run still names, so
  a missing table would keep its old documents; an empty one forgets them.
"""

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    NODE_SET_COLUMN_HINT,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

from .client import LatticeAPIError, LatticeClient
from .render import (
    UNKNOWN_USER,
    cycle_content,
    cycle_title,
    feedback_content,
    feedback_title,
    goal_content,
    goal_title,
    reviewee_content,
    reviewee_title,
)

logger = get_logger("lattice_connector")

LATTICE_SOURCE_NAME = "lattice"
DEFAULT_RESOURCE_NAME = "lattice"

GOAL_STATES = ("Draft", "Active", "Ended", "Archived")
DEFAULT_GOAL_STATES = ("Active", "Ended")


@dataclass(frozen=True)
class _Selection:
    """What to sync, normalized from the arguments of ``lattice_source``."""

    include_goals: bool
    goal_states: tuple[str, ...]
    include_goal_updates: bool
    include_private_goals: bool
    include_feedback: bool
    feedback_only_public: bool
    include_review_cycles: bool
    review_cycle_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def lattice_source(
    api_key: str | None = None,
    *,
    region: str | None = None,
    include_goals: bool = True,
    goal_states: Sequence[str] = DEFAULT_GOAL_STATES,
    include_goal_updates: bool = True,
    include_private_goals: bool = False,
    include_feedback: bool = False,
    feedback_only_public: bool = True,
    include_review_cycles: bool = True,
    review_cycle_ids: Sequence[str] | None = None,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    client: Any = None,
    check_active: Callable[[], None] | None = None,
):
    """Create a dlt source that yields Lattice goals, feedback and reviews as documents.

    Args:
        api_key: A Lattice API key. Falls back to ``LATTICE_API_KEY``. It carries
            the privileges of its creator, so make it from an account that sees
            only what should end up in memory.
        region: Data residency of the workspace, ``"us"`` (default) or
            ``"emea"``. Falls back to ``LATTICE_REGION``.
        include_goals: Sync goals and key results.
        goal_states: Which goals to sync, by state: ``Draft``, ``Active``,
            ``Ended``, ``Archived``. Defaults to ``Active`` and ``Ended``.
        include_goal_updates: Add each goal's progress updates (status and
            comment) to its document. Costs one request per goal.
        include_private_goals: Also sync goals Lattice marks private. Off by default.
        include_feedback: Sync continuous feedback. **Off by default**: it is
            the most sensitive data in Lattice.
        feedback_only_public: Only sync feedback shared with the whole company
            (``onlyPublic``). Set to ``False`` to include private feedback the
            key's owner can see.
        include_review_cycles: Sync one document per launched review cycle (name,
            stage and dates, no responses).
        review_cycle_ids: Cycle ids whose submitted review *responses* to sync,
            one document per reviewee. **Off by default.** List the ids with
            ``LatticeClient().list_all("/v1/reviewCycles")`` (``examples/example.py``
            prints them); the error for an unknown id lists the ones that exist.
        resource_name: Stable name prefix for this workspace's staging tables. Give
            each Lattice workspace its own name when several sync into one dataset.
        client: A ``LatticeClient`` (mainly a test-injection point); when omitted
            one is built from the key and region above.
        check_active: Called before every request and between rows; raises to stop a
            sync whose access was revoked mid-run (for hosts).

    Returns:
        A dlt source for ``cognee.remember(...)`` / ``cognee.add(...)``. It needs
        the default ``write_disposition="replace"``; any other is refused because
        deletions are only visible as documents missing from the snapshot. After a
        run, ``source.cognee_sync_stats`` holds counts only, never content.
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Lattice connector requires a cognee build with table-scoped document "
            "cleanup and per-row node sets (DOCUMENT_SYNC_VERSION 2, cognee 1.6.4)."
        )
    if not re.fullmatch(r"[a-z][a-z0-9_]*", resource_name):
        raise ValueError("resource_name must be lower case letters, digits and underscores.")
    if isinstance(goal_states, str):
        goal_states = [goal_states]
    if isinstance(review_cycle_ids, str):
        review_cycle_ids = [review_cycle_ids]
    states = tuple(dict.fromkeys(goal_states))
    unknown = [state for state in states if state not in GOAL_STATES]
    if unknown or not states:
        raise ValueError(f"goal_states must be a non-empty subset of {GOAL_STATES}, got {unknown}.")
    if review_cycle_ids is not None and not review_cycle_ids:
        # An empty list would silently forget every review synced before.
        raise ValueError("review_cycle_ids is empty; omit it to sync no review responses.")
    selection = _Selection(
        include_goals=include_goals,
        goal_states=states,
        include_goal_updates=include_goal_updates,
        include_private_goals=include_private_goals,
        include_feedback=include_feedback,
        feedback_only_public=feedback_only_public,
        include_review_cycles=include_review_cycles,
        review_cycle_ids=tuple(dict.fromkeys(review_cycle_ids or ())),
    )
    if not (
        selection.include_goals
        or selection.include_feedback
        or selection.include_review_cycles
        or selection.review_cycle_ids
    ):
        # Nothing selected would forget everything synced before.
        raise ValueError("Nothing is selected to sync.")

    if client is None:
        client = LatticeClient(api_key, region=region)
    if check_active is not None:
        client.check_active = check_active
    tables = _Tables(resource_name)
    stats: dict[str, int] = {}

    def resource(table: str, rows):
        @dlt.resource(
            name=table,
            primary_key="id",
            write_disposition="replace",
            columns=NODE_SET_COLUMN_HINT,
        )
        def staged():
            # The caller's write_disposition overrides the one declared above. A
            # merge run would keep every row that dropped out of the snapshot, so
            # nothing would ever be forgotten: refuse it before reading anything.
            disposition = source.resources[table].write_disposition
            if disposition != "replace":
                raise ValueError(
                    f'lattice_source needs write_disposition="replace", got {disposition!r}. '
                    "It lists everything on each run and deletions are the rows that are gone."
                )
            try:
                yield from guarded_rows(rows(client, selection, tables, stats), check_active)
            finally:
                # Reopened by the next kind's first request.
                client.close()

        return staged

    @dlt.source(name=LATTICE_SOURCE_NAME)
    def _lattice():
        return (
            resource(tables.goals, _goal_rows),
            resource(tables.feedback, _feedback_rows),
            resource(tables.cycles, _cycle_rows),
            resource(tables.reviews, _review_rows),
        )

    source = _lattice()
    # Opt into the document path (record -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, LATTICE_SOURCE_NAME)
    # Give the source its own dlt pipeline per dataset, so a second workspace
    # synced into the same dataset keeps its own state.
    setattr(source, PIPELINE_SCOPE_ATTR, resource_name)
    # Counts of the last run, for hosts that record how a sync went. Never content.
    source.cognee_sync_stats = stats
    return source


@dataclass(frozen=True)
class _Tables:
    """Names of the staging tables and ids, all derived from ``resource_name``."""

    name: str

    @property
    def goals(self) -> str:
        return f"{self.name}_goals"

    @property
    def feedback(self) -> str:
        return f"{self.name}_feedback"

    @property
    def cycles(self) -> str:
        return f"{self.name}_review_cycles"

    @property
    def reviews(self) -> str:
        return f"{self.name}_reviews"

    def row_id(self, kind: str, *parts: str) -> str:
        return ":".join((self.name, kind, *parts))


# ---------------------------------------------------------------------------
# Row builders: each reads one kind completely, or raises. Nothing is yielded
# from a listing that was cut short.
# ---------------------------------------------------------------------------
def _goal_rows(client, selection: _Selection, tables: _Tables, stats: dict) -> Iterator[dict]:
    if not selection.include_goals:
        return
    names = _user_names(client)
    goals: dict[str, dict] = {}
    skipped = 0
    for state in selection.goal_states:
        for goal in client.list_all("/v1/goals", {"state": state}):
            goal_id = goal.get("id")
            if (
                not goal_id
                or goal_id in goals
                # Defense in depth: never trust the server to have applied a filter.
                or goal.get("state") not in selection.goal_states
                or (goal.get("isPrivate") and not selection.include_private_goals)
            ):
                skipped += 1
                continue
            goals[goal_id] = goal

    # A parent is only named when it is in scope itself, so a skipped private goal
    # cannot leak its name through its children.
    titles = {goal_id: goal_title(goal) for goal_id, goal in goals.items()}
    count = 0
    for goal_id, goal in goals.items():
        updates: list[dict] = []
        if selection.include_goal_updates:
            try:
                updates = client.list_all(f"/v1/goals/{goal_id}/updates")
            except LatticeAPIError as exc:
                if exc.status not in (403, 404):
                    raise
                if _goal_is_gone(client, goal_id):
                    # Deleted since it was listed: leave it out so it is forgotten.
                    logger.warning("Lattice: goal %s is gone, skipping.", goal_id)
                    continue
                # Still there, only its updates are unreadable. Keep the goal: an
                # unreadable endpoint must never be read as a deletion.
                logger.warning("Lattice: updates of goal %s are unreadable: %s", goal_id, exc)
        owners = [_name(names, ref) for ref in _refs(goal.get("owners"))]
        parent = (goal.get("parentGoal") or {}).get("id")
        count += 1
        yield {
            "id": tables.row_id("goal", goal_id),
            "title": titles[goal_id],
            "url": goal.get("url"),
            "content": goal_content(goal, owners, titles.get(parent), updates),
            NODE_SET_COLUMN: ["goals"],
        }
    stats.update(goals=count, goals_skipped=skipped)
    logger.info("Lattice: synced %d goal(s), skipped %d.", count, skipped)


def _feedback_rows(client, selection: _Selection, tables: _Tables, stats: dict) -> Iterator[dict]:
    if not selection.include_feedback:
        return
    names = _user_names(client)
    params = {"onlyPublic": "true"} if selection.feedback_only_public else None
    seen: set[str] = set()
    count = 0
    for feedback in client.list_all("/v1/feedbacks", params):
        feedback_id = feedback.get("id")
        if (
            not feedback_id
            or feedback_id in seen
            # Defense in depth: never trust the server to have applied onlyPublic.
            or (selection.feedback_only_public and not feedback.get("isPublic"))
        ):
            continue
        seen.add(feedback_id)
        author = _name(names, feedback.get("author"))
        targets = [_name(names, ref) for ref in _refs(feedback.get("targets"))]
        count += 1
        yield {
            "id": tables.row_id("feedback", feedback_id),
            "title": feedback_title(author, targets),
            "url": feedback.get("url"),
            "content": feedback_content(feedback),
            NODE_SET_COLUMN: ["feedback"],
        }
    stats.update(feedback=count)
    logger.info("Lattice: synced %d feedback item(s).", count)


def _cycle_rows(client, selection: _Selection, tables: _Tables, stats: dict) -> Iterator[dict]:
    if not selection.include_review_cycles:
        return
    count = 0
    for cycle in _launched_cycles(client).values():
        count += 1
        yield {
            "id": tables.row_id("cycle", cycle["id"]),
            "title": cycle_title(cycle),
            "url": cycle.get("url"),
            "content": cycle_content(cycle),
            NODE_SET_COLUMN: ["review_cycles"],
        }
    stats.update(review_cycles=count)
    logger.info("Lattice: synced %d review cycle(s).", count)


def _review_rows(client, selection: _Selection, tables: _Tables, stats: dict) -> Iterator[dict]:
    if not selection.review_cycle_ids:
        return
    cycles = {cycle["id"]: cycle for cycle in _all_cycles(client)}
    missing = [cycle_id for cycle_id in selection.review_cycle_ids if cycle_id not in cycles]
    if missing:
        # Skipping it would forget what it synced before; make the user decide.
        available = {cycle_id: cycle.get("name") for cycle_id, cycle in cycles.items()}
        raise ValueError(
            f"Lattice review cycle(s) {missing} not found. Available: {available}. "
            "Remove an id from `review_cycle_ids` to forget its reviews."
        )
    names = _user_names(client)
    questions: dict[str, dict] = {}
    count = 0
    for cycle_id in selection.review_cycle_ids:
        cycle = cycles[cycle_id]
        reviewee_users = {
            reviewee["id"]: (reviewee.get("user") or {}).get("id")
            for reviewee in client.list_all(f"/v1/reviewCycle/{cycle_id}/reviewees")
            if reviewee.get("id")
        }
        by_reviewee: dict[str, dict[str, dict]] = {}
        for review in client.list_all(f"/v1/reviewCycle/{cycle_id}/reviews"):
            reviewee_id = (review.get("reviewee") or {}).get("id")
            # Only what a reviewer finished and the key's owner may read: a draft
            # answer, a declined request or a hidden response is not a review.
            if (
                not reviewee_id
                or not review.get("id")
                or not review.get("submittedAt")
                or review.get("declinedAt")
                or not (review.get("response") or review.get("calibratedResponse"))
            ):
                continue
            by_reviewee.setdefault(reviewee_id, {})[review["id"]] = review
        for reviewee_id, reviews in by_reviewee.items():
            for review in reviews.values():
                _read_question(client, questions, (review.get("questionRevision") or {}).get("id"))
            person = names.get(reviewee_users.get(reviewee_id) or "", UNKNOWN_USER)
            count += 1
            yield {
                "id": tables.row_id("review", cycle_id, reviewee_id),
                "title": reviewee_title(cycle.get("name") or cycle_id, person),
                "url": cycle.get("url"),
                "content": reviewee_content(reviews.values(), questions),
                NODE_SET_COLUMN: ["reviews"],
            }
    stats.update(reviewees=count)
    logger.info("Lattice: synced reviews of %d reviewee(s).", count)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def _user_names(client) -> dict[str, str]:
    """User id -> name, for every user including deactivated ones."""
    try:
        users = client.list_all("/v1/users", {"status": "NULL"})
    except LatticeAPIError as exc:
        if exc.status not in (400, 422):
            raise
        # Lattice documents status NULL as "every user"; if this workspace refuses it,
        # fall back to the default (active users) so a deactivated owner is the only
        # name lost.
        logger.warning("Lattice: users with any status were refused (%s); active only.", exc)
        users = client.list_all("/v1/users")
    names = {
        user["id"]: user.get("preferredName") or user.get("name") or UNKNOWN_USER
        for user in users
        if user.get("id")
    }
    if not names:
        # The key's own user always exists, so an empty list means the key sees
        # nothing (wrong region or permissions); every listing would read as empty.
        raise LatticeAPIError(
            200,
            "Lattice returned no users for this API key, so nothing it lists can be trusted. "
            "Check the key's permissions and the region.",
        )
    return names


def _all_cycles(client) -> list[dict]:
    return [cycle for cycle in client.list_all("/v1/reviewCycles") if cycle.get("id")]


def _launched_cycles(client) -> dict[str, dict]:
    """Review cycles that have left draft, by id."""
    return {cycle["id"]: cycle for cycle in _all_cycles(client) if cycle.get("stage") != "Draft"}


def _read_question(client, questions: dict[str, dict], revision_id: str | None) -> None:
    if revision_id and revision_id not in questions:
        try:
            questions[revision_id] = client.get(f"/v1/questionRevision/{revision_id}")
        except LatticeAPIError as exc:
            if exc.status not in (403, 404):
                raise
            # Only the wording is lost (the document falls back to the question id);
            # one unreadable question must not stop every run.
            logger.warning("Lattice: question %s is unreadable: %s", revision_id, exc)
            questions[revision_id] = {}


def _goal_is_gone(client, goal_id: str) -> bool:
    """True only when Lattice itself says the goal does not exist."""
    try:
        client.get(f"/v1/goal/{goal_id}")
    except LatticeAPIError as exc:
        return exc.status == 404
    return False


def _refs(listing: Any) -> list[dict]:
    """The references of a Lattice list object (``{object, hasMore, data}``)."""
    if isinstance(listing, dict):
        return [ref for ref in listing.get("data") or [] if isinstance(ref, dict)]
    return []


def _name(names: dict[str, str], ref: Any) -> str:
    return names.get((ref or {}).get("id"), UNKNOWN_USER)
