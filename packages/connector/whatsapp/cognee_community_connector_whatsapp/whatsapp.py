"""Read official WhatsApp chat exports as per-chat, per-day Cognee documents."""

from __future__ import annotations

import hashlib
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WHATSAPP_SOURCE_NAME = "whatsapp"
WHATSAPP_TABLE_NAME = "whatsapp_documents"


def _mask_phone_numbers(value: str) -> str:
    """Mask phone-number-like strings to reduce accidental data exposure."""
    return re.sub(
        r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)",
        "[phone]",
        value,
    )


def whatsapp_export_source(
    path: str | Path | Iterable[str | Path],
    *,
    date_order: str = "auto",
    chat_id: str | None = None,
    timezone: str | None = None,
    mask_phone_numbers: bool = True,
):
    """Return a DLT resource for one export, a collection, or a folder of exports.

    ``chat_id`` can pin a single export to a stable identity if its filename
    changes. Directory exports derive their identity from each filename.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The WhatsApp connector requires the dlt extra: pip install "cognee[dlt]".'
        ) from exc

    try:
        from cognee.tasks.ingestion import dlt_utils

        document_source_attr = dlt_utils.DOCUMENT_SOURCE_ATTR
    except ImportError:
        document_source_attr = "cognee_document_source"

    export_paths = _collect_export_paths(path)
    if chat_id is not None and len(export_paths) != 1:
        raise ValueError("chat_id can only be used with a single WhatsApp export.")
    if chat_id is not None and not chat_id.strip():
        raise ValueError("chat_id must not be empty.")

    parsed_exports = []
    seen_chat_ids: set[str] = set()
    for export_path in export_paths:
        chat_name = _chat_name(export_path)
        resolved_chat_id = chat_id or chat_name
        if resolved_chat_id in seen_chat_ids:
            raise ValueError(
                f"Multiple exports resolve to chat_id {resolved_chat_id!r}; "
                "use distinct export filenames."
            )
        seen_chat_ids.add(resolved_chat_id)
        messages, window_start, window_end = _parse_export_details(
            export_path,
            date_order=date_order,
            timezone=timezone,
        )
        parsed_exports.append(
            _ParsedExport(
                chat_id=resolved_chat_id,
                chat_name=chat_name,
                messages=messages,
                window_start=window_start,
                window_end=window_end,
                timezone=timezone,
                mask_phone_numbers=mask_phone_numbers,
            )
        )

    @dlt.resource(
        name=WHATSAPP_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def whatsapp_documents():
        state = dlt.current.resource_state()
        rows, known_days = _sync_export_rows(
            parsed_exports,
            state.get("known_days", {}),
        )
        state["known_days"] = known_days
        yield from rows

    setattr(whatsapp_documents, document_source_attr, WHATSAPP_SOURCE_NAME)
    return whatsapp_documents


def _collect_export_paths(
    paths: str | Path | Iterable[str | Path],
) -> list[Path]:
    """Expand directories and validate all requested WhatsApp export paths."""
    requested = [paths] if isinstance(paths, (str, Path)) else list(paths)
    if not requested:
        raise ValueError("Provide at least one WhatsApp export file or folder.")

    exports: list[Path] = []
    for item in requested:
        path = Path(item)
        if not path.exists():
            raise FileNotFoundError(f"WhatsApp export does not exist: {path}")
        if path.is_dir():
            candidates = sorted(
                (
                    candidate
                    for candidate in path.rglob("*")
                    if candidate.is_file() and candidate.suffix.lower() in {".txt", ".zip"}
                ),
                key=lambda candidate: str(candidate).casefold(),
            )
            if not candidates:
                raise ValueError(f"No .txt or .zip WhatsApp exports found in {path}")
            exports.extend(candidates)
        elif path.suffix.lower() in {".txt", ".zip"}:
            exports.append(path)
        else:
            raise ValueError(f"Expected a .txt or .zip WhatsApp export: {path}")

    return exports


def _chat_name(path: Path) -> str:
    """Derive a readable chat name from the export filename."""
    if path.stem.casefold() in {"_chat", "chat"} and path.parent.name:
        return path.parent.name
    return path.stem


@dataclass
class _ParsedExport:
    chat_id: str
    chat_name: str
    messages: list[WhatsAppMessage]
    window_start: date
    window_end: date
    timezone: str | None
    mask_phone_numbers: bool


def _document_id(chat_id: str, day: str) -> str:
    return f"whatsapp:{chat_id}:{day}"


def _document_row(export: _ParsedExport, day: str, messages: list[WhatsAppMessage]) -> dict:
    lines = [
        f"Chat: {export.chat_name}",
        f"Date: {day}",
    ]
    if export.timezone:
        lines.append(f"Timezone: {export.timezone}")
    lines.append("")

    for message in sorted(messages, key=lambda item: item.timestamp):
        sender = message.sender or "Unknown"
        if export.mask_phone_numbers:
            sender = _mask_phone_numbers(sender)
        lines.append(f"{message.timestamp:%H:%M} {sender}: {message.text}")

    title = f"{export.chat_name} — {day}"
    content = "\n".join(lines).rstrip()
    content_hash = hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()
    return {
        "id": _document_id(export.chat_id, day),
        "title": title,
        "content": content,
        "content_hash": content_hash,
        "_deleted": False,
    }


def _sync_export_rows(
    exports: list[_ParsedExport],
    known_days: dict[str, list[str]],
) -> tuple[list[dict], dict[str, list[str]]]:
    """Build daily documents and window-scoped hard-delete rows."""
    updated_known = {chat_id: set(days) for chat_id, days in known_days.items()}
    current_by_export: list[tuple[_ParsedExport, dict[str, list[WhatsAppMessage]]]] = []

    for export in exports:
        messages_by_day: dict[str, list[WhatsAppMessage]] = {}
        for message in export.messages:
            day = message.timestamp.date().isoformat()
            messages_by_day.setdefault(day, []).append(message)
        current_by_export.append((export, messages_by_day))

    rows: list[dict] = []
    for export, messages_by_day in current_by_export:
        current_days = set(messages_by_day)
        rows.extend(
            _document_row(export, day, messages_by_day[day]) for day in sorted(current_days)
        )

        chat_known_days = updated_known.setdefault(export.chat_id, set())
        deleted_days = sorted(
            day
            for day in chat_known_days
            if export.window_start.isoformat() <= day <= export.window_end.isoformat()
            and day not in current_days
        )
        rows.extend(
            {"id": _document_id(export.chat_id, day), "_deleted": True} for day in deleted_days
        )

        chat_known_days.difference_update(deleted_days)
        chat_known_days.update(current_days)

    return rows, {chat_id: sorted(days) for chat_id, days in updated_known.items()}


# Android: 31/12/23, 9:15 pm - Alex: Hello
# iPhone: [31/12/23, 9:15:42 PM] Alex: Hello
DATE_PATTERN = r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
TIME_PATTERN = r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:[aApP]\s*\.?\s*[mM]\.?)?"

ANDROID_LINE = re.compile(
    rf"^(?P<date>{DATE_PATTERN}),\s*"
    rf"(?P<time>{TIME_PATTERN})\s+-\s+"
    rf"(?P<body>.*)$"
)

IPHONE_LINE = re.compile(
    rf"^\[(?P<date>{DATE_PATTERN}),\s*"
    rf"(?P<time>{TIME_PATTERN})\]\s*"
    rf"(?P<body>.*)$"
)

MEDIA_EXTENSIONS = {
    ".jpg": "[photo]",
    ".jpeg": "[photo]",
    ".png": "[photo]",
    ".webp": "[photo]",
    ".mp4": "[video]",
    ".mov": "[video]",
    ".3gp": "[video]",
    ".opus": "[audio]",
    ".ogg": "[audio]",
    ".mp3": "[audio]",
    ".m4a": "[audio]",
    ".pdf": "[document]",
    ".doc": "[document]",
    ".docx": "[document]",
    ".xls": "[document]",
    ".xlsx": "[document]",
    ".vcf": "[contact]",
    ".ppt": "[document]",
    ".pptx": "[document]",
    ".wav": "[audio]",
    ".avi": "[video]",
    ".zip": "[document]",
}


@dataclass
class WhatsAppMessage:
    timestamp: datetime
    sender: str
    text: str


def _clean_text(text: str) -> str:
    """Normalize invisible characters and common export placeholders."""
    text = text.replace("\u200e", "").replace("\u200f", "")
    text = text.replace("\u202f", " ").replace("\xa0", " ")
    text = text.strip()

    # Normalize attached media filenames, e.g. IMG-123.jpg.
    attached = re.fullmatch(
        r"<attached:\s*(.+?)>",
        text,
        flags=re.IGNORECASE,
    )
    if attached:
        filename = attached.group(1)
        extension = Path(filename).suffix.lower()
        return MEDIA_EXTENSIONS.get(extension, "[attachment]")

    file_attached = re.fullmatch(r"(?:(.+?)\s*)?\(file attached\)", text, flags=re.IGNORECASE)
    if file_attached:
        filename = file_attached.group(1)
        extension = Path(filename.strip()).suffix.lower() if filename else ""
        return MEDIA_EXTENSIONS.get(extension, "[attachment]")

    if re.fullmatch(r"(?:<media omitted>|media omitted)", text, flags=re.IGNORECASE):
        return "[media]"

    omitted = re.fullmatch(
        r"(image|photo|video|audio|sticker|document|gif|contact|"
        r"location|voice message)\s+omitted",
        text,
        flags=re.IGNORECASE,
    )
    if omitted:
        kind = omitted.group(1).lower()
        if kind == "image":
            kind = "photo"
        if kind == "voice message":
            kind = "audio"
        return f"[{kind}]"

    # WhatsApp sometimes marks a message as edited.
    text = re.sub(r"\s+\(edited\)$", "", text, flags=re.IGNORECASE)
    return text.strip()


def _is_deleted_message(text: str) -> bool:
    normalized = text.strip().casefold()
    return normalized in {
        "this message was deleted",
        "this message was deleted.",
        "you deleted this message",
        "you deleted this message.",
    }


def _is_system_message(body: str) -> bool:
    """Identify common WhatsApp system notices, not ordinary chat messages."""
    text = body.casefold().strip()

    system_phrases = (
        "messages and calls are end-to-end encrypted",
        "created group",
        "created this group",
        "added ",
        "removed ",
        "left",
        "left the group",
        "changed the subject",
        "changed this group's icon",
        "changed the group description",
        "changed the group name",
        "security code changed",
        "your security code changed",
        "this group was created",
    )

    return any(text.startswith(phrase) for phrase in system_phrases)


def _date_order(lines: list[str], requested: str) -> str:
    """Infer DMY or MDY from unambiguous dates; default to DMY."""
    if requested in {"DMY", "MDY"}:
        return requested

    if requested != "auto":
        raise ValueError("date_order must be 'auto', 'DMY', or 'MDY'")

    seen = set()

    for line in lines:
        match = ANDROID_LINE.match(line) or IPHONE_LINE.match(line)
        if not match:
            continue

        parts = re.split(r"[./-]", match.group("date"))
        first, second = int(parts[0]), int(parts[1])

        if first > 12 and second <= 12:
            seen.add("DMY")
        elif second > 12 and first <= 12:
            seen.add("MDY")

    if len(seen) > 1:
        raise ValueError(
            "Export contains conflicting date-order clues. "
            "Pass date_order='DMY' or date_order='MDY' explicitly."
        )

    return next(iter(seen), "DMY")


def _parse_timestamp(
    date_text: str,
    time_text: str,
    order: str,
    timezone_info: tzinfo | None = None,
) -> datetime:
    date_parts = re.split(r"[./-]", date_text)
    first, second, year = map(int, date_parts)

    if len(date_parts[2]) == 2:
        year += 2000 if year < 70 else 1900

    if order == "DMY":
        day, month = first, second
    else:
        month, day = first, second

    normalized_time = re.sub(r"\s+", " ", time_text.strip().upper())
    normalized_time = normalized_time.replace(".", "")

    time_formats = (
        "%I:%M:%S %p",
        "%I:%M %p",
        "%H:%M:%S",
        "%H:%M",
    )

    parsed_time = None
    for fmt in time_formats:
        try:
            parsed_time = datetime.strptime(normalized_time, fmt).time()
            break
        except ValueError:
            continue

    if parsed_time is None:
        raise ValueError(f"Unrecognized time: {time_text!r}")

    return datetime(
        year,
        month,
        day,
        parsed_time.hour,
        parsed_time.minute,
        parsed_time.second,
        tzinfo=timezone_info,
    )


def _resolve_timezone(timezone: str | None) -> tzinfo | None:
    if timezone is None:
        return None
    try:
        return ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(
            f"Unknown timezone {timezone!r}; use an IANA timezone such as 'Asia/Kolkata'."
        ) from exc


def _read_export(path: Path) -> str:
    """Read a TXT export or the chat TXT inside an iPhone ZIP export."""
    if path.is_dir():
        raise ValueError("Pass one export file at a time (.txt or .zip), not a folder.")

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            candidates = [
                name for name in archive.namelist() if Path(name).name.casefold() == "_chat.txt"
            ]

            if not candidates:
                raise ValueError("ZIP export does not contain _chat.txt")

            if len(candidates) > 1:
                raise ValueError("ZIP contains multiple _chat.txt files")

            return archive.read(candidates[0]).decode("utf-8-sig")

    if path.suffix.lower() != ".txt":
        raise ValueError("Expected a .txt or .zip WhatsApp export")

    return path.read_text(encoding="utf-8-sig")


def parse_export(
    path: str | Path,
    *,
    date_order: str = "auto",
    timezone: str | None = None,
) -> list[WhatsAppMessage]:
    """Parse a WhatsApp export into individual messages.

    Timestamps remain timezone-naive unless an IANA ``timezone`` is supplied.
    """
    messages, _, _ = _parse_export_details(
        Path(path),
        date_order=date_order,
        timezone=timezone,
    )
    return messages


def _parse_export_details(
    path: Path,
    *,
    date_order: str = "auto",
    timezone: str | None = None,
) -> tuple[list[WhatsAppMessage], date, date]:
    """Parse messages and return the export's full timestamp coverage window."""
    content = _read_export(path)
    timezone_info = _resolve_timezone(timezone)

    # Remove direction marks and normalize narrow/non-breaking spaces.
    lines = [
        line.replace("\u200e", "").replace("\u200f", "").replace("\u202f", " ").replace("\xa0", " ")
        for line in content.splitlines()
    ]

    order = _date_order(lines, date_order)
    messages: list[WhatsAppMessage] = []
    current: WhatsAppMessage | None = None
    recognized_lines = 0
    timestamp_dates: list[date] = []

    for line in lines:
        match = ANDROID_LINE.match(line) or IPHONE_LINE.match(line)

        if match:
            recognized_lines += 1
            timestamp = _parse_timestamp(
                match.group("date"),
                match.group("time"),
                order,
                timezone_info,
            )
            timestamp_dates.append(timestamp.date())
            body = match.group("body").strip()

            # A new message starts here, so finalize the previous one.
            if current is not None:
                messages.append(current)
                current = None

            if _is_system_message(body):
                continue

            # A message without a sender separator is usually a system notice.
            sender_match = re.match(r"^(?P<sender>[^:]{1,100}):\s*(?P<text>.*)$", body)
            if not sender_match:
                continue

            sender = sender_match.group("sender").strip()
            text = _clean_text(sender_match.group("text"))

            if not text or _is_deleted_message(text):
                continue

            current = WhatsAppMessage(
                timestamp=timestamp,
                sender=sender,
                text=text,
            )
        elif current is not None and line.strip():
            # Lines without a timestamp continue the preceding message.
            continuation = _clean_text(line)
            if continuation:
                current.text += "\n" + continuation

    if current is not None:
        messages.append(current)

    if recognized_lines == 0:
        raise ValueError(f"No recognizable WhatsApp messages found in {path.name}")

    return messages, min(timestamp_dates), max(timestamp_dates)
