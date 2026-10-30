"""ChatGPT conversation-export connector for cognee - a ``dlt`` source over the
official ChatGPT data export (Settings --> Data controls --> Export data).

Parses ``conversations*.json`` from the export (zip or already-extracted
folder) into one document row per conversation and hands them to
:func:`cognee.remember`, reusing the existing DLT ingestion path
(``resolve_dlt_sources`` --> ``ingest_dlt_source`` --> ``orphan_cleanup``)::

    import cognee
    from cognee_community_connector_chatgpt import chatgpt_source

    await cognee.remember(
        chatgpt_source("/path/to/export.zip"),
        dataset_name="my-chatgpt-history",
    )

Design
------
* **No auth.** This reads a file the user already has; there is nothing to
  authenticate against.
* **Primary key** - the conversation's ``id`` (its ``conversation_id`` in
  newer exports; ``id`` is used for backward compatibility with older ones).
* **Snapshot sync (``replace``)** - an export is a *full snapshot* of the
  account's conversations at the time it was requested, not a live feed, so
  the resource uses ``write_disposition="replace"`` (cognee's document-mode
  default too). Each sync reads back the whole snapshot; a conversation
  deleted in ChatGPT since the last export simply does not appear in the new
  one, so it drops out of the set and cognee's ``orphan_cleanup`` purges it
  from the graph + vector + relational stores. See the Notion/Slack
  connectors for the same pattern over a different full-snapshot source.
* **Conversation reconstruction.** ChatGPT stores each conversation as a DAG
  (``mapping``: id -> node, each node holding a ``parent`` and a ``message``)
  because edits and regenerated answers branch rather than overwrite. We walk
  ``current_node`` back to the root via ``parent`` links and reverse the
  result - this is "the conversation the user actually kept," with abandoned
  edit/regeneration branches never visited at all.
* **Incremental cognify** - a conversation's rendered content is a pure
  function of its data, so an unchanged conversation renders byte-identical
  text on every run, keeps the same content-hash ``data_id``, and is not
  re-cognified (see ``cognee/tasks/ingestion/resolve_dlt_sources.py:
  _dlt_row_identifier``). Keep the default ``incremental_loading=True``.
* **Privacy filtering** - conversations marked ``is_do_not_remember`` are
  excluded by default (``include_do_not_remember=False``); the user marked
  them out of ChatGPT's own memory, so a *different* memory system should not
  pick them up without being told to. Archived conversations are included by
  default (archiving tidies the sidebar; it is not a delete) - set
  ``include_archived=False`` to exclude them.
* **Images** become a ``[image]`` placeholder; a DALL-E prompt in the image's
  metadata is kept as text (it is content the user typed), the pixel data is
  not. Voice-mode turns keep their transcription.
* **Custom instructions** (the user's own system message) are kept; every
  other system message is plumbing and is dropped.
* **Projects / Custom GPTs** (``conversation_template_id`` / ``gizmo_id``) are
  written as ``Project: g-p-...`` / ``Custom GPT: g-...`` header lines, so
  conversations in the same one share that identifier in the graph.
* **Stale-export guard.** Every conversation id seen in any export is
  remembered (keyed by the cognee dataset plus the account's own id from
  ``user.json``) in dlt's
  *source* state -- resource state resets on every run of a ``replace``
  resource, so this has to live one level up -- along with its
  ``update_time`` (or, if an export omits it, its newest timestamp). Before
  committing a sync, any previously-seen id that is missing from the current
  export *and* whose recorded ``update_time`` is newer than everything in the
  current export raises :class:`StaleExportError` before anything is
  yielded, so ``write_disposition="replace"`` never gets
  the chance to treat an old/partial export as "everything else was deleted."
  A wrong guess here costs a skipped sync, never lost memory. Pass
  ``allow_deletes=True`` to skip the check (e.g. you are certain the export is
  current, or deliberately pointing at an older one to roll back).

Deliberately not stored
-----------------------
Tool plumbing (code-interpreter calls and output, browsing, canvas/DALL-E tool
calls), scraped web pages (``sonic_webpage``), model reasoning (``thoughts`` /
``reasoning_recap``) and raw image/audio bytes. They are machine output or
media rather than the conversation itself, and would bloat the graph with
text the user never wrote or read as part of the chat.
"""

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("chatgpt_connector")

CHATGPT_TABLE_NAME = "chatgpt_conversations"
CHATGPT_SOURCE_NAME = "chatgpt"
_DEFAULT_ACCOUNT_KEY = "unknown-account"


class StaleExportError(RuntimeError):
    """Raised when the export looks older or more incomplete than data this
    connector already synced, to stop a ``replace`` sync from treating a
    genuinely-known conversation as deleted. Re-export a current copy, or
    pass ``allow_deletes=True`` if the export is intentionally authoritative."""


# Matches conversations.json at the export root, nested under
# personal/conversations/ (current export layout), or split into
# conversations-000.json, conversations-001.json, ... (large accounts).
_CONVERSATIONS_FILE_RE = re.compile(r"(^|/)conversations(-\d+)?\.json$")

_SPEAKERS = {"user": "User", "assistant": "Assistant", "system": "Custom instructions"}

# Roles whose text we keep. Everything else (system plumbing, tool output)
# is dropped unless explicitly allowed below.
_KEPT_ROLES = {"user", "assistant"}

# content_types that never carry conversational text worth keeping, even
# from a kept role (reasoning summaries, browsing status, web-search hits).
_DROPPED_CONTENT_TYPES = {
    "thoughts",
    "reasoning_recap",
    "tether_browsing_display",
    "sonic_webpage",
    "system_error",
}


def chatgpt_source(
    export_path: str,
    *,
    include_do_not_remember: bool = False,
    include_archived: bool = True,
    allow_deletes: bool = False,
):
    """Create a dlt source that yields ChatGPT conversations as text documents.

    Args:
        export_path: Path to the export .zip, or to an already-extracted
            export folder.
        include_do_not_remember: Include conversations ChatGPT itself marked
            excluded from memory (``is_do_not_remember``). Default False.
        include_archived: Include archived conversations. Default True -
            archiving only hides a conversation from the sidebar, it does not
            delete it.
        allow_deletes: Skip the stale-export guard (see module docs) and
            trust this export as authoritative even if it looks older or
            more incomplete than data already synced. Default False.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.

    Raises:
        StaleExportError: the export is missing a conversation this
            connector already saw with a newer ``update_time``, and
            ``allow_deletes`` was not set.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The chatgpt connector requires dlt: pip install "cognee-community-connector-chatgpt"'
        ) from exc

    export_path = str(export_path)

    @dlt.resource(
        name=CHATGPT_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
        columns={"url": {"data_type": "text", "nullable": True}},
    )
    def chatgpt_conversations():
        # Two passes are required: the stale-export guard has to see every
        # conversation id and update_time in this export (regardless of the
        # do-not-remember/archived filters below, which describe what we
        # *choose* to remember, not what the export actually contains)
        # before anything is yielded, so a replace sync never commits a
        # partial/old export as "the rest was deleted."
        raw_conversations = list(_iter_raw_conversations(export_path))

        # cognee runs every dataset through one dlt pipeline ("ingest_dlt_source"),
        # so source state is shared across datasets: scope the watermark to the
        # destination dataset too, or syncing one dataset blocks another.
        scope_key = f"{dlt.current.pipeline().dataset_name}:{_account_key(export_path)}"
        source_state = dlt.current.source_state()
        account_state = source_state.setdefault("chatgpt_seen", {}).setdefault(scope_key, {})
        previously_seen = dict(account_state)

        current_ids: set[str] = set()
        current_update_times: dict[str, float] = {}
        for raw in raw_conversations:
            conv_id = raw.get("conversation_id") or raw.get("id")
            if not conv_id:
                continue
            conv_id = str(conv_id)
            current_ids.add(conv_id)
            last_activity = _last_activity(raw)
            if last_activity is not None:
                current_update_times[conv_id] = last_activity
        current_max_update = max(current_update_times.values(), default=0.0)

        if not allow_deletes:
            stale = {
                conv_id: last_update
                for conv_id, last_update in previously_seen.items()
                if conv_id not in current_ids and last_update > current_max_update
            }
            if stale:
                sample = ", ".join(f"{cid} (update_time={t})" for cid, t in list(stale.items())[:5])
                raise StaleExportError(
                    f"This export is missing {len(stale)} conversation(s) already synced with a "
                    f"newer update_time than anything in this export (e.g. {sample}). Refusing to "
                    "sync: a replace snapshot would treat them as deleted. Re-export a current "
                    "copy of your ChatGPT data, or pass allow_deletes=True if this export is "
                    "intentionally authoritative (e.g. a deliberate rollback)."
                )

        count = 0
        skipped_do_not_remember = 0
        skipped_archived = 0
        skipped_empty = 0
        for raw in raw_conversations:
            conv_id = raw.get("conversation_id") or raw.get("id")
            if not conv_id:
                continue
            if raw.get("is_do_not_remember") and not include_do_not_remember:
                skipped_do_not_remember += 1
                continue
            if raw.get("is_archived") and not include_archived:
                skipped_archived += 1
                continue
            row = _conversation_to_row(raw, conv_id)
            if row is None:
                skipped_empty += 1
                continue
            count += 1
            yield row

        # Only recorded once the guard above has passed (or been overridden)
        # for this run, so a raised/aborted run leaves the state exactly as
        # it was -- consistent with nothing else in this run being committed.
        account_state.clear()
        account_state.update(current_update_times)
        # Ids with no update_time still need to be remembered as "seen", with
        # a 0.0 watermark (never flags a future run as stale on their behalf,
        # but still lets this run's export be the known-current one for ids
        # that otherwise weren't trackable).
        for conv_id in current_ids - current_update_times.keys():
            account_state[conv_id] = 0.0

        logger.info(
            "ChatGPT export: synced %d conversation(s) "
            "(skipped %d do-not-remember, %d archived, %d empty).",
            count,
            skipped_do_not_remember,
            skipped_archived,
            skipped_empty,
        )

    @dlt.source(name=CHATGPT_SOURCE_NAME)
    def _chatgpt():
        return chatgpt_conversations

    source = _chatgpt()
    # Opt into the document ingestion path (conversation -> text document ->
    # cognify). resolve_dlt_sources reads this marker; it never imports this
    # connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, CHATGPT_SOURCE_NAME)
    return source


def _account_key(export_path: str) -> str:
    """The ChatGPT account id from the export's user.json, so the
    stale-export watermark never mixes up two different accounts' exports.
    Falls back to a constant key (logged once) when user.json is absent or
    unreadable -- the guard still works, just without per-account scoping."""
    try:
        path = Path(export_path)
        if path.is_file() and path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if n.endswith("user.json")]
                if not names:
                    raise FileNotFoundError
                with zf.open(sorted(names)[0]) as fh:
                    data = json.loads(fh.read().decode("utf-8"))
        elif path.is_dir():
            matches = sorted(path.rglob("user.json"))
            if not matches:
                raise FileNotFoundError
            with open(matches[0], encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            raise FileNotFoundError
        account_id = data.get("id") if isinstance(data, dict) else None
        if account_id:
            return str(account_id)
        raise ValueError("user.json has no 'id' field")
    except Exception as exc:
        logger.warning(
            "ChatGPT export: could not determine the account id from user.json (%s); "
            "the stale-export guard will use a shared key across all exports passed to "
            "this connector.",
            exc,
        )
        return _DEFAULT_ACCOUNT_KEY


# ---------------------------------------------------------------------------
# Export reading (module-private)
# ---------------------------------------------------------------------------


def _iter_raw_conversations(export_path: str):
    """Yield raw conversation dicts from every conversations*.json in the export.

    Accepts a .zip file or an extracted folder. Within either, the file may
    sit at the root, nested (``personal/conversations/conversations.json``),
    or split across numbered parts - all are found by name pattern at any
    depth. Each file holds either a bare JSON array of conversations, or an
    object with a ``"conversations"`` key holding that array.
    """
    path = Path(export_path)
    if path.is_file() and path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if _CONVERSATIONS_FILE_RE.search(n)]
            if not names:
                raise ValueError(f"No conversations*.json found in export zip: {export_path}")
            for name in sorted(names):
                with zf.open(name) as fh:
                    data = json.loads(fh.read().decode("utf-8"))
                yield from _coerce_conversations_list(data, name)
        return

    if path.is_dir():
        found = False
        for root, _dirs, files in os.walk(path):
            for fname in sorted(files):
                full = Path(root) / fname
                rel = full.relative_to(path).as_posix()
                if _CONVERSATIONS_FILE_RE.search(rel):
                    found = True
                    with open(full, encoding="utf-8") as fh:
                        data = json.load(fh)
                    yield from _coerce_conversations_list(data, rel)
        if not found:
            raise ValueError(f"No conversations*.json found under export folder: {export_path}")
        return

    raise ValueError(f"export_path must be a .zip file or a folder, got: {export_path!r}")


def _coerce_conversations_list(data: Any, source_name: str) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("conversations"), list):
        return data["conversations"]
    raise ValueError(
        f"{source_name} is not a recognized conversations export "
        "(expected a JSON array, or an object with a 'conversations' array)."
    )


# ---------------------------------------------------------------------------
# Conversation -> row (module-private; pure functions, no I/O)
# ---------------------------------------------------------------------------


def _conversation_to_row(raw: dict, conv_id: str) -> dict | None:
    """Build a document row from one raw conversation, or None if it has no
    keepable content (e.g. a conversation that was started and abandoned
    with no messages)."""
    title = _clean(raw.get("title")) or "Untitled conversation"
    messages = _linear_messages(raw)
    body = _render_messages(messages)
    if not body:
        return None

    header_lines = [f"# {title}", *_grouping_lines(raw)]
    text = "\n".join(header_lines) + "\n\n" + body

    return {
        "id": str(conv_id),
        "title": title,
        "content": text,
        "url": None,
    }


def _linear_messages(raw: dict) -> list[dict]:
    """Walk current_node -> root via parent links; return messages in
    chronological order. Branches from edits/regenerations are never
    visited, so only the path the user actually kept comes back."""
    mapping = raw.get("mapping") or {}
    node_id = raw.get("current_node")
    seen: set[str] = set()
    messages: list[dict] = []
    while node_id and node_id in mapping:
        if node_id in seen:
            # A cyclic mapping would hang this walk forever; a well-formed
            # export never has one, but a corrupt/truncated file might.
            logger.warning("ChatGPT export: cycle detected in conversation mapping, stopping walk.")
            break
        seen.add(node_id)
        node = mapping[node_id]
        message = node.get("message")
        if message:
            messages.append(message)
        node_id = node.get("parent")
    return list(reversed(messages))


def _render_messages(messages: list[dict]) -> str:
    lines = []
    for message in messages:
        if _is_hidden(message):
            continue
        role = message.get("author", {}).get("role")
        text = _render_content(message.get("content") or {})
        if not text:
            continue
        speaker = _SPEAKERS.get(role, "Assistant")
        lines.append(f"{speaker}: {text}")
    return "\n\n".join(lines)


def _is_hidden(message: dict) -> bool:
    author = message.get("author") or {}
    role = author.get("role")
    metadata = message.get("metadata") or {}

    if metadata.get("is_visually_hidden_from_conversation"):
        return True
    if role == "system":
        # Keep only the user's own custom instructions, carried as a system
        # message with this flag; drop every other system-role message.
        return not metadata.get("is_user_system_message")
    if role not in _KEPT_ROLES:
        return True
    recipient = message.get("recipient", "all")
    if recipient not in ("all", None):
        # The assistant addressed a tool (canvas, python, dalle, ...) rather
        # than the user - plumbing, not conversation.
        return True
    content_type = (message.get("content") or {}).get("content_type")
    return content_type in _DROPPED_CONTENT_TYPES


def _render_content(content: dict) -> str:
    content_type = content.get("content_type")
    if content_type == "text":
        return _clean("".join(content.get("parts") or []))
    if content_type == "multimodal_text":
        return _render_multimodal_parts(content.get("parts") or [])
    if content_type == "user_editable_context":
        # The user's custom instructions ("about me" + "how to respond").
        fields = (content.get("user_profile"), content.get("user_instructions"))
        return "\n".join(text for text in map(_clean, fields) if text)
    # code / execution_output / tether_quote only ever come from tool
    # plumbing (python, browsing), which _is_hidden already drops.
    return ""


def _render_multimodal_parts(parts: list) -> str:
    rendered = []
    for part in parts:
        if isinstance(part, str):
            text = _clean(part)
            if text:
                rendered.append(text)
        elif isinstance(part, dict) and part.get("content_type") == "image_asset_pointer":
            prompt = _clean(((part.get("metadata") or {}).get("dalle") or {}).get("prompt"))
            rendered.append(f"[image: {prompt}]" if prompt else "[image]")
        elif isinstance(part, dict) and part.get("content_type") == "audio_transcription":
            # Voice-mode turns: the spoken words arrive as a transcription part.
            text = _clean(part.get("text"))
            if text:
                rendered.append(text)
        # Raw audio/video asset pointers carry no text; like image pixels, not stored.
    return " ".join(r for r in rendered if r)


def _grouping_lines(raw: dict) -> list[str]:
    """Header lines naming the ChatGPT project and/or Custom GPT a conversation
    belongs to, so conversations in the same one share that identifier.
    Project ids start with ``g-p-``; other ``g-`` ids are Custom GPTs."""
    ids = []
    for value in (raw.get("conversation_template_id"), raw.get("gizmo_id")):
        if isinstance(value, str) and value.strip() and value.strip() not in ids:
            ids.append(value.strip())
    return [f"{'Project' if i.startswith('g-p-') else 'Custom GPT'}: {i}" for i in ids]


def _last_activity(raw: dict) -> float | None:
    """When a conversation was last touched: its ``update_time``, falling back
    to ``create_time`` or the newest message timestamp for exports that omit it."""
    if isinstance(raw.get("update_time"), (int, float)):
        return float(raw["update_time"])
    times = [raw.get("create_time")]
    for node in (raw.get("mapping") or {}).values():
        times.append(((node or {}).get("message") or {}).get("create_time"))
    times = [float(t) for t in times if isinstance(t, (int, float))]
    return max(times) if times else None


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
