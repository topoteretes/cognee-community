"""Deletion reconciliation for the Stack Overflow connector."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from cognee_community_connector_stack_overflow.client import StackOverflowClient

logger = logging.getLogger("stack_overflow_connector.reconcile")

_DEFAULT_MAX_DELETE_RATIO = 0.2


def reconcile_deletions(
    client,
    tracked_ids: list,
    max_delete_ratio: float = _DEFAULT_MAX_DELETE_RATIO,
    preview: bool = False,
):
    if not tracked_ids:
        return

    seen_ids = set()
    try:
        for q in client.get_questions_by_ids(tracked_ids):
            seen_ids.add(q["question_id"])
    except Exception as exc:
        logger.error("Deletion recheck failed (%s); skipping tombstones.", exc)
        return

    missing = [qid for qid in tracked_ids if qid not in seen_ids]
    if not missing:
        return

    ratio = len(missing) / len(tracked_ids)
    if ratio > max_delete_ratio:
        logger.error(
            "Deletion guard triggered: %d/%d IDs missing — aborting.",
            len(missing), len(tracked_ids),
        )
        return

    for qid in missing:
        doc_id = f"stackoverflow:question:{qid}"
        if preview:
            logger.info("PREVIEW — would delete: %s", doc_id)
            continue
        logger.info("Tombstoning deleted question: %s", doc_id)
        yield {"id": doc_id, "_deleted": True}
