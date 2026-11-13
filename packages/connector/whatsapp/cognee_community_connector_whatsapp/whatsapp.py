"""WhatsApp connector for cognee: a ``dlt`` source that turns chat exports into memory.

Point it at WhatsApp "Export chat" files (an Android ``.txt`` or an iPhone
``.zip``) and hand it to :func:`cognee.remember`::

    await cognee.remember(
        whatsapp_source(["WhatsApp Chat with Suppliers.txt"]),
        dataset_name="whatsapp",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Each chat becomes one document per day, so a question about "what the supplier
promised last month" lands on the right day of the right chat. Re-importing a
newer export only re-processes the days that changed. Deletes are window-aware:
WhatsApp caps an export at the newest messages, so a day older than the export
covers is kept, and only days inside the export's range can be forgotten.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

from cognee_community_connector_whatsapp.parser import (
    ChatExport,
    ExportFormatError,
    Message,
    read_export,
)

logger = get_logger("whatsapp_connector")

WHATSAPP_SOURCE_NAME = "whatsapp"
WHATSAPP_TABLE_NAME = "whatsapp_chat_days"

# WhatsApp keeps only the newest 10,000 messages in an export with media and
# 40,000 without. An export at or above the lower cap may have been cut, so its
# oldest day can be partial: that day is never treated as a change or a delete.
EXPORT_CAP_MIN = 10_000

# File names WhatsApp gives exports; a folder is scanned for these only.
_EXPORT_FILE = re.compile(r"^WhatsApp Chat (with |- ).+\.(txt|zip)$", re.IGNORECASE)
# "+91 98765 43210", "+1 (555) 123-4567": an international number, as a sender or in text.
_PHONE = re.compile(r"\+\d[\d\s\-()]{7,}\d")
# "@919876543210": a mention of someone not in the exporter's contacts.
_MENTION = re.compile(r"@\d{8,}")


def whatsapp_source(
    paths: str | Path | Iterable[str | Path],
    *,
    mask_phone_numbers: bool = True,
    date_order: str | None = None,
    chat_ids: dict[str, str] | None = None,
    resource_name: str = WHATSAPP_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource that yields one document per chat per day.

    Args:
        paths: Export files (``.txt`` or ``.zip``) and/or folders. A folder is
            scanned (not recursively) for files named like WhatsApp exports.
        mask_phone_numbers: Replace international phone numbers (senders not
            in the exporter's contacts, and numbers in the text) with ``+\u20261234``.
        date_order: ``"dmy"``, ``"mdy"`` or ``"ymd"`` to override date-order
            detection, for exports whose dates never go past the 12th.
        chat_ids: Map a chat name to a stable id, so a renamed group stays one
            chat (``{"Goa Trip 2026": "goa-trip"}``).
        resource_name: dlt resource (table) name, which also scopes the sync
            state. Give each set of exports its own name in a shared dataset.
        check_active: Optional host authorization checkpoint, called around every
            row; it stops the sync by raising.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"``
        and a ``_deleted`` hard-delete column. ``resource.cognee_sync_stats``
        holds the last run's counts.
    """
    if not all(
        hasattr(dlt_utils, name)
        for name in ("PIPELINE_SCOPE_ATTR", "guarded_rows", "NODE_SET_COLUMN")
    ):
        raise RuntimeError(
            "The WhatsApp connector needs cognee's dlt_utils.PIPELINE_SCOPE_ATTR, guarded_rows "
            "and NODE_SET_COLUMN (cognee>=1.6.2)."
        )
    _check_resource_name(resource_name)
    if date_order is not None and date_order not in ("dmy", "mdy", "ymd"):
        raise ValueError('date_order must be "dmy", "mdy" or "ymd"')
    files = _export_files(paths)
    if not files:
        raise ValueError("whatsapp_source found no WhatsApp export files in the given paths")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def whatsapp_chat_days():
        stats.clear()
        rows = sync_exports(
            files,
            dlt.current.resource_state(),
            mask_phone_numbers=mask_phone_numbers,
            date_order=date_order,
            chat_ids=chat_ids or {},
            stats=stats,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = whatsapp_chat_days()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(resource, DOCUMENT_SOURCE_ATTR, WHATSAPP_SOURCE_NAME)
    # Give the sync state its own dlt pipeline, apart from other sources.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource


def sync_exports(
    files: list[Path],
    state: dict,
    *,
    mask_phone_numbers: bool = True,
    date_order: str | None = None,
    chat_ids: dict[str, str] | None = None,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new, changed and deleted day rows, updating ``state`` as it goes.

    ``state`` is the dlt resource state (a plain dict). For each chat it keeps
    one fingerprint per day: ``state["chats"][chat_id]["days"][iso_date]``.
    """
    stats = stats if stats is not None else {}
    for key in (
        "files",
        "failed_files",
        "days_new",
        "days_changed",
        "days_unchanged",
        "days_deleted",
        "unparsed_lines",
        "ambiguous_date_order",
    ):
        stats.setdefault(key, 0)
    chats_state: dict = state.setdefault("chats", {})

    exports: list[ChatExport] = []
    for path in files:
        stats["files"] += 1
        try:
            export = read_export(path, date_order=date_order)
        except (ExportFormatError, OSError, UnicodeDecodeError, ValueError) as exc:
            # A file that can't be read changes nothing: no rows, no deletes.
            stats["failed_files"] += 1
            logger.warning("WhatsApp: skipped %s (%s)", path.name, type(exc).__name__)
            continue
        if not export.messages:
            stats["failed_files"] += 1
            continue
        stats["unparsed_lines"] += export.unparsed_lines
        stats["ambiguous_date_order"] += int(export.ambiguous_date_order)
        exports.append(export)

    # Oldest export first, so when two exports of a chat are given the newer wins.
    exports.sort(key=lambda e: e.messages[-1].sent_at)
    for export in exports:
        chat_id = _chat_id(export.chat_name, chat_ids or {})
        days_state: dict = chats_state.setdefault(chat_id, {}).setdefault("days", {})
        yield from _sync_chat(export, chat_id, days_state, mask_phone_numbers, stats)


def _sync_chat(
    export: ChatExport,
    chat_id: str,
    days_state: dict,
    mask_phone_numbers: bool,
    stats: dict[str, int],
) -> Iterator[dict[str, Any]]:
    days = export.days()
    covered = sorted(days)
    first, last = covered[0], covered[-1]
    # A capped export may start partway through its oldest day.
    maybe_cut = len(export.messages) >= EXPORT_CAP_MIN

    for day in covered:
        key = day.isoformat()
        if maybe_cut and day == first and key in days_state:
            stats["days_unchanged"] += 1
            continue
        row = _day_row(export.chat_name, chat_id, key, days[day], mask_phone_numbers)
        fingerprint = _fingerprint(row)
        previous = days_state.get(key)
        if previous == fingerprint:
            stats["days_unchanged"] += 1
            continue
        stats["days_changed" if previous else "days_new"] += 1
        days_state[key] = fingerprint
        yield row

    # Forget only inside the range this export covers. Days before it may simply
    # be older than the export cap, and days after it come from a newer export.
    lower = first.isoformat()
    for key in sorted(days_state):
        inside = (lower < key if maybe_cut else lower <= key) and key <= last.isoformat()
        if inside and _date_key(key) not in days:
            del days_state[key]
            stats["days_deleted"] += 1
            yield {"id": _row_id(chat_id, key), "_deleted": True}


def _day_row(
    chat_name: str,
    chat_id: str,
    day_key: str,
    messages: list[Message],
    mask_phone_numbers: bool,
) -> dict[str, Any]:
    mask = _mask if mask_phone_numbers else (lambda value: value)
    people = sorted({mask(m.sender) for m in messages if m.sender})
    lines = []
    for message in messages:
        clock = message.sent_at.strftime("%H:%M")
        text = mask(message.text).replace("\n", "\n    ")
        if message.sender is None:
            lines.append(f"{clock} · {text}")
        else:
            lines.append(f"{clock} {mask(message.sender)}: {text}")
    when = messages[0].sent_at.strftime("%A, %d %B %Y")
    header = [f"Chat: {chat_name} (WhatsApp)", f"Date: {when}"]
    if people:
        header.append(f"People: {', '.join(people)}")
    return {
        "id": _row_id(chat_id, day_key),
        "title": f"{chat_name}, {day_key}",
        "content": "\n".join([*header, "", *lines]),
        "url": "",
        dlt_utils.NODE_SET_COLUMN: [chat_name],
        "_deleted": False,
    }


def _fingerprint(row: dict[str, Any]) -> str:
    payload = "\x1f".join([row["title"], row["content"], *row[dlt_utils.NODE_SET_COLUMN]])
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _row_id(chat_id: str, day_key: str) -> str:
    return f"whatsapp:{chat_id}:{day_key}"


def _date_key(key: str) -> date:
    return date.fromisoformat(key)


def _chat_id(chat_name: str, chat_ids: dict[str, str]) -> str:
    if chat_name in chat_ids:
        return chat_ids[chat_name]
    return re.sub(r"\s+", " ", chat_name).strip().casefold()


def _mask(value: str) -> str:
    """Hide international phone numbers, keeping the last four digits."""

    def hide(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return f"+\u2026{digits[-4:]}"

    value = _PHONE.sub(hide, value)
    return _MENTION.sub(lambda m: f"@\u2026{m.group(0)[-4:]}", value)


def _export_files(paths: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"no such export file or folder: {path.name}")
        if path.is_dir():
            found.extend(
                sorted(p for p in path.iterdir() if p.is_file() and _EXPORT_FILE.match(p.name))
            )
        else:
            found.append(path)
    # De-duplicate while keeping the caller's order.
    return list(dict.fromkeys(found))


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that
    scope, and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share
    a table.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > 64:
        raise ValueError("resource_name must be at most 64 characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")
