"""Deletion detection by reconciliation for Salesloft.

Salesloft doesn't expose a universal delete feed, so deletes are detected by
reconciling previously synced IDs against the current API state.

Safety guards:
- Scoped inventory: only IDs previously synced are checked
- Two-phase confirmation: only fully completed reconciliation marks deletes
- Delete-ratio guard: configurable max deletion ratio per run
- Auth/rate-limit errors preserve existing state
"""

from __future__ import annotations

from typing import Any

from cognee.shared.logging_utils import get_logger

from cognee_community_connector_salesloft.render import deleted_row

logger = get_logger("salesloft_reconcile")

# Resource type -> API list path
_RESOURCE_PATHS: dict[str, str] = {
    "note": "/v2/notes",
    "call": "/v2/activities/calls",
    "cadence": "/v2/cadences",
}


def reconcile_deletions(
    client: Any,
    state: dict,
    *,
    resources: list[str],
    max_delete_ratio: float = 0.5,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Detect deleted records and return hard-delete marker rows.

    Args:
        client: A ``SalesloftClient`` instance.
        state: The dlt resource state dict (persisted between runs).
        resources: Resource types to reconcile (e.g. ["note", "call"]).
        max_delete_ratio: If the fraction of known IDs to be deleted exceeds
            this, abort and return [] instead of mass-deleting. Set to 1.0
            to disable.
        dry_run: If True, log what would be deleted but return [].

    Returns:
        A list of ``_deleted=True`` rows for dlt to hard-delete.
    """
    tombstones: list[dict[str, Any]] = []

    for resource_type in resources:
        path = _RESOURCE_PATHS.get(resource_type)
        if not path:
            logger.warning("Salesloft: unknown resource type '%s', skipping.", resource_type)
            continue

        known_key = f"known_ids_{resource_type}"
        known_ids: set = set(state.get(known_key, []))
        if not known_ids:
            logger.debug("Salesloft: no known %s IDs to reconcile.", resource_type)
            continue

        try:
            existing_ids = client.check_ids_exist(path, list(known_ids))
        except Exception as exc:
            # Auth error, rate limit, or server error: preserve existing
            # documents — do NOT delete on a failed reconciliation.
            logger.warning(
                "Salesloft: reconciliation failed for %s (%s). "
                "Preserving all known records.",
                resource_type,
                exc,
            )
            continue

        gone_ids = known_ids - existing_ids

        if not gone_ids:
            logger.debug(
                "Salesloft: all %d known %s(s) still exist.",
                len(known_ids),
                resource_type,
            )
            continue

        # Delete-ratio guard.
        delete_ratio = len(gone_ids) / len(known_ids) if known_ids else 0
        if delete_ratio > max_delete_ratio:
            logger.warning(
                "Salesloft: %d/%d %s(s) (%.0f%%) would be deleted — exceeds "
                "max_delete_ratio=%.0f%%. Aborting reconciliation for this "
                "resource to prevent mass deletion.",
                len(gone_ids),
                len(known_ids),
                resource_type,
                delete_ratio * 100,
                max_delete_ratio * 100,
            )
            continue

        if dry_run:
            logger.info(
                "Salesloft [dry_run]: would delete %d %s(s): %s",
                len(gone_ids),
                resource_type,
                sorted(gone_ids),
            )
            continue

        for record_id in gone_ids:
            tombstones.append(deleted_row(record_id, resource_type))

        # Update known IDs to reflect the deletions.
        state[known_key] = sorted(existing_ids)
        logger.info(
            "Salesloft: reconciled %d deleted %s(s).",
            len(gone_ids),
            resource_type,
        )

    return tombstones


def track_known_ids(
    state: dict,
    resource_type: str,
    record_ids: list[int | str],
) -> None:
    """Record synced IDs so reconciliation can check them on the next run."""
    known_key = f"known_ids_{resource_type}"
    existing: set = set(state.get(known_key, []))
    existing.update(record_ids)
    state[known_key] = sorted(existing)
