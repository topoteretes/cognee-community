"""DLT source for Stack Overflow questions + answers (incremental sync + forget-on-delete).

Each question is rendered as a *document* (solution-first Markdown) and
tagged with ``cognee_document_source = "stackoverflow"`` so cognee's
``resolve_dlt_sources`` routes it through the normal cognify
entity-extraction pipeline instead of the dlt-schema path.

Sync strategy
-------------
* **Incremental** — ``sort=activity&order=asc&min=<cursor>`` picks up new
  questions AND edits/new answers on old questions. The cursor advances only
  after each batch loads successfully, so interrupted runs resume safely.
* **Forget-on-delete** — ``reconcile.py`` rechecks tracked IDs in batches of
  100 and emits ``_deleted=True`` tombstones for missing questions.
* **Multi-tag** — one dlt resource per tag, deduplicated by question ID.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterator

logger = logging.getLogger("stack_overflow_connector")

# dlt staging-table / resource name.
SO_TABLE_NAME = "stackoverflow_questions"
SO_SOURCE_NAME = "stackoverflow"

_EXTRA_HINT = (
    "The Stack Overflow connector requires dlt: "
    'pip install "cognee-community-connector-stack-overflow"'
)


def stack_overflow_source(
    api_key: str | None = None,
    tags: list[str] | None = None,
    max_questions_per_tag: int = 500,
    min_score: int = 0,
    min_answers: int = 0,
    max_requests_per_run: int | None = None,
    max_delete_ratio: float = 0.2,
    preview_deletions: bool = False,
    resource_name: str = SO_TABLE_NAME,
    client: Any = None,
):
    """Create a dlt source that yields Stack Overflow Q&A documents.

    Args:
        api_key: Stack Exchange API key. Falls back to ``STACKOVERFLOW_API_KEY``.
        tags: Tags to ingest. Each tag is a separate stream. Default: ``["python"]``.
        max_questions_per_tag: Hard cap on questions fetched per tag per run.
        min_score: Skip questions with score below this threshold.
        min_answers: Skip questions with fewer answers than this (default 0 = all).
        max_requests_per_run: Budget cap. ``None`` = unlimited (use with care).
        max_delete_ratio: Safety guard — abort tombstoning if > this fraction
            of tracked questions would be deleted.
        preview_deletions: Log what would be deleted but do not tombstone.
        resource_name: dlt resource / staging-table name.
        client: Pre-built ``StackOverflowClient`` (test injection point).

    Returns:
        A dlt source suitable for ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
    from cognee_community_connector_stack_overflow.client import StackOverflowClient
    from cognee_community_connector_stack_overflow.render import render_question
    from cognee_community_connector_stack_overflow.reconcile import reconcile_deletions

    if client is None:
        resolved_key = api_key or os.environ.get("STACKOVERFLOW_API_KEY")
        client = StackOverflowClient(
            api_key=resolved_key,
            max_requests_per_run=max_requests_per_run,
        )

    resolved_tags: list[str] = tags or ["python"]

    @dlt.source(name=SO_SOURCE_NAME)
    def _source():
        for tag in resolved_tags:
            yield _questions_resource(
                tag=tag,
                client=client,
                max_questions_per_tag=max_questions_per_tag,
                min_score=min_score,
                min_answers=min_answers,
                max_delete_ratio=max_delete_ratio,
                preview_deletions=preview_deletions,
                resource_name=f"{resource_name}_{tag.replace('-', '_')}",
            )

    return _source()


def _questions_resource(
    tag: str,
    client: Any,
    max_questions_per_tag: int,
    min_score: int,
    min_answers: int,
    max_delete_ratio: float,
    preview_deletions: bool,
    resource_name: str,
):
    """dlt resource for a single tag — incremental + forget-on-delete."""
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
    from cognee_community_connector_stack_overflow.render import render_question
    from cognee_community_connector_stack_overflow.reconcile import reconcile_deletions

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"nullable": True, "data_type": "bool"}},
    )
    def questions(
        last_activity_date=dlt.sources.incremental(
            "last_activity_date", initial_value=None
        ),
    ) -> Iterator[dict[str, Any]]:
        """Yield rendered Q&A documents and deletion tombstones for *tag*."""
        from_date: int | None = last_activity_date.last_value

        tracked_ids: list[int] = list(
            dlt.current.resource_state().get("tracked_ids", [])
        )
        seen_this_run: set[str] = set()
        fetched = 0

        for question in client.get_questions_by_tag(
            tag=tag,
            from_date=from_date,
            min_score=min_score,
        ):
            qid: int = question.get("question_id", 0)
            doc_id = f"stackoverflow:question:{qid}"

            if doc_id in seen_this_run:
                continue  # cross-tag dedupe
            seen_this_run.add(doc_id)

            answers = question.get("answers", [])
            if len(answers) < min_answers:
                continue

            try:
                row = render_question(question)
            except Exception as exc:
                logger.warning("Render error for question %d: %s", qid, exc)
                continue

            # Mark as document so cognee routes it through cognify, not dlt-schema.
            row[DOCUMENT_SOURCE_ATTR] = SO_SOURCE_NAME
            # Store last_activity_date for the incremental cursor.
            row["last_activity_date"] = question.get("last_activity_date", 0)

            if qid not in tracked_ids:
                tracked_ids.append(qid)

            yield row
            fetched += 1
            if fetched >= max_questions_per_tag:
                logger.info(
                    "Reached max_questions_per_tag=%d for tag=%r; stopping.",
                    max_questions_per_tag,
                    tag,
                )
                break

            if not client.budget_ok():
                logger.warning(
                    "Quota/budget exhausted after %d questions for tag=%r; "
                    "cursor NOT advanced past partial data.",
                    fetched,
                    tag,
                )
                return  # cursor won't advance (dlt uses last successfully yielded value)

        # Persist tracked IDs so deletion recheck works on the next run.
        dlt.current.resource_state()["tracked_ids"] = tracked_ids

        # Forget-on-delete: recheck tracked IDs and tombstone missing ones.
        yield from reconcile_deletions(
            client=client,
            tracked_ids=tracked_ids,
            max_delete_ratio=max_delete_ratio,
            preview=preview_deletions,
        )

    # Tag the node-set so scoped recall works:
    #   cognee.search(..., node_name=["stackoverflow:tag:polars"])
    questions.apply_hints(
        additional_table_hints={
            "x-cognee-node-set": [
                f"stackoverflow:tag:{tag}",
                "stackoverflow",
            ]
        }
    )

    return questions