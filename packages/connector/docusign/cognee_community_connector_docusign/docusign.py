"""DLT source for DocuSign envelopes (incremental + forget-on-delete).

Reads agreements from the DocuSign eSignature REST API and yields one document
per envelope for cognee's ingestion pipeline::

    import cognee
    from cognee_community_connector_docusign import docusign_source

    await cognee.remember(
        docusign_source(),  # DOCUSIGN_* credentials from the environment
        dataset_name="docusign",
        write_disposition="merge",  # incremental (see .. important:: below)
    )

.. important::
   Pass ``write_disposition="merge"`` to sync incrementally. The add pipeline
   defaults to ``"replace"``, under which dlt resets the resource state every
   run: each run then lists every envelope in full. That is still correct
   (envelopes that are gone drop out of the snapshot and cognee's
   ``orphan_cleanup`` forgets them), just not incremental.

Design
------
* **Auth**: OAuth JWT grant, or a plain access token (see ``auth.py``). The API
  host comes from ``/oauth/userinfo``, never a hardcoded ``demo.docusign.net``.
  Only ``GET`` requests are issued. The ``signature`` scope DocuSign hands out
  is not itself read-only, which is why the README recommends a dedicated API
  user with a limited permission profile.
* **Documents**: one row per envelope, ``envelope:<envelopeId>``. The text is a
  short header written as plain sentences (subject, sender, dates, each signer
  with their role and signing date, custom fields, what signers typed into
  form fields) followed by the text layer of ``documents/combined``. Signers
  and dates sit next to the contract text so cognify links them in the graph.
  The source declares ``cognee_document_source = "docusign"``, so each row
  flows through the normal cognify entity-extraction pipeline.
* **Scope**: envelopes with one of ``statuses`` (default ``completed``),
  optionally inside ``folder_ids``. Changing any scope setting makes the next
  run a full re-list that forgets whatever fell out of scope.
* **Incremental cursor**: ``GET /envelopes?from_date=<cursor>&from_to_status=changed``
  lists the envelopes whose status changed since the cursor; the cursor is the
  newest ``statusChangedDateTime`` seen, minus a small overlap. One listing
  page already carries recipients, custom fields and folders, so unchanged
  envelopes cost no extra request.
* **Forget-on-delete**: DocuSign envelopes are not deleted. A *completed*
  envelope cannot be voided; "removed" means it moved to the Deleted folder
  (``recyclebin``) or was purged, and neither of those changes
  ``statusChangedDateTime``, so a ``from_date`` listing never sees them. Each
  run therefore does two checks:

  1. envelopes the listing returns that are voided, deleted, in the recycle
     bin or in no folder, purged, unreadable, or no longer in scope become
     tombstones;
  2. every envelope id the previous runs ingested but this listing did not
     return is looked up again by id (``envelope_ids=...`` with folders and
     purge information) and tombstoned when it is gone.

  The ``_deleted`` hard-delete column makes dlt remove the row on ``merge``,
  and cognee's ``orphan_cleanup`` then forgets it from the graph and vectors.
* **Failure safety**: errors (401, exhausted 5xx retries, network, a body that
  is not a JSON object) are not swallowed. They abort the run before dlt
  commits the new cursor, so nothing is deleted and the next run retries from
  the same point. A request budget or a long 429 instead stops the run cleanly:
  progress is kept, nothing is forgotten, and the next run resumes at the cursor.
"""

import io
import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, NODE_SET_COLUMN
from pypdf import PdfReader

from cognee_community_connector_docusign.auth import (
    ACCOUNT_ID_ENV,
    BASE_URI_ENV,
    DocuSignAuth,
    resolve_account,
    resolve_credentials,
)
from cognee_community_connector_docusign.client import (
    DocuSignAPIError,
    DocuSignClient,
    DocuSignRateLimitError,
)

logger = get_logger("docusign_connector")

# dlt resource / staging-table name for DocuSign envelopes.
DOCUSIGN_TABLE_NAME = "docusign_envelopes"
DOCUSIGN_SOURCE_NAME = "docusign"

_PAGE_SIZE = 100
# Envelope ids per lookup request: keeps the URL far below common length limits.
_RECHECK_BATCH = 50
# Re-read envelopes this far behind the stored cursor, for clock skew and
# eventual consistency. Unchanged ones are skipped without a document fetch.
_CURSOR_OVERLAP = timedelta(minutes=5)
_DEFAULT_FROM_DATE = "2000-01-01T00:00:00Z"
_MAX_FIELD_CHARS = 500

# Statuses that always mean "gone"; they are watched but never ingested.
_REMOVED_STATUSES = frozenset({"voided", "deleted"})
# Statuses that are never an agreement to read.
_REJECTED_STATUSES = _REMOVED_STATUSES | {"created", "template"}
_UNPURGED = frozenset({"", "unpurged"})
# DocuSign empties the Deleted folder after a while. The envelope then sits in no
# folder, and its documents answer HTTP 400 with this code: the API user can no
# longer read it (seen live).
_UNREADABLE_CODES = frozenset({"USER_NOT_ENVELOPE_SENDER_OR_RECIPIENT"})
# Recipient categories shown as "received a copy".
_COPY_CATEGORIES = ("carbonCopies", "certifiedDeliveries", "agents", "editors", "intermediaries")
_SIGNER_CATEGORIES = ("signers", "inPersonSigners")

_STAT_KEYS = (
    "full_sync",
    "listed",
    "ingested",
    "unchanged",
    "forgotten",
    "rechecked",
    "pdf_no_text",
    "pdf_unreadable",
    "failed",
    "failed_rate_limit",
    "failed_budget",
)

# Folder names the list endpoint accepts besides folder GUIDs. They are virtual
# or system folders, so ``GET /folders`` does not always return them.
_SYSTEM_FOLDERS = frozenset(
    {
        "awaiting_my_signature",
        "completed",
        "draft",
        "drafts",
        "expiring_soon",
        "inbox",
        "out_for_signature",
        "recyclebin",
        "sentitems",
        "waiting_for_others",
    }
)
_RESOURCE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_FRACTION_RE = re.compile(r"(\.\d{6})\d+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES_RE = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Scope:
    """Which envelopes are in scope, and how they are rendered.

    Everything here changes either the set of ingested envelopes or their text,
    so it is hashed into the state: a different scope forces a full re-list.
    """

    statuses: tuple[str, ...]
    folder_ids: tuple[str, ...]
    from_date: str
    include_certificates: bool
    include_form_data: bool
    node_set_field: str | None

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(
            [
                self.statuses,
                self.folder_ids,
                self.from_date,
                self.include_certificates,
                self.include_form_data,
                self.node_set_field,
            ]
        )
        return sha256(blob.encode()).hexdigest()[:16]


def docusign_source(
    integration_key: str | None = None,
    user_id: str | None = None,
    private_key: str | None = None,
    *,
    private_key_path: str | None = None,
    access_token: str | None = None,
    account_id: str | None = None,
    base_uri: str | None = None,
    environment: str | None = None,
    statuses: list[str] | tuple[str, ...] = ("completed",),
    folder_ids: list[str] | None = None,
    from_date: str | None = None,
    node_set_field: str | None = None,
    include_certificates: bool = False,
    include_form_data: bool = True,
    max_requests: int | None = None,
    resource_name: str = DOCUSIGN_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Create a dlt source that yields DocuSign envelopes as documents.

    Credentials come from the arguments or the ``DOCUSIGN_*`` variables. Give
    either an ``access_token`` or the JWT triple ``integration_key`` +
    ``user_id`` + a private key (``private_key`` PEM text or ``private_key_path``).

    Args:
        account_id: Account to read. Defaults to the user's default account
            (``DOCUSIGN_ACCOUNT_ID``).
        base_uri: API host, e.g. ``https://na3.docusign.net``. Normally
            discovered from ``/oauth/userinfo`` (``DOCUSIGN_BASE_URI``).
        environment: ``"demo"`` (default, developer sandbox) or ``"production"``
            (``DOCUSIGN_ENVIRONMENT``).
        statuses: Envelope statuses to ingest. ``voided`` and ``deleted`` are
            not accepted: those envelopes are always forgotten.
        folder_ids: Only ingest envelopes in these folders (ids, names, or
            system folders such as ``sentitems``; names are resolved through
            ``GET /folders``). Each configured folder also becomes a node set.
            An envelope that leaves the folders is forgotten.
        from_date: Ignore envelopes whose status changed before this date.
        node_set_field: Name of an envelope custom field (for example
            ``agreement_type``) whose value becomes the envelope's node set, so
            ``recall`` can be scoped to just NDAs.
        include_certificates: Append the certificate of completion to each PDF.
            Off by default: it holds signer IP addresses and audit data.
        include_form_data: Add what signers typed into form fields to the text.
        max_requests: Stop cleanly after about this many API requests in one
            run, keep what was read, and resume from there next run. Use it to
            stay under DocuSign's hourly API limit when the first sync of a
            large account would not fit. ``None`` (default) means no budget.
            Forget-on-delete only runs on a run that was not cut short.
        resource_name: Stable name of the dlt resource, the staging table and
            the incremental state. Every DocuSign source syncing into one
            dataset needs its own name: two sources sharing one share state, and
            each run would forget the other's envelopes. A run refuses to
            continue when the stored state belongs to a different account.
            Renaming a source starts it over; documents synced under the old
            name are not cleaned up.
        check_active: Optional host authorization checkpoint, called before each
            request and around each row.
        session: ``requests`` session for every call (mainly a test-injection
            point).

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``.
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError("DocuSign sync requires cognee>=1.6.4 (document node sets).")

    credentials = resolve_credentials(
        integration_key,
        user_id,
        private_key,
        private_key_path=private_key_path,
        access_token=access_token,
        environment=environment,
    )
    scope = _build_scope(
        statuses, folder_ids, from_date, include_certificates, include_form_data, node_set_field
    )
    if not _RESOURCE_NAME_RE.fullmatch(resource_name or ""):
        raise ValueError(
            "resource_name must be letters, digits and underscores, not starting with a digit."
        )
    if max_requests is not None and max_requests < 1:
        raise ValueError("max_requests must be a positive number of requests, or None.")
    account_id = account_id or os.environ.get(ACCOUNT_ID_ENV)
    base_uri = base_uri or os.environ.get(BASE_URI_ENV)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def docusign_envelopes():
        http = session or requests.Session()
        auth = DocuSignAuth(credentials, http)
        if account_id and base_uri:
            resolved_account, resolved_uri = account_id, base_uri
        else:
            resolved_account, resolved_uri = resolve_account(
                http, auth, credentials.oauth_host, account_id
            )
        client = DocuSignClient(
            auth, resolved_uri, resolved_account, http, check_active=check_active
        )
        rows = sync_envelopes(
            client,
            scope,
            dlt.current.resource_state(),
            stats,
            max_requests=max_requests,
            owner=resolved_account,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    @dlt.source(name=DOCUSIGN_SOURCE_NAME)
    def _docusign():
        return docusign_envelopes

    source = _docusign()
    # Opt into the document ingestion path (envelope → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, DOCUSIGN_SOURCE_NAME)
    # Cursors and the ledger of ingested envelopes live in a pipeline of their
    # own. Its scope is the resource name, so the same value names the staging
    # table and the state, and two sources never share a cursor.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics: counts only, never subjects or contract text.
    source.cognee_sync_stats = stats
    return source


def _build_scope(
    statuses, folder_ids, from_date, include_certificates, include_form_data, node_set_field
) -> Scope:
    cleaned = tuple(sorted({s.strip().lower() for s in statuses if s and s.strip()}))
    if not cleaned:
        raise ValueError("statuses must name at least one envelope status, e.g. ['completed'].")
    rejected = sorted(set(cleaned) & _REJECTED_STATUSES)
    if rejected:
        raise ValueError(
            f"statuses {rejected} cannot be ingested: voided and deleted envelopes are always "
            "forgotten, and drafts and templates are not agreements."
        )
    start = _parse_time(from_date) if from_date else None
    if from_date and start is None:
        raise ValueError(f"from_date must be an ISO date or datetime, got {from_date!r}.")
    return Scope(
        statuses=cleaned,
        folder_ids=tuple(sorted({f.strip() for f in folder_ids or [] if f and f.strip()})),
        from_date=_format_time(start) if start else _DEFAULT_FROM_DATE,
        include_certificates=include_certificates,
        include_form_data=include_form_data,
        node_set_field=(node_set_field or "").strip() or None,
    )


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def sync_envelopes(
    client: DocuSignClient,
    scope: Scope,
    state: dict,
    stats: dict[str, int] | None = None,
    *,
    max_requests: int | None = None,
    owner: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed envelopes since the last run, plus hard-delete markers.

    ``state`` holds the ``scope`` fingerprint, the ``cursor``, the ``owner``
    account and ``known``, a ledger ``{envelope_id: statusChangedDateTime}`` of
    what has been ingested. They are only written after the run finished, and
    dlt only commits the state when the whole run loads, so a failed run leaves
    them where they were.

    The listing is read oldest change first. When ``max_requests`` is reached,
    or DocuSign asks for a long wait, the run stops cleanly: what was read is
    kept, the cursor stays at the first envelope not yet read, and the next run
    resumes there. A run that is cut short never forgets anything, because its
    listing is incomplete. A run that cannot resume (a scope change over an
    existing ledger) raises instead.
    """
    stats = stats if stats is not None else {}
    stats.clear()
    stats.update(dict.fromkeys(_STAT_KEYS, 0))

    if owner and state.get("owner") not in (None, owner):
        raise RuntimeError(
            "This DocuSign source's stored state belongs to a different account. Give each "
            "source syncing into one dataset its own resource_name; sharing one would make "
            "each run forget the other account's envelopes."
        )

    known: dict[str, str] = dict(state.get("known") or {})
    cursor = _parse_time(state.get("cursor"))
    full = state.get("scope") != scope.fingerprint or cursor is None
    stats["full_sync"] = int(full)
    if full and known:
        logger.info("DocuSign: scope changed or no cursor; re-listing all envelopes.")
    # A first sync, or an incremental one, can stop and resume. A full re-list
    # over an existing ledger cannot: its cleanup needs the whole listing.
    resumable = not (full and known)

    folder_values, folder_tokens = _resolve_folders(client, scope)
    kept: dict[str, str] = {} if full else dict(known)
    listed: set[str] = set()
    newest = None if full else cursor
    from_date = scope.from_date if full else _format_time(cursor - _CURSOR_OVERLAP)
    started_at = client.requests_made
    stopped: str | None = None
    resume_at: datetime | None = None
    current: datetime | None = None

    try:
        for envelope in client.paginate_envelopes(_list_params(scope, from_date, folder_values)):
            envelope_id = envelope.get("envelopeId")
            if not envelope_id:
                logger.warning("DocuSign: skipping a listed envelope that has no id.")
                continue
            changed_raw = envelope.get("statusChangedDateTime") or ""
            current = changed = _parse_time(changed_raw)

            reason = _removal_reason(envelope, scope, folder_tokens)
            unchanged = (
                reason is None
                and not full
                and known.get(envelope_id) == changed_raw
                and changed_raw
            )
            if (
                max_requests
                and resumable
                # at least one envelope per run, so a tiny budget still makes progress
                and stats["ingested"] > 0
                and reason is None
                and not unchanged
                and client.requests_made - started_at + _requests_per_envelope(scope) > max_requests
            ):
                stopped, resume_at = "budget", changed
                break

            listed.add(envelope_id)
            stats["listed"] += 1
            if unchanged:
                stats["unchanged"] += 1
            else:
                row = (
                    None
                    if reason
                    else _envelope_to_row(client, envelope, scope, folder_tokens, stats)
                )
                if row is not None:
                    kept[envelope_id] = changed_raw
                    stats["ingested"] += 1
                    yield row
                elif envelope_id in known:
                    kept.pop(envelope_id, None)
                    stats["forgotten"] += 1
                    yield _deleted_row(envelope_id)
            if changed and (newest is None or changed > newest):
                newest = changed
            current = None
    except DocuSignRateLimitError:
        if not resumable:
            raise
        stopped, resume_at = "rate_limit", current

    if stopped:
        stats["failed"] = 1
        stats[f"failed_{stopped}"] = 1
        logger.warning(
            "DocuSign: stopped early (%s); progress is kept and the next run resumes. "
            "Nothing is forgotten on a run that was cut short.",
            stopped.replace("_", " "),
        )
        newest = resume_at or newest
    elif full:
        unlisted = set(known) - listed
        if unlisted and not listed:
            # An empty listing while envelopes were known almost always means a
            # filter typo or a transient problem, not that everything was
            # removed. Forgetting every agreement on that signal would purge
            # the dataset, so keep them this run.
            logger.warning(
                "DocuSign: the listing returned 0 envelopes but %d were known; skipping "
                "forget-on-delete this run.",
                len(known),
            )
            kept = dict(known)
        else:
            for envelope_id in sorted(unlisted):
                stats["forgotten"] += 1
                yield _deleted_row(envelope_id)
    else:
        yield from _recheck_known(
            client,
            scope,
            folder_tokens,
            sorted(set(kept) - listed),
            kept,
            stats,
            listing_worked=bool(listed),
        )

    state["scope"] = scope.fingerprint
    state["cursor"] = _format_time(newest) if newest else None
    state["known"] = dict(sorted(kept.items()))
    if owner:
        state["owner"] = owner
    logger.info(
        "DocuSign: %d listed, %d ingested, %d unchanged, %d forgotten.",
        stats["listed"],
        stats["ingested"],
        stats["unchanged"],
        stats["forgotten"],
    )


def _requests_per_envelope(scope: Scope) -> int:
    """Requests one new or changed envelope costs: its PDF, plus its form data."""
    return 2 if scope.include_form_data else 1


def _resolve_folders(client: DocuSignClient, scope: Scope) -> tuple[list[str], frozenset[str]]:
    """Turn configured folders into list-endpoint values and match tokens.

    The list endpoint only takes folder GUIDs or a fixed set of system names, and
    answers a custom folder's *name* with HTTP 400. ``GET /folders`` maps names
    (and types) to GUIDs. An unknown folder raises: skipping it would quietly
    forget what an earlier run ingested from it.
    """
    if not scope.folder_ids:
        return [], frozenset()
    available = _flatten_folders(client.get_json("folders").get("folders") or [])
    values: list[str] = []
    tokens: set[str] = set()
    for wanted in scope.folder_ids:
        key = wanted.lower()
        matches = [
            folder
            for folder in available
            if key in {str(folder.get(k, "")).lower() for k in ("folderId", "name", "type")}
        ]
        tokens.add(key)
        if matches:
            for folder in matches:
                values.append(folder["folderId"])
                tokens.add(folder["folderId"].lower())
        elif key in _SYSTEM_FOLDERS:
            values.append(key)
        else:
            names = ", ".join(sorted(str(f.get("name")) for f in available if f.get("name")))
            raise ValueError(
                f"DocuSign folder {wanted!r} was not found. Use a folder id, a folder name "
                f"({names}), or one of: {', '.join(sorted(_SYSTEM_FOLDERS))}."
            )
    return list(dict.fromkeys(values)), frozenset(tokens)


def _flatten_folders(folders: list[dict]) -> list[dict]:
    flat: list[dict] = []
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        flat.append(folder)
        flat.extend(_flatten_folders(folder.get("folders") or []))
    return flat


def _list_params(scope: Scope, from_date: str, folder_values: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "from_date": from_date,
        # Filter on when the status changed, not on when the envelope was created.
        "from_to_status": "changed",
        # Oldest change first, so a run that stops early can resume at its cursor.
        "order_by": "status_changed",
        "order": "asc",
        "include": "recipients,custom_fields,folders",
        "include_purge_information": "true",
        "count": _PAGE_SIZE,
    }
    if folder_values:
        # DocuSign returns nothing at all when a folder filter is combined with a
        # list of statuses (seen live), so the status is checked here instead.
        params["folder_ids"] = ",".join(folder_values)
    else:
        # voided/deleted are listed too, so a tracked envelope that ended up there
        # is seen and forgotten; _removal_reason keeps them out of the dataset.
        params["status"] = ",".join(sorted(set(scope.statuses) | _REMOVED_STATUSES))
    return params


def _recheck_known(
    client: DocuSignClient,
    scope: Scope,
    folder_tokens: frozenset[str],
    candidates: list[str],
    kept: dict[str, str],
    stats: dict[str, int],
    *,
    listing_worked: bool,
) -> Iterator[dict[str, Any]]:
    """Look tracked envelopes the listing did not return up by id, forget the gone ones.

    This is what catches the Deleted folder and purges, which leave the
    status-change date alone. ``kept`` loses every forgotten id.
    """
    if not candidates:
        return
    found: dict[str, dict] = {}
    for start in range(0, len(candidates), _RECHECK_BATCH):
        batch = candidates[start : start + _RECHECK_BATCH]
        params = {
            "envelope_ids": ",".join(batch),
            "include": "folders",
            "include_purge_information": "true",
            "count": _PAGE_SIZE,
        }
        for envelope in client.paginate_envelopes(params):
            if envelope.get("envelopeId"):
                found[envelope["envelopeId"]] = envelope
    stats["rechecked"] += len(candidates)

    if not found and not listing_worked:
        # Nothing came back from the lookup *and* from the listing: that points
        # at the query or the account, not at every agreement having vanished.
        # Keep them and report, rather than wipe the dataset. When the listing
        # did return envelopes, the API is working and absent ids are really gone.
        logger.warning(
            "DocuSign: looking up %d tracked envelope(s) by id returned none; "
            "skipping forget-on-delete for them this run.",
            len(candidates),
        )
        return

    for envelope_id in candidates:
        envelope = found.get(envelope_id)
        if envelope is not None and _removal_reason(envelope, scope, folder_tokens) is None:
            continue
        kept.pop(envelope_id, None)
        stats["forgotten"] += 1
        yield _deleted_row(envelope_id)


def _removal_reason(envelope: dict, scope: Scope, folder_tokens: frozenset[str]) -> str | None:
    """Why an envelope must not be in the dataset, or ``None`` when it belongs."""
    status = (envelope.get("status") or "").lower()
    if status in _REMOVED_STATUSES:
        return f"status {status}"
    if status not in scope.statuses:
        return f"status {status or 'unknown'} is out of scope"
    if (envelope.get("purgeState") or "").lower() not in _UNPURGED:
        return "purged"
    folders = [f for f in envelope.get("folders") or [] if isinstance(f, dict)]
    if "folders" in envelope and not folders:
        return "in no folder (removed from the Deleted folder)"
    if any(_is_recycle_bin(folder) for folder in folders):
        return "in the Deleted folder"
    if folder_tokens and folders and not _matching_folders(folders, folder_tokens):
        return "outside the configured folders"
    return None


def _is_recycle_bin(folder: dict) -> bool:
    return "recyclebin" in {str(folder.get(key, "")).lower() for key in ("type", "folderId")}


def _matching_folders(folders: list[dict], folder_tokens: frozenset[str]) -> list[dict]:
    return [
        folder
        for folder in folders
        if folder_tokens
        & {str(folder.get(key, "")).lower() for key in ("folderId", "name", "type")}
    ]


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _envelope_to_row(
    client: DocuSignClient,
    envelope: dict,
    scope: Scope,
    folder_tokens: frozenset[str],
    stats: dict[str, int],
) -> dict[str, Any] | None:
    """Fetch an envelope's PDF (and form data) and render its document row.

    Returns ``None`` when the envelope vanished between the listing and the
    fetch (404): the caller forgets it, like any other removed envelope.
    """
    envelope_id = envelope["envelopeId"]
    try:
        pdf = client.get_bytes(
            f"envelopes/{envelope_id}/documents/combined",
            {"certificate": "true" if scope.include_certificates else "false"},
        )
        form_data = _fetch_form_data(client, envelope_id) if scope.include_form_data else {}
    except DocuSignAPIError as exc:
        if exc.status == 404 or exc.code in _UNREADABLE_CODES:
            logger.info("DocuSign: an envelope is gone or unreadable for this user; skipping.")
            return None
        raise

    text = _pdf_text(pdf, stats)
    header = _render_header(envelope, form_data)
    row: dict[str, Any] = {
        "id": _row_id(envelope_id),
        "title": (envelope.get("emailSubject") or "").strip() or f"Envelope {envelope_id}",
        "content": f"{header}\n\n{text}" if text else header,
        # Hard-delete marker (always False for live envelopes). Removed
        # envelopes are emitted separately with _deleted=True.
        "_deleted": False,
    }
    node_sets = _node_sets(envelope, scope, folder_tokens)
    if node_sets:
        row[NODE_SET_COLUMN] = node_sets
    return row


def _fetch_form_data(client: DocuSignClient, envelope_id: str) -> dict[str, Any]:
    try:
        return client.get_json(f"envelopes/{envelope_id}/form_data")
    except DocuSignAPIError as exc:
        if exc.status == 404:  # no form fields on this envelope
            return {}
        raise


def _pdf_text(data: bytes, stats: dict[str, int]) -> str:
    """Text layer of a PDF. Scans (no text) and unreadable files are counted, not dropped."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # third-party parser on untrusted bytes: any failure is "unreadable"
        logger.warning("DocuSign: a PDF could not be parsed (%s).", type(exc).__name__)
        stats["pdf_unreadable"] += 1
        return ""
    text = _clean_text("\n\n".join(pages))
    if not text:
        # Typically a scanned document. The header still goes in, so the
        # agreement, its signers and dates are searchable.
        stats["pdf_no_text"] += 1
    return text


def _clean_text(text: str) -> str:
    text = _CONTROL_RE.sub("", text)
    lines = (_SPACES_RE.sub(" ", line).strip() for line in text.splitlines())
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _node_sets(envelope: dict, scope: Scope, folder_tokens: frozenset[str]) -> list[str]:
    """Configured folders the envelope sits in, plus the ``node_set_field`` value."""
    names = [
        str(folder.get("name") or folder.get("folderId"))
        for folder in _matching_folders(
            [f for f in envelope.get("folders") or [] if isinstance(f, dict)], folder_tokens
        )
        if folder_tokens
    ]
    if scope.node_set_field:
        wanted = scope.node_set_field.lower()
        for name, value in _custom_fields(envelope):
            if name.lower() == wanted and value:
                names.append(value)
    return list(dict.fromkeys(name.strip() for name in names if name and name.strip()))


def _custom_fields(envelope: dict) -> list[tuple[str, str]]:
    fields = envelope.get("customFields") or {}
    pairs = []
    for key in ("textCustomFields", "listCustomFields"):
        for field in fields.get(key) or []:
            name, value = str(field.get("name") or "").strip(), str(field.get("value") or "")
            if name and value.strip():
                pairs.append((name, value.strip()[:_MAX_FIELD_CHARS]))
    return pairs


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_header(envelope: dict, form_data: dict) -> str:
    """Describe the envelope as plain sentences.

    Sentences, not ``Label: value`` lines, on purpose: label-style headers were
    extracted as entities of their own by cognify (``Status``, ``Signer``).
    """
    subject = (envelope.get("emailSubject") or "").strip() or f"envelope {envelope['envelopeId']}"
    status = (envelope.get("status") or "unknown").lower()
    sender = envelope.get("sender") or {}
    sender_text = _person(sender.get("userName"), sender.get("email"))

    opening = f'The agreement "{subject}"'
    sent_on = _when(envelope.get("sentDateTime"))
    if sender_text and sent_on:
        opening += f" was sent by {sender_text} on {sent_on}"
    elif sender_text:
        opening += f" was sent by {sender_text}"
    elif sent_on:
        opening += f" was sent on {sent_on}"
    completed_on = _when(envelope.get("completedDateTime"))
    if status == "completed" and completed_on:
        opening += f" and was completed on {completed_on}."
    elif status == "completed":
        opening += " and is completed."
    else:
        opening += f" and its status is {status}."
    lines = [opening]

    recipients = envelope.get("recipients") or {}
    signers = [r for key in _SIGNER_CATEGORIES for r in recipients.get(key) or []]
    for signer in sorted(signers, key=_routing_key):
        lines.append(_signer_sentence(signer))
    for key in _COPY_CATEGORIES:
        for recipient in sorted(recipients.get(key) or [], key=_routing_key):
            who = _person(recipient.get("name"), recipient.get("email"))
            if who:
                role = _role(recipient) or "recipient"
                lines.append(f"{who} received a copy as {role}.")

    for name, value in _custom_fields(envelope):
        lines.append(f"The custom field {name} is {value}.")
    lines.extend(_form_sentences(form_data))
    return "\n".join(lines)


def _signer_sentence(signer: dict) -> str:
    who = _person(signer.get("name") or signer.get("signerName"), signer.get("email")) or "A signer"
    role = _role(signer)
    status = (signer.get("status") or "").lower()
    if status in ("completed", "signed"):
        when = _when(signer.get("signedDateTime"))
        return f"{who} signed" + (f" as {role}" if role else "") + (f" on {when}." if when else ".")
    if status == "declined":
        when = _when(signer.get("declinedDateTime"))
        reason = (signer.get("declinedReason") or "").strip()
        return (
            f"{who} declined to sign"
            + (f" on {when}" if when else "")
            + (f" and gave the reason: {reason[:_MAX_FIELD_CHARS]}" if reason else "")
            + "."
        )
    return f"{who} has not signed yet" + (f" and is {role}" if role else "") + "."


def _form_sentences(form_data: dict) -> list[str]:
    """What signers typed into form fields; often absent from the PDF text layer."""
    sentences = []
    for recipient in form_data.get("recipientFormData") or []:
        who = _person(recipient.get("name"), recipient.get("email")) or "A signer"
        for field in recipient.get("formData") or []:
            sentence = _field_sentence(who, field)
            if sentence:
                sentences.append(sentence)
    if not sentences:
        for field in form_data.get("formData") or []:
            sentence = _field_sentence("A signer", field)
            if sentence:
                sentences.append(sentence)
    return sentences


def _field_sentence(who: str, field: dict) -> str | None:
    name = str(field.get("name") or "").strip()
    value = str(field.get("value") if field.get("value") is not None else "").strip()
    if not name or not value:
        return None
    return f"{who} filled in {name} with {value[:_MAX_FIELD_CHARS]}."


def _person(name: str | None, email: str | None) -> str:
    name, email = (name or "").strip(), (email or "").strip()
    if name and email:
        return f"{name} ({email})"
    return name or email


def _role(recipient: dict) -> str:
    return (recipient.get("roleName") or "").strip()


def _routing_key(recipient: dict) -> tuple[int, str]:
    try:
        order = int(recipient.get("routingOrder") or 0)
    except (TypeError, ValueError):
        order = 0
    return order, str(recipient.get("name") or recipient.get("signerName") or "")


def _when(value: str | None) -> str:
    """``2026-01-05 at 10:00 UTC``; empty when the timestamp is missing."""
    parsed = _parse_time(value)
    return f"{parsed:%Y-%m-%d} at {parsed:%H:%M} UTC" if parsed else ""


# ---------------------------------------------------------------------------
# Ids and timestamps
# ---------------------------------------------------------------------------


def _row_id(envelope_id: str) -> str:
    return f"envelope:{envelope_id}"


def _deleted_row(envelope_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete an envelope by id."""
    return {"id": _row_id(envelope_id), "_deleted": True}


def _parse_time(value: str | None) -> datetime | None:
    """Parse a DocuSign ISO-8601 timestamp (up to 7 fractional digits) to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(_FRACTION_RE.sub(r"\1", value.strip()))
    except ValueError:
        logger.warning("DocuSign: ignoring unparseable timestamp %r.", value)
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
