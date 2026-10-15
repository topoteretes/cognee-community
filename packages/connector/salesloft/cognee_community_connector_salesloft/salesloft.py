"""DLT source for Salesloft (incremental sync + forget-on-delete).

Fetches Salesloft notes, calls, and cadences, renders them to readable
documents, and yields them as a dlt resource for cognee's ingestion pipeline.

Design
------
* **Auth** — OAuth 2.0 authorization-code flow with rotating refresh tokens.
  A pluggable TokenStore (env, file, or callback) holds tokens. The refresh
  token is persisted before the access token is used, so rotated secrets
  never go into plaintext dlt state.
* **Primary key** — deterministic ``salesloft:{type}:{id}``. Combined with
  ``write_disposition="merge"`` this gives idempotent upserts.
* **Incremental cursor** — ``updated_at`` with ASC sort. Per-resource
  checkpoints, so a failure in calls doesn't roll back notes.
* **Signal filter** — configurable ``SignalPolicy`` drops noise (empty calls,
  short dials without notes). The filter is explicit and unit-tested.
* **Forget-on-delete** — reconciliation-based: previously synced IDs are
  re-checked against the API; missing records are emitted with the
  ``_deleted`` hard-delete marker. Safety guards prevent mass deletion from
  partial API responses.
* **Rate limits** — Salesloft's rate-limit headers are read and honored,
  with exponential backoff + jitter and safe Retry-After parsing.

Documents are ingested as **normal documents** (through cognify entity
extraction, not the dlt-row path) via cognee's DOCUMENT_SOURCE_ATTR marker.
"""

from __future__ import annotations

import os
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_salesloft.auth import (
    EnvTokenStore,
    FileTokenStore,
    TokenStore,
    get_valid_access_token,
    verify_credentials,
)
from cognee_community_connector_salesloft.client import SalesloftClient
from cognee_community_connector_salesloft.policy import DEFAULT_POLICY, SignalPolicy
from cognee_community_connector_salesloft.reconcile import (
    reconcile_deletions,
    track_known_ids,
)
from cognee_community_connector_salesloft.render import render_cadence, render_call, render_note

logger = get_logger("salesloft_connector")

# dlt resource / staging-table name.
SALESLOFT_TABLE_NAME = "salesloft_records"
SALESLOFT_SOURCE_NAME = "salesloft"

_EXTRA_HINT = (
    "The Salesloft connector requires httpx: "
    'pip install "cognee-community-connector-salesloft" '
    "(provides dlt and httpx)."
)

# Supported resource types and their API paths.
_RESOURCE_ENDPOINTS: dict[str, str] = {
    "notes": "/v2/notes",
    "calls": "/v2/activities/calls",
    "cadences": "/v2/cadences",
}


def salesloft_source(
    *,
    access_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    token_store: TokenStore | None = None,
    resources: list[str] | None = None,
    cadence_ids: list[int] | None = None,
    since: str | None = None,
    policy: SignalPolicy | None = None,
    max_delete_ratio: float = 0.5,
    dry_run: bool = False,
    _client: SalesloftClient | None = None,
):
    """Create a dlt source that yields Salesloft records as documents.

    Args:
        access_token: Salesloft API access token. Falls back to
            ``SALESLOFT_ACCESS_TOKEN`` or the token store.
        client_id: OAuth client ID (for token refresh). Falls back to
            ``SALESLOFT_CLIENT_ID``.
        client_secret: OAuth client secret (for token refresh). Falls back to
            ``SALESLOFT_CLIENT_SECRET``.
        token_store: Pluggable token storage. Defaults to ``EnvTokenStore``
            if ``SALESLOFT_ACCESS_TOKEN`` is set, else ``FileTokenStore``.
        resources: Which resource types to ingest. Defaults to
            ``["notes", "calls", "cadences"]``.
        cadence_ids: Restrict to these cadence IDs (for scoped ingestion).
        since: ISO 8601 timestamp for the initial sync start (e.g. to avoid
            backfilling years of history). Subsequent syncs use the persisted
            cursor.
        policy: Signal filter policy. Defaults to ``DEFAULT_POLICY``.
        max_delete_ratio: Safety guard — abort reconciliation if this fraction
            of known IDs would be deleted. Set to 1.0 to disable.
        dry_run: If True, print what would be ingested and removed without
            changing memory.
        _client: Pre-built ``SalesloftClient`` (test injection point).

    Returns:
        A dlt source suitable for ``cognee.remember(...)`` / ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    # Resolve resources.
    selected_resources = resources or ["notes", "calls", "cadences"]
    for r in selected_resources:
        if r not in _RESOURCE_ENDPOINTS:
            raise ValueError(
                f"Unknown resource '{r}'. Supported: {list(_RESOURCE_ENDPOINTS.keys())}"
            )

    # Resolve policy.
    signal_policy = policy or DEFAULT_POLICY

    # Resolve access token.
    def _build_client() -> SalesloftClient:
        if _client is not None:
            return _client

        resolved_token = access_token or os.environ.get("SALESLOFT_ACCESS_TOKEN")
        if not resolved_token:
            # Try token store refresh.
            cid = client_id or os.environ.get("SALESLOFT_CLIENT_ID")
            csecret = client_secret or os.environ.get("SALESLOFT_CLIENT_SECRET")
            store = token_store
            if store is None:
                if os.environ.get("SALESLOFT_ACCESS_TOKEN"):
                    store = EnvTokenStore()
                else:
                    store = FileTokenStore()

            if cid and csecret:
                resolved_token = get_valid_access_token(cid, csecret, store)
            else:
                stored = store.load() if store else None
                if stored:
                    resolved_token = stored.get("access_token")
                if not resolved_token:
                    raise ValueError(
                        "Salesloft access token required: pass access_token=, set "
                        "SALESLOFT_ACCESS_TOKEN, or configure OAuth credentials "
                        "(SALESLOFT_CLIENT_ID + SALESLOFT_CLIENT_SECRET)."
                    )

        # Fail-fast credential check.
        verify_credentials(resolved_token)
        return SalesloftClient(resolved_token)

    @dlt.resource(
        name=SALESLOFT_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def salesloft_records():
        client = _build_client()
        try:
            resource_state = dlt.current.resource_state()

            for resource_type in selected_resources:
                # Map plural config name to singular for internal use.
                singular = resource_type.rstrip("s")
                endpoint = _RESOURCE_ENDPOINTS[resource_type]

                # Resolve cursor: use persisted cursor, else the `since` arg.
                cursor_key = f"cursor_{resource_type}"
                cursor = resource_state.get(cursor_key) or since
                latest_updated_at = cursor

                count = 0
                record_ids: list[int | str] = []

                for record in client.paginate(endpoint, updated_after=cursor):
                    # Apply signal policy.
                    if singular == "call" and not signal_policy.accept_call(record):
                        continue
                    if singular == "cadence" and not signal_policy.accept_cadence(record):
                        continue
                    if singular == "note" and not signal_policy.accept_note(record):
                        continue

                    # Apply cadence scope filter.
                    if cadence_ids and singular in ("note", "call"):
                        record_cadence = record.get("cadence") or {}
                        record_cadence_id = record_cadence.get("id")
                        if record_cadence_id and record_cadence_id not in cadence_ids:
                            continue

                    # Render to document row.
                    if singular == "note":
                        row = render_note(record)
                    elif singular == "call":
                        row = render_call(record)
                    elif singular == "cadence":
                        row = render_cadence(record)
                    else:
                        continue  # pragma: no cover

                    if dry_run:
                        logger.info("Salesloft [dry_run]: would ingest %s", row["id"])
                        continue

                    count += 1
                    record_ids.append(record.get("id"))
                    yield row

                    # Track cursor (keyset: latest updated_at).
                    record_updated = record.get("updated_at", "")
                    if record_updated and (
                        not latest_updated_at or record_updated > latest_updated_at
                    ):
                        latest_updated_at = record_updated

                # Advance cursor only after the full resource succeeds.
                if latest_updated_at and not dry_run:
                    resource_state[cursor_key] = latest_updated_at

                # Track known IDs for reconciliation.
                if record_ids and not dry_run:
                    track_known_ids(resource_state, singular, record_ids)

                logger.info(
                    "Salesloft: synced %d %s record(s).",
                    count,
                    resource_type,
                )

            # Reconcile deletions.
            singular_resources = [r.rstrip("s") for r in selected_resources]
            tombstones = reconcile_deletions(
                client,
                resource_state,
                resources=singular_resources,
                max_delete_ratio=max_delete_ratio,
                dry_run=dry_run,
            )
            yield from tombstones

            if tombstones:
                logger.info(
                    "Salesloft: emitted %d deletion tombstone(s).",
                    len(tombstones),
                )
        finally:
            if _client is None:
                client.close()

    @dlt.source(name=SALESLOFT_SOURCE_NAME)
    def _salesloft():
        return salesloft_records

    source = _salesloft()
    # Opt into the document ingestion path (record → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, SALESLOFT_SOURCE_NAME)
    return source


def list_cadences(
    access_token: str | None = None,
    _client: SalesloftClient | None = None,
) -> list[dict[str, Any]]:
    """List available cadences (helper for resource selection).

    Returns a list of dicts with ``id``, ``name``, ``archived_at``, and
    ``owner`` for each cadence.
    """
    token = access_token or os.environ.get("SALESLOFT_ACCESS_TOKEN")
    if not token and _client is None:
        raise ValueError(
            "Access token required: pass access_token= or set SALESLOFT_ACCESS_TOKEN."
        )

    client = _client or SalesloftClient(token)
    try:
        cadences = []
        for record in client.paginate("/v2/cadences"):
            cadences.append(
                {
                    "id": record.get("id"),
                    "name": record.get("name"),
                    "archived_at": record.get("archived_at"),
                    "owner": (record.get("owner") or {}).get("name"),
                }
            )
        return cadences
    finally:
        if _client is None:
            client.close()
