"""DLT source for Microsoft Teams channel threads (incremental + forget-on-delete).

Reads channel messages and their replies from Microsoft Graph and yields one
markdown-ish document per thread for cognee's ingestion pipeline::

    import cognee
    from cognee_community_connector_teams import teams_source

    await cognee.remember(
        teams_source(team_ids=["<team id>"]),
        dataset_name="teams",
        write_disposition="merge",  # incremental (see .. important:: below)
    )

.. important::
   Pass ``write_disposition="merge"`` to sync incrementally. The add pipeline
   defaults to ``"replace"``, under which dlt resets the resource state every
   run: each run then walks every channel in full and loads a complete
   snapshot. That is still correct (deleted threads drop out of the snapshot
   and cognee's ``orphan_cleanup`` forgets them), just not incremental.

Design
------
* **Auth**: app-only Microsoft Graph via an Entra ID app registration (see
  ``graph.py``, shared with the other Microsoft 365 connectors). Only ``GET``
  requests are issued.
* **Scope**: standard channels of the selected teams (``team_ids``, or every
  team the app can list), optionally narrowed by ``channel_ids``. 1:1 and
  group chats are never read, and private/shared channels are skipped.
* **Documents**: one row per thread, ``thread:<team>:<channel>:<root>``,
  holding the root message and its replies in time order. A reply edit or
  deletion re-renders the whole thread, so the thread is the unit of change
  and short chat lines stay together in context for cognify. The source
  declares ``cognee_document_source = "teams"``, so each thread flows through
  the normal cognify entity-extraction pipeline instead of the relational
  dlt-row path.
* **Incremental cursor**: Graph's channel delta query is gone from v1.0, but
  the free ``GET .../messages?$expand=replies`` list is sorted by the last
  modified time of the *whole reply chain*, newest first. Each run walks a
  channel from the top and stops at the first thread whose chain timestamp is
  older than that channel's stored cursor (minus a small overlap for Graph's
  eventual consistency). An unchanged channel costs one request. Cursors live
  in dlt's per-resource state, one per channel.
* **Forget-on-delete**: ``merge`` with an ``_deleted`` hard-delete column. A
  deleted root (``deletedDateTime`` set) tombstones its thread; a deleted
  reply just re-renders the thread without it. A channel (or team) that is
  deleted or dropped from the selection has every thread it contributed
  tombstoned; archived channels stay listed and keep their threads. dlt
  removes the rows on ``merge``, and cognee's ``orphan_cleanup`` then forgets
  them from the graph and vector stores.
* **Failure safety**: errors (401/403, exhausted 429/5xx retries, network) are
  not swallowed. They abort the run before dlt commits the new cursors, so
  nothing is deleted and the next run retries from the same point.
"""

import html
import re
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.sources.rest_api import rest_api_resources
from dlt.sources.rest_api.config_setup import create_response_hooks

from cognee_community_connector_teams.graph import (
    GRAPH_DATA_SELECTOR,
    graph_client_config,
    graph_credentials,
    graph_rest_client,
)

logger = get_logger("teams_connector")

# dlt resource / staging-table name for Teams threads.
TEAMS_TABLE_NAME = "teams_threads"
TEAMS_SOURCE_NAME = "teams"

# Graph caps channel message pages at 50 roots.
_PAGE_SIZE = 50
# Re-read threads this far behind the stored cursor: Graph is eventually
# consistent, so a change can surface with a slightly older timestamp.
# Re-rendering an unchanged thread is idempotent (same content hash).
_CURSOR_OVERLAP = timedelta(minutes=5)
# Longest title snippet taken from a thread's first message.
_TITLE_SNIPPET_CHARS = 80

# Teams bodies are HTML. Mentions become "@Name", block-level tags become line
# breaks, list items become "- " bullets, and every other tag is dropped.
_MENTION_RE = re.compile(r"<at\b[^>]*>(.*?)</at>", re.IGNORECASE | re.DOTALL)
_LIST_ITEM_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_BREAK_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|h[1-6]|tr|blockquote|pre)>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\u00a0]+")

# A 404 means the channel vanished between the listing and the walk. Skip it;
# the next run's listing no longer contains it and tombstones its threads.
_IGNORE_NOT_FOUND = create_response_hooks([{"status_code": 404, "action": "ignore"}])


def teams_source(
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    *,
    team_ids: list[str] | None = None,
    channel_ids: list[str] | None = None,
    session: Any = None,
):
    """Create a dlt source that yields Teams channel threads as documents.

    Args:
        tenant_id: Directory (tenant) id of the Entra ID app registration.
            Falls back to ``MS_GRAPH_TENANT_ID``.
        client_id: Application (client) id. Falls back to ``MS_GRAPH_CLIENT_ID``.
        client_secret: Client secret value. Falls back to
            ``MS_GRAPH_CLIENT_SECRET``.
        team_ids: Teams to ingest. When omitted, every team the app can list
            is synced (needs ``Team.ReadBasic.All``).
        channel_ids: Restrict ingestion to these channel ids within the
            selected teams. A channel dropped from this list is forgotten on
            the next sync.
        session: ``requests`` session for the token and Graph calls (mainly a
            test-injection point); when omitted dlt's retrying session is used.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``.
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Teams sync requires cognee>=1.6.3 (table-scoped document cleanup).")

    tenant_id, client_id, client_secret = graph_credentials(tenant_id, client_id, client_secret)
    client_config = graph_client_config(tenant_id, client_id, client_secret, session=session)
    selected_channels = set(channel_ids) if channel_ids else None

    @dlt.resource(
        name=TEAMS_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def teams_threads():
        channels = list_channels(client_config, team_ids, selected_channels)
        yield from sync_threads(
            graph_rest_client(client_config), channels, dlt.current.resource_state()
        )

    @dlt.source(name=TEAMS_SOURCE_NAME)
    def _teams():
        return teams_threads

    source = _teams()
    # Opt into the document ingestion path (thread → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, TEAMS_SOURCE_NAME)
    # Keep the per-channel cursors in a pipeline of their own, so syncing into
    # two datasets in turn does not restore one dataset's cursors into the other.
    # One source per tenant per dataset: the staging table name is fixed.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, f"{TEAMS_SOURCE_NAME}:{tenant_id}")
    return source


# ---------------------------------------------------------------------------
# Graph reads
# ---------------------------------------------------------------------------


def list_channels(
    client_config: dict[str, Any],
    team_ids: list[str] | None,
    channel_ids: set[str] | None = None,
) -> list[dict]:
    """Return the standard channels of the selected teams via dlt's ``rest_api``.

    Each channel dict carries Graph's channel fields plus ``_teams_id`` (the
    parent team id). The listing must be complete: ``sync_threads`` treats a
    known channel missing from it as deleted.
    """
    if team_ids:
        # rest_api resolves child paths from any parent resource, so explicit
        # ids feed the same channel endpoint as the listed teams would. Yield
        # them as one page: rest_api children consume their parent page-wise.
        @dlt.resource(name="teams")
        def teams():
            yield [{"id": team_id} for team_id in team_ids]

    else:
        teams = {"name": "teams", "endpoint": {"path": "teams"}}

    config: Any = {
        "client": client_config,
        "resource_defaults": {"endpoint": {"data_selector": GRAPH_DATA_SELECTOR}},
        "resources": [
            teams,
            {
                "name": "channels",
                "include_from_parent": ["id"],
                "endpoint": {
                    "path": "teams/{resources.teams.id}/channels",
                    "params": {
                        # Private and shared channels have their own membership;
                        # only standard channels are visible to the whole team.
                        "$filter": "membershipType eq 'standard'",
                        # Skips the channel email, which Graph is slow to fill.
                        "$select": "id,displayName,membershipType",
                    },
                    # A deleted team drops out like a deleted channel, so its
                    # threads are forgotten. 401/403 still abort the run.
                    "response_actions": [{"status_code": 404, "action": "ignore"}],
                },
            },
        ],
    }
    resources = {resource.name: resource for resource in rest_api_resources(config)}
    channels = list(resources["channels"])
    if channel_ids is not None:
        channels = [channel for channel in channels if channel.get("id") in channel_ids]
    return channels


def _walk_channel(client, team_id: str, channel_id: str) -> Iterator[tuple[dict, list[dict]]]:
    """Yield ``(root, replies)`` for a channel's threads, newest chain first.

    Pages are fetched lazily, so a caller that stops iterating stops paging.
    Replies past the inline page (``replies@odata.nextLink``) are fetched
    before the thread is yielded, so the caller always sees the whole thread.
    """
    path = f"teams/{team_id}/channels/{channel_id}/messages"
    params = {"$top": _PAGE_SIZE, "$expand": "replies"}
    for page in client.paginate(path, params=params, hooks=_IGNORE_NOT_FOUND):
        for root in page:
            replies = list(root.get("replies") or [])
            next_link = root.get("replies@odata.nextLink")
            if next_link:
                for reply_page in client.paginate(next_link, hooks=_IGNORE_NOT_FOUND):
                    replies.extend(reply_page)
            yield root, replies


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def sync_threads(client, channels: list[dict], state: dict) -> Iterator[dict[str, Any]]:
    """Yield changed threads since the last run, plus hard-delete markers.

    ``state["channels"]`` maps ``<team>:<channel>`` to that channel's cursor
    and the root ids of the threads it contributed. A channel's entry is only
    replaced after its walk finishes, and dlt only commits the state when the
    whole run loads, so a failed run leaves every cursor where it was.
    """
    known: dict[str, dict] = state.setdefault("channels", {})
    current: set[str] = set()

    for channel in channels:
        key = _channel_key(channel["_teams_id"], channel["id"])
        current.add(key)
        known[key] = yield from _sync_channel(client, channel, known.get(key) or {})

    vanished = set(known) - current
    if vanished and not current:
        # An empty listing while channels were known almost always means a
        # transient failure or a selection typo, not a wipe. Forgetting every
        # thread on that signal would purge the dataset, so keep it this run.
        logger.warning(
            "Teams: channel listing returned 0 channels but %d were known; skipping "
            "forget-on-delete this run.",
            len(known),
        )
        vanished = set()

    for key in sorted(vanished):
        entry = known.pop(key)
        logger.info(
            "Teams: channel %s is gone; forgetting its %d thread(s).",
            key,
            len(entry.get("threads", [])),
        )
        for root_id in entry.get("threads", []):
            yield _deleted_row(_thread_id(entry["team_id"], entry["channel_id"], root_id))


def _sync_channel(client, channel: dict, entry: dict) -> Generator[dict, None, dict]:
    """Walk one channel back to its cursor, yielding rows; return its new entry.

    The entry is returned (not written to state here) so the caller only
    advances the channel once the walk has finished without error.
    """
    team_id = channel["_teams_id"]
    channel_id = channel["id"]
    channel_name = channel.get("displayName") or channel_id

    cursor = _parse_time(entry.get("cursor"))
    stop_before = cursor - _CURSOR_OVERLAP if cursor else None
    threads = set(entry.get("threads", []))
    newest = cursor
    changed = deleted = 0

    for root, replies in _walk_channel(client, team_id, channel_id):
        chain_time = _chain_time(root, replies)
        if stop_before and chain_time and chain_time < stop_before:
            # Threads are sorted by chain time, so the rest are older still.
            break
        if chain_time and (newest is None or chain_time > newest):
            newest = chain_time

        if root.get("messageType") != "message":
            # System events (member added, channel renamed, ...) carry no prose.
            continue

        root_id = root["id"]
        thread_id = _thread_id(team_id, channel_id, root_id)
        row = None if root.get("deletedDateTime") else _thread_to_row(root, replies, channel_name)
        if row is not None:
            row["id"] = thread_id
            threads.add(root_id)
            changed += 1
            yield row
        elif root_id in threads:
            # A deleted root, or one left with no text (only images/cards).
            threads.discard(root_id)
            deleted += 1
            yield _deleted_row(thread_id)

    logger.info(
        "Teams: channel %s synced %d changed thread(s), %d deletion(s).",
        channel_name,
        changed,
        deleted,
    )
    return {
        "team_id": team_id,
        "channel_id": channel_id,
        "cursor": _format_time(newest) if newest else None,
        "threads": sorted(threads),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _thread_to_row(root: dict, replies: list[dict], channel_name: str) -> dict | None:
    """Render a thread to a document row, or None when it has no text.

    Only ``title``/``content``/``url`` (+ ``id``) are kept, so a reaction that
    bumps ``lastModifiedDateTime`` without changing any text does not churn
    the content-hash data_id.
    """
    live_replies = [
        reply
        for reply in replies
        if reply.get("messageType") == "message" and not reply.get("deletedDateTime")
    ]
    live_replies.sort(key=lambda reply: (reply.get("createdDateTime") or "", reply.get("id")))

    sections = [_render_message(root, verb="wrote")]
    sections.extend(_render_message(reply, verb="replied") for reply in live_replies)
    sections = [section for section in sections if section]
    if not sections:
        return None

    return {
        "title": _thread_title(root, channel_name),
        "content": "\n\n".join(sections),
        "url": root.get("webUrl"),
        # Hard-delete marker (always False for live threads). Deleted threads
        # are emitted separately with _deleted=True.
        "_deleted": False,
    }


def _render_message(message: dict, verb: str) -> str:
    """Render one message as ``<author> <verb> on <date> at <time> UTC:`` and its text.

    The header reads as a sentence on purpose: label-style headers such as
    ``Reply from <author> (<time>):`` were extracted as entities of their own.
    """
    text = _message_text(message)
    if not text:
        return ""
    created = _parse_time(message.get("createdDateTime"))
    when = f" on {created:%Y-%m-%d} at {created:%H:%M} UTC" if created else ""
    return f"{_author(message)} {verb}{when}:\n{text}"


def _message_text(message: dict) -> str:
    """Plain text of a message body plus the names of any attached files."""
    body = message.get("body") or {}
    content = body.get("content") or ""
    text = html_to_text(content) if body.get("contentType") == "html" else content.strip()

    names = [
        attachment["name"]
        for attachment in message.get("attachments") or []
        if isinstance(attachment, dict) and attachment.get("name")
    ]
    if names:
        attached = "\n".join(f"[attachment: {name}]" for name in names)
        text = f"{text}\n{attached}" if text else attached
    return text


def _author(message: dict) -> str:
    """Display name of whoever (user, bot, or device) sent the message."""
    sender = message.get("from") or {}
    for kind in ("user", "application", "device"):
        name = (sender.get(kind) or {}).get("displayName")
        if name:
            return name
    return "Unknown sender"


def _thread_title(root: dict, channel_name: str) -> str:
    """``<channel>: <subject>``, falling back to the first line of the root."""
    subject = (root.get("subject") or "").strip()
    if not subject:
        first_line = next((line for line in _message_text(root).splitlines() if line), "")
        subject = first_line[:_TITLE_SNIPPET_CHARS].rstrip()
    return f"{channel_name}: {subject}" if subject else channel_name


def html_to_text(raw: str | None) -> str:
    """Convert a Teams HTML message body to plain text, keeping line breaks.

    Dependency-free on purpose: message bodies are small, simple fragments,
    and feeding raw tags into entity extraction would only add noise.
    """
    if not raw:
        return ""
    text = _MENTION_RE.sub(lambda match: f"@{_TAG_RE.sub('', match.group(1))}", raw)
    text = _LIST_ITEM_RE.sub("- ", text)
    text = _BREAK_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = (_SPACES_RE.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Ids and timestamps
# ---------------------------------------------------------------------------


def _channel_key(team_id: str, channel_id: str) -> str:
    return f"{team_id}:{channel_id}"


def _thread_id(team_id: str, channel_id: str, root_id: str) -> str:
    """Stable document id for a thread: channel ids are only unique per team."""
    return f"thread:{team_id}:{channel_id}:{root_id}"


def _deleted_row(thread_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a thread by id."""
    return {"id": thread_id, "_deleted": True}


def _chain_time(root: dict, replies: list[dict]) -> datetime | None:
    """Latest change anywhere in the thread: the key Graph sorts threads by."""
    times = [
        _parse_time(message.get(field))
        for message in (root, *replies)
        for field in ("lastModifiedDateTime", "deletedDateTime", "createdDateTime")
    ]
    return max((time for time in times if time), default=None)


def _parse_time(value: str | None) -> datetime | None:
    """Parse a Graph ISO-8601 timestamp (any fractional precision) to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Teams: ignoring unparseable timestamp %r.", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
